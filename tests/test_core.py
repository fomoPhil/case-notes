from datetime import datetime

from app.main import Request, fallback_plan, profile, resolve_datetime


def test_voice_journal_cannot_propose_external_actions():
    request = Request(mode="voice_journal", client_id="C-0001", transcript="Please record that workplace avoidance was discussed and create prep questions.")
    plan = fallback_plan(request, profile(request.client_id))
    assert [action.type for action in plan.actions] == ["write_note"]


def test_datetime_resolution_is_not_model_owned():
    assert resolve_datetime("July 21 2026 3:00 PM") == "2026-07-21T15:00"
    assert resolve_datetime("next Tuesday at 3pm", base=datetime(2026, 7, 18, 9, 0)) == "2026-07-21T15:00"


def test_unknown_client_is_rejected():
    try:
        profile("not-a-client")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("Unknown client was accepted")
