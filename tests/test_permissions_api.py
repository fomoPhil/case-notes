"""Tests for the first-run permission triggers and setup marker endpoints.

The osascript runner and the screencapture path are monkeypatched, so nothing
here touches the real Calendar, Mail, or screen. Non-live.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "ensure_vault", lambda: None)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    return TestClient(app_module.app), vault_dir


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_granted(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (True, "Debrief"))
    body = tc.post("/api/permissions/calendar").json()
    assert body["granted"] is True
    assert body["hint"]


def test_calendar_denied(client, monkeypatch):
    tc, _ = client
    denial = "execution error: Not authorized to send Apple events to Calendar. (-1743)"
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (False, denial))
    body = tc.post("/api/permissions/calendar").json()
    assert body["granted"] is False
    assert "Automation" in body["hint"]


def test_calendar_unknown(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (False, "some other error"))
    body = tc.post("/api/permissions/calendar").json()
    assert body["granted"] == "unknown"
    assert "some other error" in body["hint"]


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------


def test_mail_granted(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (True, "Mail"))
    body = tc.post("/api/permissions/mail").json()
    assert body["granted"] is True


def test_mail_denied(client, monkeypatch):
    tc, _ = client
    denial = "execution error: Not authorized to send Apple events to Mail. (-1743)"
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (False, denial))
    body = tc.post("/api/permissions/mail").json()
    assert body["granted"] is False
    assert "Mail" in body["hint"]


def test_mail_unknown(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.actions, "_run_osascript", lambda script: (False, "weird failure"))
    body = tc.post("/api/permissions/mail").json()
    assert body["granted"] == "unknown"
    assert "weird failure" in body["hint"]


def test_mail_script_targets_mail_by_tell(client, monkeypatch):
    """The Mail probe must tell Mail directly (not System Events) so it trips
    Mail's Automation prompt."""
    tc, _ = client
    captured = {}

    def _fake(script):
        captured["script"] = script
        return (True, "Mail")

    monkeypatch.setattr(app_module.actions, "_run_osascript", _fake)
    tc.post("/api/permissions/mail")
    assert 'tell application "Mail"' in captured["script"]
    assert "System Events" not in captured["script"]


# ---------------------------------------------------------------------------
# Screen recording
# ---------------------------------------------------------------------------


def test_screen_granted(client, monkeypatch):
    tc, _ = client

    def _capture_ok(path):
        Path(path).write_bytes(b"x" * 4096)
        return True

    monkeypatch.setattr(app_module.verify, "_capture_and_downscale", _capture_ok)
    body = tc.post("/api/permissions/screen").json()
    assert body["granted"] is True
    assert "restart" in body["hint"].lower() or "reopen" in body["hint"].lower()


def test_screen_denied(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.verify, "_capture_and_downscale", lambda path: False)
    body = tc.post("/api/permissions/screen").json()
    assert body["granted"] is False


# ---------------------------------------------------------------------------
# Setup marker: complete / reset, and the first_run flip in /api/status
# ---------------------------------------------------------------------------


def _stub_status(monkeypatch):
    monkeypatch.setattr(
        app_module.doctor,
        "run_checks",
        lambda: [
            {"name": "Model server reachable", "ok": True, "detail": "", "fix": "", "hard": True},
            {"name": "Gemma model loaded", "ok": True, "detail": "", "fix": "", "hard": True},
            {"name": "Vault writable", "ok": True, "detail": "", "fix": "", "hard": True},
        ],
    )
    monkeypatch.setattr(app_module.models, "detect_servers", lambda: [])
    monkeypatch.setattr(app_module.models, "pick_gemma", lambda: None)


def test_setup_complete_and_reset_flip_first_run(client, monkeypatch):
    tc, vault_dir = client
    _stub_status(monkeypatch)
    marker = vault_dir / app_module._SETUP_MARKER

    # Starts as a first run (no marker).
    assert tc.get("/api/status").json()["first_run"] is True
    assert not marker.exists()

    # Complete writes the marker and first_run flips to False.
    assert tc.post("/api/setup/complete").json() == {"ok": True}
    assert marker.exists()
    app_module._doctor_cache["checks"] = None
    assert tc.get("/api/status").json()["first_run"] is False

    # Reset removes the marker and first_run flips back to True.
    assert tc.post("/api/setup/reset").json() == {"ok": True}
    assert not marker.exists()
    app_module._doctor_cache["checks"] = None
    assert tc.get("/api/status").json()["first_run"] is True


def test_setup_reset_is_idempotent(client, monkeypatch):
    tc, vault_dir = client
    # Reset with no marker present must not error.
    assert tc.post("/api/setup/reset").json() == {"ok": True}
    assert not (vault_dir / app_module._SETUP_MARKER).exists()
