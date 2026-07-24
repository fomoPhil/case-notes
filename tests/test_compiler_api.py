"""Tests for the template-import endpoints. Temp vault, no live server/model/network.

The model-readiness guard and the compile functions are monkeypatched so these
exercise the endpoint contracts: the upload allowlist, the hard cloud consent/key
gate, the 502+fallback error mapping, the dry-run preview, and the save path
persisting both the format and its prompt layer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
from debrief import compiler


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    app_module.settings_store.ensure_settings()
    # The compiler resolves the settings store through config at call time too.
    monkeypatch.setattr(app_module.compiler.config, "VAULT_DIR", vault_dir, raising=False)
    # Model-readiness is not the unit under test here.
    monkeypatch.setattr(app_module, "_require_model_ready", lambda: None)
    return TestClient(app_module.app), vault_dir


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_md_returns_text(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings/import/upload",
        files={"file": ("template.md", b"# Data\n\n# Plan\n", "text/markdown")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"doc_text", "chars", "truncated", "pdf_unsupported"}
    assert "Data" in body["doc_text"]
    assert body["pdf_unsupported"] is False


def test_upload_rejects_bad_extension(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings/import/upload",
        files={"file": ("template.rtf", b"data", "application/rtf")},
    )
    assert resp.status_code == 400


def test_upload_rejects_oversize(client):
    tc, _ = client
    big = b"x" * (app_module._IMPORT_MAX_UPLOAD_BYTES + 10)
    resp = tc.post(
        "/api/settings/import/upload",
        files={"file": ("template.txt", big, "text/plain")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# compile: cloud gate + error mapping, local
# ---------------------------------------------------------------------------


def test_compile_cloud_requires_consent(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings/import/compile",
        json={"doc_text": "x", "mode": "cloud", "api_key": "k", "consent": False},
    )
    assert resp.status_code == 400


def test_compile_cloud_requires_key(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings/import/compile",
        json={"doc_text": "x", "mode": "cloud", "api_key": "", "consent": True},
    )
    assert resp.status_code == 400


def test_compile_cloud_success(client, monkeypatch):
    tc, _ = client

    def fake_gemini(doc_text, api_key):
        return {"spec": {"id": "cloud-fmt", "name": "Cloud"}, "prompt_layer": "layer"}

    monkeypatch.setattr(app_module.compiler, "compile_gemini", fake_gemini)
    resp = tc.post(
        "/api/settings/import/compile",
        json={"doc_text": "x", "mode": "cloud", "api_key": "k", "consent": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spec"]["id"] == "cloud-fmt"
    assert body["prompt_layer"] == "layer"


def test_compile_cloud_error_maps_to_502_with_fallback(client, monkeypatch):
    tc, _ = client

    def boom(doc_text, api_key):
        raise compiler.CompilerError("Gemini returned 500: upstream error", status=500)

    monkeypatch.setattr(app_module.compiler, "compile_gemini", boom)
    resp = tc.post(
        "/api/settings/import/compile",
        json={"doc_text": "x", "mode": "cloud", "api_key": "k", "consent": True},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["fallback"] == "local"
    assert "error" in body


def test_compile_local_returns_spec(client, monkeypatch):
    tc, _ = client

    def fake_local(doc_text, profession):
        return {"id": "local-fmt", "name": "Local", "sections": [{"key": "a"}]}

    monkeypatch.setattr(app_module.compiler, "compile_local", fake_local)
    resp = tc.post(
        "/api/settings/import/compile",
        json={"doc_text": "some text", "mode": "local", "profession": "therapy"},
    )
    assert resp.status_code == 200
    assert resp.json()["spec"]["id"] == "local-fmt"


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def test_preview_runs_dry_run(client, monkeypatch):
    tc, _ = client

    def fake_dry_run(spec, profession):
        return {"note": {"data": "narrative"}, "sections": [{"key": "data", "heading": "Data"}]}

    monkeypatch.setattr(app_module.compiler, "dry_run", fake_dry_run)
    resp = tc.post(
        "/api/settings/import/preview",
        json={
            "spec": {"name": "Prev", "sections": [{"key": "data", "heading": "Data"}]},
            "profession": "therapy",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["note"]["data"] == "narrative"
    assert body["sections"] == [{"key": "data", "heading": "Data"}]


def test_preview_rejects_invalid_spec(client):
    tc, _ = client
    # No sections: fails formats validation -> 400.
    resp = tc.post(
        "/api/settings/import/preview",
        json={"spec": {"name": "Bad", "sections": []}, "profession": "therapy"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# save: persists spec + prompt layer + optional active format
# ---------------------------------------------------------------------------


def test_save_persists_spec_and_prompt_layer_and_active(client):
    tc, vault_dir = client
    spec = {
        "name": "Imported Note",
        "clinical": True,
        "sections": [{"key": "narrative", "heading": "Narrative"}],
        "style_rules": "Prose.",
    }
    resp = tc.post(
        "/api/settings/import/save",
        json={"spec": spec, "prompt_layer": "Always cite the plan.", "set_active": True},
    )
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["id"] == "imported-note"
    assert summary["sections"] == 1
    assert summary["prompt_layer"] is True
    assert summary["active"] is True

    # The format file exists.
    fmt_file = vault_dir / "_Settings" / "formats" / "imported-note.json"
    assert fmt_file.is_file()
    # The prompt layer was written.
    layer_file = vault_dir / "_Settings" / "profile" / "imported-note.prompt.md"
    assert layer_file.is_file()
    assert "Always cite the plan." in layer_file.read_text(encoding="utf-8")
    # The active note format was persisted.
    settings = tc.get("/api/settings").json()["settings"]
    assert settings["note_format"] == "imported-note"


def test_save_without_prompt_layer_writes_no_profile_file(client):
    tc, vault_dir = client
    spec = {"name": "No Layer", "sections": [{"key": "a", "heading": "A"}]}
    resp = tc.post("/api/settings/import/save", json={"spec": spec})
    assert resp.status_code == 200
    assert resp.json()["prompt_layer"] is False
    assert list((vault_dir / "_Settings" / "profile").glob("*.prompt.md")) == []
