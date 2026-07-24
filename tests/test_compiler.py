"""Tests for debrief.compiler: document text extraction and the two compile paths.

All non-live. No real network, no real model, no real Gemini. subprocess, the
local model (llm.chat), and requests.post are monkeypatched. The Gemini tests
assert the request shape (URL carries the model, the key is a header and never in
the URL, responseSchema is in the body) and that the key can never ride out in an
error message.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from debrief import compiler, formats


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the settings store (and thus custom formats) at a temp vault."""
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    import debrief.settings_store as settings_store

    settings_store.ensure_settings()
    return settings_store


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------


def test_extract_md_reads_directly():
    out = compiler.extract_document_text(b"# Heading\n\nBody text.", "note.md")
    assert out["text"] == "# Heading\n\nBody text."
    assert out["chars"] == len("# Heading\n\nBody text.")
    assert out["truncated"] is False
    assert out["pdf_unsupported"] is False


def test_extract_txt_replaces_bad_bytes():
    # An invalid utf-8 byte must not raise; it is replaced.
    out = compiler.extract_document_text(b"good \xff text", "note.txt")
    assert "good" in out["text"] and "text" in out["text"]


def test_extract_truncates_at_cap():
    big = ("x" * (compiler.MAX_DOC_CHARS + 500)).encode("utf-8")
    out = compiler.extract_document_text(big, "big.txt")
    assert out["truncated"] is True
    assert out["chars"] == compiler.MAX_DOC_CHARS


def test_extract_rejects_unknown_extension():
    with pytest.raises(ValueError):
        compiler.extract_document_text(b"data", "note.rtf")


