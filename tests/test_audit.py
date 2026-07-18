"""Tests for debrief.audit. Temp-dir based: never touches the real vault."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    """Point the activity log at a temp vault and return the audit module."""
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")

    import debrief.audit as audit_mod

    # Rebind the module-level VAULT_DIR that audit.py imported by value.
    monkeypatch.setattr(audit_mod, "VAULT_DIR", tmp_path / "vault")
    return audit_mod


def _sample_result() -> dict:
    """A fabricated executed-debrief result covering every optional section."""
    return {
        "client_id": "C-0001",
        "client": {"name": "Bob Smith", "first_name": "Bob"},
        "corrected_transcript": "This was session fourteen and we talked at length.",
        "note_path": str(
            Path("/anywhere")  # replaced per test to sit under the temp vault
        ),
        "actions": [
            {
                "type": "schedule_followup",
                "status": "ok",
                "datetime_display": "Tuesday, July 21, 2026 at 3:00 PM",
                "resolved_datetime": "2026-07-21T15:00:00",
            },
            {"type": "draft_client_email", "status": "ok"},
        ],
        "deduped_actions": [{"type": "schedule_followup"}],
        "verification": [
            {"surface": "calendar", "confirmed": True, "what_i_see": "An event for Bob at 3 PM is visible on Tuesday July 21."},
            {"surface": "obsidian", "confirmed": False, "what_i_see": "x " * 200},
        ],
        "timings": {"transcribe": 1.2, "extract": 2.1, "verify": 4.2},
        "unsupported_requests": ["Text the client a reminder tomorrow morning."],
        "errors": [{"stage": "mail", "error": "no email address"}],
    }


def _result_in_vault(audit, tmp_path) -> dict:
    """Sample result whose note_path lives under the temp vault."""
    result = _sample_result()
    note = audit.VAULT_DIR / "Clients" / "C-0001" / "Sessions" / "2026-07-18-session.md"
    result["note_path"] = str(note)
    return result


def test_entry_created_with_frontmatter_once(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    path = audit.log_debrief_run(result)

    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")

    # Frontmatter header present exactly once.
    assert text.startswith("---\n")
    assert text.count("type: activity-log") == 1
    assert "date:" in text.split("---", 2)[1]

    # Core bullets rendered.
    assert "### " in text  # a time-stamped heading
    assert "- Heard:" in text
    assert "- Booked: Tuesday, July 21, 2026 at 3:00 PM" in text
    assert "- Email: drafted" in text
    assert "- Timing:" in text


def test_second_append_does_not_duplicate_frontmatter(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    path1 = audit.log_debrief_run(result)
    path2 = audit.log_debrief_run(result)

    assert path1 == path2  # same day file
    text = path2.read_text(encoding="utf-8")

    # Frontmatter block written once, but two entries appended.
    assert text.count("type: activity-log") == 1
    assert text.count("- Booked:") == 2
    # Two heading lines (one per run).
    assert text.count("### ") == 2


def test_wikilinks_well_formed(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    path = audit.log_debrief_run(result)
    text = path.read_text(encoding="utf-8")

    # Client wikilink to the profile, with a display alias.
    assert "[[Clients/C-0001/_Profile|Bob Smith]]" in text
    # Note wikilink relative to the vault, extension dropped, aliased to the stem.
    assert "[[Clients/C-0001/Sessions/2026-07-18-session|2026-07-18-session]]" in text
    assert ".md]]" not in text  # extension must be dropped in links


def test_optional_sections_render(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    text = audit.log_debrief_run(result).read_text(encoding="utf-8")

    # Verified block: a confirmed check and a truncated failing check.
    assert "- Verified:" in text
    assert "calendar: ✓" in text
    assert "obsidian: ✗" in text
    assert "..." in text  # long what_i_see was truncated

    # Deduped, unsupported, and errors sections present because data was supplied.
    assert "- Deduped: 1 duplicate action dropped" in text
    assert "- Unsupported requests:" in text
    assert "Text the client a reminder tomorrow morning." in text
    assert "- Errors:" in text
    assert "mail: no email address" in text


def test_optional_sections_omitted_when_empty(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    result["deduped_actions"] = []
    result["unsupported_requests"] = []
    result["errors"] = []
    result["verification"] = []
    text = audit.log_debrief_run(result).read_text(encoding="utf-8")

    assert "- Deduped:" not in text
    assert "- Unsupported requests:" not in text
    assert "- Errors:" not in text
    assert "- Verified:" not in text
    # Booked and Email are always present.
    assert "- Booked:" in text
    assert "- Email:" in text


def test_booked_and_email_fallback_phrases(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    result["actions"] = []  # no follow-up, no email
    text = audit.log_debrief_run(result).read_text(encoding="utf-8")
    assert "- Booked: none requested" in text
    assert "- Email: none" in text


def test_no_em_dash_in_output(audit, tmp_path):
    result = _result_in_vault(audit, tmp_path)
    # Inject an em dash via model-derived text to prove the final sweep strips it.
    result["verification"][0]["what_i_see"] = "Event visible — confirmed on screen."
    result["unsupported_requests"] = ["Do this — then that."]
    text = audit.log_debrief_run(result).read_text(encoding="utf-8")
    assert "—" not in text, "em dash must never appear in the activity log"


def test_logging_failure_does_not_raise_out(audit, tmp_path, monkeypatch):
    result = _result_in_vault(audit, tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(builtins, "open", _boom)

    # Must not raise, must return None, and must record an audit error.
    path = audit.log_debrief_run(result)
    assert path is None
    assert any(
        isinstance(e, dict) and e.get("stage") == "audit" for e in result["errors"]
    ), "a logging failure must be recorded in result['errors']"
