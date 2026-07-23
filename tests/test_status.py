"""Tests for GET /api/status. Doctor and detection are mocked; no live server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    # Point the status logic at the temp vault; do not scaffold the real one.
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "ensure_vault", lambda: None)
    # Reset the doctor cache between tests.
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    return TestClient(app_module.app), vault_dir


_SERVERS = [
    {
        "provider": "lmstudio",
        "base_url": "http://localhost:1234/v1",
        "reachable": True,
        "models": ["gemma-4-12b-it-qat"],
        "gemma_model": "gemma-4-12b-it-qat",
    },
    {
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "reachable": False,
        "models": [],
        "gemma_model": None,
    },
]


def _healthy_checks():
    return [
        {"name": "Model server reachable", "ok": True, "detail": "reachable: lmstudio", "fix": "", "hard": True},
        {"name": "Gemma model loaded", "ok": True, "detail": "gemma-4-12b-it-qat", "fix": "", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "ffmpeg on PATH", "ok": True, "detail": "/usr/bin/ffmpeg", "fix": "", "hard": True},
    ]


def _unhealthy_checks():
    return [
        {"name": "Model server reachable", "ok": False, "detail": "no server", "fix": "Start LM Studio.", "hard": True},
        {"name": "Gemma model loaded", "ok": False, "detail": "no gemma", "fix": "Load gemma.", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "ffmpeg on PATH", "ok": True, "detail": "/usr/bin/ffmpeg", "fix": "", "hard": True},
    ]


def test_status_shape_and_ready(client, monkeypatch):
    tc, vault_dir = client
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    monkeypatch.setattr(app_module.models, "detect_servers", lambda: _SERVERS)
    monkeypatch.setattr(
        app_module.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )

    resp = tc.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"servers", "active_model", "vault", "checks", "ready", "first_run"}
    assert body["ready"] is True
    assert body["active_model"]["model"] == "gemma-4-12b-it-qat"
    assert body["vault"]["path"] == str(vault_dir)
    assert body["vault"]["exists"] is True
    assert body["vault"]["writable"] is True
    # No marker yet -> first run.
    assert body["first_run"] is True


def test_status_not_ready(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.doctor, "run_checks", _unhealthy_checks)
    monkeypatch.setattr(app_module.models, "detect_servers", lambda: _SERVERS)
    monkeypatch.setattr(app_module.models, "pick_gemma", lambda: None)

    body = tc.get("/api/status").json()
    assert body["ready"] is False
    assert body["active_model"] is None


def test_first_run_false_when_marker_present(client, monkeypatch):
    tc, vault_dir = client
    (vault_dir / app_module._SETUP_MARKER).write_text("done")
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    monkeypatch.setattr(app_module.models, "detect_servers", lambda: _SERVERS)
    monkeypatch.setattr(
        app_module.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )

    body = tc.get("/api/status").json()
    assert body["first_run"] is False


def test_debrief_returns_503_when_not_ready(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.doctor, "run_checks", _unhealthy_checks)
    # Empty multipart with a tiny audio part; guard should trip before reading.
    resp = tc.post(
        "/api/debrief",
        data={"client_id": "C-0001"},
        files={"audio": ("a.webm", b"x", "audio/webm")},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["fix"] == "Start LM Studio."
