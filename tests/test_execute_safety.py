"""What /api/execute will and will not do with a plan from the browser.

The plan round-trips through the page, so anything in it that decides a path,
a recipient, or a screen capture is checked again on the server. Calendar,
Mail, the screen check and the model are all mocked: nothing here touches a
real app.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from debrief import settings_store


def _healthy_checks():
    return [
        {"key": "model_server", "name": "Local AI", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"key": "model_loaded", "name": "AI model", "ok": True, "detail": "ok", "fix": "", "hard": True},
    ]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.records, "VAULT_DIR", vault_dir)
    app_module.vault.ensure_vault()
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0

    calls = {"mail": [], "calendar": [], "verify": []}

    def fake_mail(to, subject, body, attachment):
        calls["mail"].append(to)
        return True

    def fake_event(title, dt, duration):
        calls["calendar"].append((title, dt))
        return True

    def no_model(*a, **k):
        raise app_module.llm.ModelUnavailable("model is off in tests")

    monkeypatch.setattr(app_module.pipeline.actions, "create_mail_draft", fake_mail)
    monkeypatch.setattr(app_module.pipeline.actions, "create_calendar_event", fake_event)
    monkeypatch.setattr(app_module.pipeline.actions, "open_calendar_at", lambda dt: None)
    monkeypatch.setattr(app_module.pipeline.llm, "chat", no_model)
    monkeypatch.setattr(app_module.pipeline.vault, "obsidian_open_uri", lambda p: "obsidian://x")

    from debrief import verify as verify_mod

    def fake_verify(checks):
        calls["verify"].append(checks)
        return []

    monkeypatch.setattr(verify_mod, "verify_on_screen", fake_verify)
    return TestClient(app_module.app), vault_dir, calls


def _plan(client_id="C-0001", **extra):
    plan = {
        "client_id": client_id,
        "client": {"name": "Bob Smith", "first_name": "Bob", "email": "someone@evil.example"},
        "note": {"data": "d", "assessment": "a", "plan": "p"},
        "actions": [{"type": "draft_client_email", "enabled": True}],
        "session_meta": {"session_date": "2026-11-03"},
        "verify": False,
    }
    plan.update(extra)
    return plan


def test_path_in_client_id_is_refused(env):
    tc, vault_dir, calls = env
    r = tc.post("/api/execute", json=_plan("../../escape"))
    assert r.status_code == 400
    assert not (vault_dir.parent.parent / "escape").exists()
    assert calls["mail"] == []


def test_unknown_client_is_refused(env):
    tc, vault_dir, calls = env
    r = tc.post("/api/execute", json=_plan("C-9999"))
    assert r.status_code == 404
    assert not (vault_dir / "Clients" / "C-9999").exists()


def test_mail_recipient_comes_from_the_profile_not_the_plan(env):
    tc, _, calls = env
    r = tc.post("/api/execute", json=_plan())
    assert r.status_code == 200, r.text
    assert calls["mail"] == ["bob@example.com"]


def test_debrief_for_unknown_client_is_refused(env):
    tc, _, _ = env
    r = tc.post("/api/debrief", data={"client_id": "C-9999"}, files={"audio": ("a.webm", b"x")})
    assert r.status_code == 404
