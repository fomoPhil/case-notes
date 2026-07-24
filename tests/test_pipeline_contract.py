"""Contract tests for pipeline.transcribe_and_extract session_meta.

The review UI (Phase G) renders section-by-section from session_meta["sections"]
and hides feature nudges from session_meta["features"], so the plan must always
carry both. Model-bound stages are monkeypatched: no STT, no LLM, no network.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    import debrief.vault as vault_mod

    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path / "vault")
    vault_mod.ensure_vault()
    return vault_mod


def _stub_pipeline(monkeypatch):
    """Stub the transcribe, correct, and extract stages so no model is called."""
    from debrief import pipeline, stt, extract as extract_mod

    monkeypatch.setattr(stt, "transcribe", lambda wav: "raw transcript")
    monkeypatch.setattr(
        stt, "correct_transcript", lambda *a, **k: "corrected transcript"
    )
    canned = {
        "note": {},
        "actions": [],
        "unsupported_requests": [],
        "next_session_suggestions": [],
    }
    calls: dict = {}

    def fake_extract(transcript, ctx, framework, now, format_id="DAP", profession="therapy"):
        calls["format_id"] = format_id
        calls["profession"] = profession
        return dict(canned)

    monkeypatch.setattr(extract_mod, "extract", fake_extract)
    return pipeline, calls


def test_session_meta_carries_sections_and_features(vault, monkeypatch):
    pipeline, _ = _stub_pipeline(monkeypatch)
    plan = pipeline.transcribe_and_extract("ignored.wav", "C-0001")
    meta = plan["session_meta"]

    assert meta["format"] == "DAP"
    # Sections drive the editable review UI.
    assert isinstance(meta["sections"], list) and meta["sections"]
    keys = [s["key"] for s in meta["sections"]]
    assert keys == ["data", "assessment", "plan"]
    assert all({"key", "heading"} <= set(s) for s in meta["sections"])
    # Features drive which nudges/toggles the UI shows.
    assert set(meta["features"]) == {"calendar", "email", "verify", "assistant"}


def test_session_meta_follows_selected_format(vault, monkeypatch):
    import debrief.settings_store as settings_store

    settings_store.save({"note_format": "SOAP", "profession": "therapy"})
    pipeline, calls = _stub_pipeline(monkeypatch)

    plan = pipeline.transcribe_and_extract("ignored.wav", "C-0001")
    meta = plan["session_meta"]

    assert meta["format"] == "SOAP"
    assert [s["key"] for s in meta["sections"]] == [
        "subjective",
        "objective",
        "assessment",
        "plan",
    ]
    # The selected format and profession flow into the extract call.
    assert calls["format_id"] == "SOAP"
    assert calls["profession"] == "therapy"
