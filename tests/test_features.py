"""Feature toggle tests: calendar, email, verify, assistant gates.

Covers the four seams the settings.features flags feed:
  1. build_extract_system drops guidance for a disabled action type.
  2. pipeline normalization filters disabled action types out of the plan.
  3. execute_plan defensively marks a disabled action type "disabled in settings".
  4. the assistant endpoints 403 when off and 200 when on.
  5. /api/execute's verify default flows from settings.features.verify.

Defaults are all-on, so the untouched suite behaves identically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
from debrief import formats, pipeline


# ---------------------------------------------------------------------------
# 1. Prompt omits disabled guidance regions
# ---------------------------------------------------------------------------


def test_prompt_all_on_keeps_both_actions():
    prompt = formats.build_extract_system(formats.get_spec("DAP"), "therapy")
    assert "schedule_followup" in prompt
    assert "draft_client_email" in prompt
    # Markers never leak into the rendered prompt.
    assert "ACTION-CALENDAR" not in prompt and "ACTION-EMAIL" not in prompt


def test_prompt_calendar_off_strips_schedule_guidance():
    prompt = formats.build_extract_system(
        formats.get_spec("DAP"), "therapy", {"calendar": False, "email": True}
    )
    assert "schedule_followup" not in prompt
    assert "draft_client_email" in prompt
    assert "ACTION-CALENDAR" not in prompt


def test_prompt_email_off_strips_email_guidance():
    prompt = formats.build_extract_system(
        formats.get_spec("DAP"), "therapy", {"calendar": True, "email": False}
    )
    assert "draft_client_email" not in prompt
    assert "schedule_followup" in prompt
    assert "ACTION-EMAIL" not in prompt


# ---------------------------------------------------------------------------
# 2. Pipeline normalization filters disabled action types
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import debrief.config as config
    import debrief.vault as vault_mod

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path / "vault")
    vault_mod.ensure_vault()
    return vault_mod


def _stub_extract_with_both_actions(monkeypatch):
    from debrief import stt, extract as extract_mod

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "corrected")

    def fake_extract(transcript, ctx, framework, now, format_id="DAP", profession="therapy", features=None):
        return {
            "note": {},
            "actions": [
                {"type": "schedule_followup", "datetime_utterance": "next Tuesday at 3", "duration_min": 50},
                {"type": "draft_client_email", "purpose": "confirmation", "attachment": None},
            ],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(extract_mod, "extract", fake_extract)


def test_normalization_drops_calendar_when_disabled(vault, monkeypatch):
    import debrief.settings_store as settings_store

    settings_store.save({"features": {"calendar": False, "email": True}})
    _stub_extract_with_both_actions(monkeypatch)

    plan = pipeline.transcribe_and_extract("ignored.wav", "C-0001")
    types = [a["type"] for a in plan["actions"]]
    assert "schedule_followup" not in types
    assert "draft_client_email" in types


def test_normalization_drops_email_when_disabled(vault, monkeypatch):
    import debrief.settings_store as settings_store

    settings_store.save({"features": {"calendar": True, "email": False}})
    _stub_extract_with_both_actions(monkeypatch)

    plan = pipeline.transcribe_and_extract("ignored.wav", "C-0001")
    types = [a["type"] for a in plan["actions"]]
    assert "draft_client_email" not in types
    assert "schedule_followup" in types


# ---------------------------------------------------------------------------
# 3. execute_plan marks disabled action types skipped
# ---------------------------------------------------------------------------


def test_execute_skips_disabled_action_types(vault, monkeypatch):
    import debrief.settings_store as settings_store

    settings_store.save({"features": {"calendar": False, "email": False}})

    plan = {
        "client_id": "C-0001",
        "client": {"name": "Bob Smith", "first_name": "Bob", "email": "bob@example.com"},
        "corrected_transcript": "session transcript",
        "note": {"data": "d", "assessment": "a", "plan": "p", "risk_present": False,
                 "interventions": [], "themes": [], "client_quotes": []},
        "actions": [
            {"type": "schedule_followup", "resolved_datetime": "2026-07-21T15:00", "duration_min": 50, "enabled": True},
            {"type": "draft_client_email", "attachment": None, "enabled": True},
        ],
        "session_meta": {"session_date": "2026-07-18", "format": "DAP", "sections": [], "features": {}},
    }
    # No verify so no screen access; note write still happens into the tmp vault.
    result = pipeline.execute_plan(plan, verify=False)
    statuses = {a["type"]: a for a in result["actions"]}
    assert statuses["schedule_followup"]["status"] == "skipped"
    assert statuses["schedule_followup"]["detail"] == "disabled in settings"
    assert statuses["draft_client_email"]["status"] == "skipped"
    assert statuses["draft_client_email"]["detail"] == "disabled in settings"


# ---------------------------------------------------------------------------
# 4 + 5. API: assistant gate + verify default from settings
# ---------------------------------------------------------------------------


def _healthy_checks():
    return [
        {"name": "Model server reachable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Gemma model loaded", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "ffmpeg on PATH", "ok": True, "detail": "ok", "fix": "", "hard": True},
    ]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    import debrief.audit as audit_mod
    monkeypatch.setattr(audit_mod, "VAULT_DIR", vault_dir)
    app_module.vault.ensure_vault()
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    return TestClient(app_module.app), vault_dir


def test_assistant_403_when_disabled(client):
    tc, _ = client
    from debrief import settings_store

    settings_store.save({"features": {"assistant": False}})
    resp = tc.post("/api/assistant/plan", json={"text": "hi"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "The assistant is turned off in Settings."

    resp2 = tc.post("/api/assistant/execute", json={"proposals": []})
    assert resp2.status_code == 403


def test_assistant_200_when_enabled(client, monkeypatch):
    tc, _ = client
    # Default settings are all-on, so the assistant gate passes.
    monkeypatch.setattr(
        app_module.classify, "classify",
        lambda transcript, has_selected_client: {"route": "assistant", "client_hint": ""},
    )
    monkeypatch.setattr(
        app_module.agent, "run_agent",
        lambda text, now, client_hint=None: {
            "final_text": "done", "proposals": [], "transcript": [],
        },
    )
    resp = tc.post("/api/assistant/plan", json={"text": "make a worksheet"})
    assert resp.status_code == 200
    assert resp.json()["route"] == "assistant"


def test_execute_verify_default_from_settings(client, monkeypatch):
    tc, _ = client
    from debrief import settings_store

    captured: dict = {}

    def fake_execute(plan, verify=True):
        captured["verify"] = verify
        return {
            "actions": [], "deduped_actions": [], "actions_taken": ["note-filed"],
            "note_path": None, "audio_archive_path": None, "obsidian_uri": None,
            "verification": [], "timings": {}, "errors": [],
        }

    monkeypatch.setattr(app_module.pipeline, "execute_plan", fake_execute)

    # verify feature off -> default verify False when the plan omits it.
    settings_store.save({"features": {"verify": False}})
    resp = tc.post("/api/execute", json={"client_id": "C-0001", "note": {}})
    assert resp.status_code == 200
    assert captured["verify"] is False

    # An explicit verify on the plan still wins over the settings default.
    resp = tc.post("/api/execute", json={"client_id": "C-0001", "note": {}, "verify": True})
    assert resp.status_code == 200
    assert captured["verify"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