def test_extract_docx_via_textutil(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = b"Subjective\nObjective\nAssessment\nPlan\n"
        stderr = b""

    def fake_run(cmd, **kw):
        assert cmd[0] == "textutil"
        assert "-stdout" in cmd
        return FakeProc()

    monkeypatch.setattr(compiler.subprocess, "run", fake_run)
    out = compiler.extract_document_text(b"PK\x03\x04fake docx", "template.docx")
    assert "Subjective" in out["text"]
    assert out["pdf_unsupported"] is False


def test_extract_pdf_unsupported_when_no_pdftotext(monkeypatch):
    monkeypatch.setattr(compiler.shutil, "which", lambda name: None)
    out = compiler.extract_document_text(b"%PDF-1.4 fake", "template.pdf")
    assert out["pdf_unsupported"] is True
    assert out["text"] == ""
    assert out["chars"] == 0


def test_extract_pdf_via_pdftotext_when_present(monkeypatch):
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/usr/bin/pdftotext")

    class FakeProc:
        returncode = 0
        stdout = b"Goal\nReality\nOptions\nWay forward\n"
        stderr = b""

    monkeypatch.setattr(compiler.subprocess, "run", lambda cmd, **kw: FakeProc())
    out = compiler.extract_document_text(b"%PDF-1.4 fake", "template.pdf")
    assert out["pdf_unsupported"] is False
    assert "Reality" in out["text"]


# ---------------------------------------------------------------------------
# compile_local: post-processing (slugify / reserved / dedupe / collision)
# ---------------------------------------------------------------------------


def _canned(raw):
    def fake_chat(messages, schema=None, **kw):
        assert schema is compiler.FORMAT_SPEC_SCHEMA
        return raw

    return fake_chat


def test_compile_local_slugifies_and_drops_reserved(store, monkeypatch):
    from debrief import llm

    raw = {
        "name": "Intake Summary",
        "clinical": False,
        "style_rules": "Plain prose.",
        "sections": [
            {"key": "Presenting Concern", "heading": "Presenting Concern", "description": "x"},
            {"key": "themes", "heading": "Themes", "description": "reserved, dropped"},
            {"key": "History", "heading": "History", "description": "y"},
            {"key": "history", "heading": "History again", "description": "dup, dropped"},
        ],
    }
    monkeypatch.setattr(llm, "chat", _canned(raw))
    spec = compiler.compile_local("some document text", "therapy")
    keys = [s["key"] for s in spec["sections"]]
    assert keys == ["presenting_concern", "history"]  # reserved + dup gone
    assert spec["id"] == "intake-summary"
    assert spec["risk_section"] is False  # derived from clinical=False


def test_compile_local_risk_section_follows_clinical(store, monkeypatch):
    from debrief import llm

    raw = {
        "name": "Clinical Progress",
        "clinical": True,
        "sections": [{"key": "narrative", "heading": "Narrative", "description": "z"}],
    }
    monkeypatch.setattr(llm, "chat", _canned(raw))
    spec = compiler.compile_local("doc", "therapy")
    assert spec["clinical"] is True
    assert spec["risk_section"] is True


def test_compile_local_id_collision_suffix(store, monkeypatch):
    from debrief import llm

    # An existing custom spec claims the id "intake".
    formats.save_custom({"name": "Intake", "sections": [{"key": "a"}]})

    raw = {"name": "Intake", "sections": [{"key": "one", "heading": "One", "description": ""}]}
    monkeypatch.setattr(llm, "chat", _canned(raw))
    spec = compiler.compile_local("doc", "therapy")
    assert spec["id"] == "intake-2"


def test_compile_local_no_usable_sections_raises(store, monkeypatch):
    from debrief import llm

    raw = {"name": "Empty", "sections": [{"key": "themes"}, {"key": "risk"}]}
    monkeypatch.setattr(llm, "chat", _canned(raw))
    with pytest.raises(compiler.CompilerError):
        compiler.compile_local("doc", "therapy")


# ---------------------------------------------------------------------------
# compile_gemini: request shape + key handling
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _gemini_payload(spec_json: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": spec_json}]}}]}


def test_compile_gemini_request_shape(store, monkeypatch):
    import debrief.config as config

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        captured["timeout"] = timeout
        spec = {
            "name": "Cloud Format",
            "sections": [{"key": "one", "heading": "One", "description": "d"}],
            "style_rules": "Prose.",
            "clinical": True,
            "compiled_prompt_layer": "Keep it warm and specific.",
        }
        import json as _json

        return _FakeResp(200, payload=_gemini_payload(_json.dumps(spec)))

    monkeypatch.setattr(compiler.requests, "post", fake_post)

    out = compiler.compile_gemini("document text", "SECRET-KEY-123")

    # URL carries the configured model, and the key is NOT in the URL.
    assert config.GEMINI_MODEL in captured["url"]
    assert captured["url"].endswith(f"models/{config.GEMINI_MODEL}:generateContent")
    assert "SECRET-KEY-123" not in captured["url"]
    assert "key=" not in captured["url"]
    # The key rides in the x-goog-api-key header.
    assert captured["headers"]["x-goog-api-key"] == "SECRET-KEY-123"
    # responseSchema is present in the request body.
    gen = captured["body"]["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert "responseSchema" in gen
    assert captured["timeout"] == 60
    # The compiled spec and prompt layer come back.
    assert out["spec"]["name"] == "Cloud Format"
    assert out["spec"]["risk_section"] is True
    assert out["prompt_layer"] == "Keep it warm and specific."


def test_compile_gemini_scrubs_key_from_error(store, monkeypatch):
    # A non-200 whose body echoes the key back must not leak it in the error.
    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        return _FakeResp(
            403,
            payload=None,
            text='{"error": "bad key API_KEY_LEAK_9000 rejected"}',
        )

    monkeypatch.setattr(compiler.requests, "post", fake_post)
    with pytest.raises(compiler.CompilerError) as ei:
        compiler.compile_gemini("doc", "API_KEY_LEAK_9000")
    msg = str(ei.value)
    assert "API_KEY_LEAK_9000" not in msg
    assert ei.value.status == 403


def test_compile_gemini_parse_failure_raises(store, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        return _FakeResp(200, payload={"candidates": []})  # no parts

    monkeypatch.setattr(compiler.requests, "post", fake_post)
    with pytest.raises(compiler.CompilerError):
        compiler.compile_gemini("doc", "k")


# ---------------------------------------------------------------------------
# dry_run: renders a note, writes nothing to _Settings
# ---------------------------------------------------------------------------


def test_dry_run_returns_note_and_writes_nothing(store, monkeypatch):
    from debrief import llm

    def fake_chat(messages, schema=None, **kw):
        return {
            "note": {"data": "Client narrative.", "interventions": [], "themes": [], "client_quotes": []},
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(llm, "chat", fake_chat)

    spec = {
        "id": "demo-format",
        "name": "Demo",
        "clinical": False,
        "sections": [{"key": "data", "heading": "Data", "description": ""}],
        "style_rules": "",
        "prompt_guidance": "",
        "risk_section": False,
    }
    out = compiler.dry_run(spec, "therapy")
    assert out["note"]["data"] == "Client narrative."
    assert out["sections"] == [{"key": "data", "heading": "Data"}]

    # Nothing was written to _Settings: no format file, no prompt layer.
    assert list(store.formats_dir().glob("*.json")) == []
    assert list(store.profile_dir().glob("*.prompt.md")) == []
