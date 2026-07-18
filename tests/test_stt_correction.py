"""Unit tests for the three-layer glossary correction prompt.

These are deterministic and do NOT call the model: they assert how
debrief.stt assembles the correction system prompt from the static glossary,
the client chart layer, and the framework elevation layer, plus the strict
correction contract (empty input passes through, ordinary words are guarded).

The one call-path test monkeypatches llm.chat so no LM Studio is needed.
"""

from __future__ import annotations

import pytest

from debrief import stt


def test_empty_text_passes_through():
    assert stt.correct_transcript("") == ""
    assert stt.correct_transcript("   ") == "   "


def test_static_glossary_always_present():
    system = stt._build_correction_system(None, None)
    # Core static instructions and the anti-over-correction guard are present.
    assert "transcription correction pass" in system
    assert "anti-over-correction" in system.lower()
    # No client or framework layer when neither is supplied.
    assert "CLIENT CHART CONTEXT" not in system
    assert "This clinician practices" not in system


def test_client_layer_includes_name_diagnosis_meds_framework():
    ctx = {
        "name": "Bob Smith",
        "diagnosis": ["F41.1"],
        "medications": ["sertraline 50mg"],
        "framework": "CBT",
    }
    system = stt._build_correction_system(ctx, None)
    assert "CLIENT CHART CONTEXT" in system
    assert "Bob Smith" in system
    # ICD-10 code is expanded to its spelled-out term.
    assert "F41.1" in system and "generalized anxiety disorder" in system
    assert "sertraline 50mg" in system
    # Framework falls back to the profile's framework when not passed explicitly.
    assert "This clinician practices CBT" in system


def test_explicit_framework_overrides_profile_and_elevates_terms():
    ctx = {"name": "Jane Doe", "framework": "CBT"}
    system = stt._build_correction_system(ctx, "EMDR")
    assert "This clinician practices EMDR" in system
    # EMDR-specific mangle-prone terms are elevated.
    assert "SUDs" in system and "bilateral stimulation" in system


def test_unknown_framework_adds_no_elevation_line():
    assert stt._framework_layer("underwater basket weaving") == ""
    assert stt._framework_layer(None) == ""


def test_no_client_context_only_framework_layer():
    system = stt._build_correction_system(None, "DBT")
    assert "CLIENT CHART CONTEXT" not in system
    assert "This clinician practices DBT" in system
    assert "chain analysis" in system


def test_correct_transcript_passes_assembled_system(monkeypatch):
    captured: dict = {}

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        captured["messages"] = messages
        return "corrected text"

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    out = stt.correct_transcript(
        "raw text",
        {"name": "Bob Smith", "framework": "CBT"},
        "EMDR",
    )
    assert out == "corrected text"
    system_msg = captured["messages"][0]["content"]
    user_msg = captured["messages"][1]["content"]
    assert "Bob Smith" in system_msg
    assert "This clinician practices EMDR" in system_msg
    assert user_msg == "raw text"


def test_glossary_section_within_token_budget():
    # Guard: the static glossary section stays under ~600 tokens. Word-based
    # estimate (English ~= words * 1.33) is closer than chars/4 for this text.
    static = stt._GLOSSARY_PATH.read_text(encoding="utf-8")
    est_tokens = int(len(static.split()) * 1.33)
    assert est_tokens < 600, f"glossary ~{est_tokens} tokens, over budget"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
