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

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw transcript")
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

    def fake_extract(
        transcript, ctx, framework, now, format_id="DAP", profession="therapy", features=None
    ):
        calls["format_id"] = format_id
        calls["profession"] = profession
        calls["features"] = features
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


# ---------------------------------------------------------------------------
# Finding 3: the on-screen verify check must name the ACTIVE format's headings,
# not a hardcoded DAP trio, so a filed SOAP/GROW note is not falsely failed.
# ---------------------------------------------------------------------------


def test_verify_note_check_uses_active_format_headings(vault, monkeypatch):
    from debrief import pipeline
    import debrief.verify as verify_mod

    captured: dict = {}

    def fake_verify(checks):
        captured["checks"] = checks
        return [{**c, "confirmed": True, "what_i_see": "ok"} for c in checks]

    # Stub the verify layer and the surface openers so nothing touches real apps.
    monkeypatch.setattr(verify_mod, "verify_on_screen", fake_verify)
    monkeypatch.setattr(pipeline.vault, "obsidian_open_uri", lambda p: "obsidian://x")
    monkeypatch.setattr(pipeline.vault, "obsidian_available", lambda: True)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *a, **k: None)

    soap_sections = [
        {"key": "subjective", "heading": "Subjective"},
        {"key": "objective", "heading": "Objective"},
        {"key": "assessment", "heading": "Assessment"},
        {"key": "plan", "heading": "Plan"},
    ]
    plan = {
        "client_id": "C-0001",
        "client": {"name": "Bob Smith", "first_name": "Bob"},
        "corrected_transcript": "session transcript",
        "note": {
            "subjective": "s",
            "objective": "o",
            "assessment": "a",
            "plan": "p",
            "risk_present": False,
            "interventions": [],
            "themes": [],
            "client_quotes": [],
        },
        "actions": [],
        "session_meta": {
            "session_date": "2026-07-18",
            "format": "SOAP",
            "sections": soap_sections,
            "framework": "CBT",
            "features": {},
        },
    }

    pipeline.execute_plan(plan, verify=True)

    obsidian = [c for c in captured["checks"] if c["surface"] == "obsidian"]
    assert obsidian, "expected an Obsidian verification check"
    question = obsidian[0]["question"]
    assert "Subjective" in question and "Objective" in question
    assert "Data, Assessment, and Plan" not in question
