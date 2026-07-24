"""Tests for GET/POST /api/settings. Temp vault; no live server, no model."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    # Scaffold the settings store into the temp vault (no full vault needed).
    app_module.settings_store.ensure_settings()
    return TestClient(app_module.app), vault_dir


def test_get_settings_shape(client):
    tc, _ = client
    resp = tc.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"settings", "dictionary", "professions", "formats"}
    assert body["settings"]["profession"] == "therapy"
    assert body["dictionary"] == ""
    # Phase C: formats now returns the registry summaries (builtins first).
    fmt_ids = [f["id"] for f in body["formats"]]
    assert fmt_ids[:4] == ["DAP", "SOAP", "GROW", "meeting-memo"]
    dap = next(f for f in body["formats"] if f["id"] == "DAP")
    assert dap["clinical"] is True and "name" in dap
    grow = next(f for f in body["formats"] if f["id"] == "GROW")
    assert grow["clinical"] is False
    ids = {p["id"] for p in body["professions"]}
    assert {"therapy", "slp", "coaching", "legal_meeting"} <= ids
    therapy = next(p for p in body["professions"] if p["id"] == "therapy")
    assert therapy["clinical"] is True


def test_post_settings_persists(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings",
        json={"settings": {"note_format": "SOAP", "features": {"verify": False}}},
    )
    assert resp.status_code == 200
    # Read back through a fresh GET to prove persistence.
    body = tc.get("/api/settings").json()
    assert body["settings"]["note_format"] == "SOAP"
    assert body["settings"]["features"]["verify"] is False
    assert body["settings"]["features"]["calendar"] is True


def test_post_dictionary_persists(client):
    tc, _ = client
    resp = tc.post("/api/settings", json={"dictionary": "Zoloft is sertraline"})
    assert resp.status_code == 200
    assert "sertraline" in resp.json()["dictionary"]
    assert "sertraline" in tc.get("/api/settings").json()["dictionary"]


def test_post_invalid_profession_400(client):
    tc, _ = client
    resp = tc.post("/api/settings", json={"settings": {"profession": "astrology"}})
    assert resp.status_code == 400


def test_post_invalid_stt_engine_400(client):
    tc, _ = client
    resp = tc.post("/api/settings", json={"settings": {"stt_engine": "ears"}})
    assert resp.status_code == 400


def test_post_invalid_note_format_400(client):
    tc, _ = client
    resp = tc.post("/api/settings", json={"settings": {"note_format": "HAIKU"}})
    assert resp.status_code == 400


def test_post_valid_profession_ok(client):
    tc, _ = client
    resp = tc.post("/api/settings", json={"settings": {"profession": "coaching"}})
    assert resp.status_code == 200
    assert resp.json()["settings"]["profession"] == "coaching"


# ---------------------------------------------------------------------------
# Finding 4: type confusion in settings patches must 400, not 500, and persist
# nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"note_format": 5},
        {"note_format": ["DAP"]},
        {"stt_engine": ["parakeet"]},
        {"features": "yes"},
        {"features": {"calendar": "no"}},
    ],
)
def test_post_bad_typed_patch_400_and_persists_nothing(client, patch):
    tc, _ = client
    resp = tc.post("/api/settings", json={"settings": patch})
    assert resp.status_code == 400
    # Nothing was written: the stored settings still hold the defaults.
    body = tc.get("/api/settings").json()["settings"]
    assert body["note_format"] == "DAP"
    assert body["stt_engine"] == "parakeet"
    assert body["features"] == {
        "calendar": True,
        "email": True,
        "verify": True,
        "assistant": True,
    }


# ---------------------------------------------------------------------------
# Finding 2: a path-traversal note_format is rejected at the API boundary.
# ---------------------------------------------------------------------------


def test_post_traversal_note_format_400(client):
    tc, _ = client
    resp = tc.post(
        "/api/settings",
        json={"settings": {"note_format": "../../../etc/passwd"}},
    )
    assert resp.status_code == 400
    assert tc.get("/api/settings").json()["settings"]["note_format"] == "DAP"
