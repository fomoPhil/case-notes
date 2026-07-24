"""Tests for debrief.formats: the format registry and the schema generator.

The golden test embeds a FROZEN copy of the old hand-written EXTRACT_SCHEMA
literal (as it stood before formats.py existed) and asserts that
build_extract_schema(get_spec("DAP")) deep-equals it. This is the backward-compat
tripwire: any drift in the generated DAP schema turns this test red. These are
deterministic and never call the model.
"""

from __future__ import annotations

import datetime as dt

import pytest

from debrief import formats


# The old extract.py:24-102 EXTRACT_SCHEMA literal, frozen here verbatim. Do NOT
# regenerate this from the code under test: it must be an independent copy so it
# actually guards against drift.
_FROZEN_DAP_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "note": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "data": {"type": "string"},
                "assessment": {"type": "string"},
                "plan": {"type": "string"},
                "risk_present": {"type": "boolean"},
                "risk": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "assessed": {"type": "boolean"},
                        "ideation": {"type": "string"},
                        "plan_intent_means": {"type": "string"},
                        "protective_factors": {"type": "string"},
                        "interventions_taken": {"type": "string"},
                    },
                    "required": [
                        "assessed",
                        "ideation",
                        "plan_intent_means",
                        "protective_factors",
                        "interventions_taken",
                    ],
                },
                "interventions": {"type": "array", "items": {"type": "string"}},
                "themes": {"type": "array", "items": {"type": "string"}},
                "client_quotes": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "data",
                "assessment",
                "plan",
                "risk_present",
                "risk",
                "interventions",
                "themes",
                "client_quotes",
            ],
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["schedule_followup", "draft_client_email"],
                    },
                    "datetime_utterance": {"type": ["string", "null"]},
                    "duration_min": {"type": ["integer", "null"]},
                    "purpose": {"type": ["string", "null"]},
                    "attachment": {"type": ["string", "null"]},
                },
                "required": [
                    "type",
                    "datetime_utterance",
                    "duration_min",
                    "purpose",
                    "attachment",
                ],
            },
        },
        "unsupported_requests": {"type": "array", "items": {"type": "string"}},
        "next_session_suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "note",
        "actions",
        "unsupported_requests",
        "next_session_suggestions",
    ],
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the settings store (and thus custom formats) at a temp vault."""
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    import debrief.settings_store as settings_store

    settings_store.ensure_settings()
    return settings_store


# ---------------------------------------------------------------------------
# Golden DAP schema
# ---------------------------------------------------------------------------


def test_golden_dap_schema_deep_equals_old_literal():
    generated = formats.build_extract_schema(formats.get_spec("DAP"))
    assert generated == _FROZEN_DAP_SCHEMA


def test_golden_dap_required_order_is_preserved():
    # required is a list, so order matters (unlike dict property order).
    generated = formats.build_extract_schema(formats.get_spec("DAP"))
    assert generated["properties"]["note"]["required"] == (
        _FROZEN_DAP_SCHEMA["properties"]["note"]["required"]
    )


def test_extract_module_symbol_matches_generated():
    from debrief import extract

    assert extract.EXTRACT_SCHEMA == _FROZEN_DAP_SCHEMA


# ---------------------------------------------------------------------------
# Other builtin schema shapes
# ---------------------------------------------------------------------------


def test_soap_schema_shape():
    schema = formats.build_extract_schema(formats.get_spec("SOAP"))
    note = schema["properties"]["note"]
    assert note["required"] == [
        "subjective",
        "objective",
        "assessment",
        "plan",
        "risk_present",
        "risk",
        "interventions",
        "themes",
        "client_quotes",
    ]
    # Clinical: risk block present and byte-identical to the DAP risk block.
    assert note["properties"]["risk"] == (
        _FROZEN_DAP_SCHEMA["properties"]["note"]["properties"]["risk"]
    )


def test_grow_schema_has_no_risk():
    schema = formats.build_extract_schema(formats.get_spec("GROW"))
    note = schema["properties"]["note"]
    assert "risk" not in note["properties"]
    assert "risk_present" not in note["properties"]
    assert note["required"] == [
        "goal",
        "reality",
        "options",
        "way_forward",
        "interventions",
        "themes",
        "client_quotes",
    ]


def test_meeting_memo_schema_shape():
    schema = formats.build_extract_schema(formats.get_spec("meeting-memo"))
    note = schema["properties"]["note"]
    assert "risk" not in note["properties"]
    assert note["required"] == [
        "attendees",
        "discussion",
        "decisions",
        "action_items",
        "interventions",
        "themes",
        "client_quotes",
    ]


def test_top_level_shell_is_stable_across_formats():
    for fid in ("SOAP", "GROW", "meeting-memo"):
        schema = formats.build_extract_schema(formats.get_spec(fid))
        assert schema["properties"]["actions"] == (
            _FROZEN_DAP_SCHEMA["properties"]["actions"]
        )
        assert schema["required"] == _FROZEN_DAP_SCHEMA["required"]


# ---------------------------------------------------------------------------
# System prompt generation
# ---------------------------------------------------------------------------


def test_system_prompt_clinical_keeps_risk_and_fills_tokens():
    prompt = formats.build_extract_system(formats.get_spec("DAP"), "therapy")
    assert "{{" not in prompt and "RISK:START" not in prompt
    assert "## Risk" in prompt
    assert "risk_present" in prompt
    # Format guidance and vocab table were injected.
    assert "audit-critical trio" in prompt
    assert "Framework vocabulary table" in prompt


def test_system_prompt_non_clinical_strips_risk():
    prompt = formats.build_extract_system(formats.get_spec("GROW"), "coaching")
    assert "{{" not in prompt and "RISK:START" not in prompt
    assert "risk_present" not in prompt
    assert "## Risk" not in prompt
    assert "Way forward" in prompt


def test_system_prompt_appends_custom_layer(store):
    (store.profile_dir() / "DAP.prompt.md").write_text(
        "Always mention the treatment plan number.", encoding="utf-8"
    )
    prompt = formats.build_extract_system(formats.get_spec("DAP"), "therapy")
    assert "Always mention the treatment plan number." in prompt


# ---------------------------------------------------------------------------
# Custom spec round-trip + validation
# ---------------------------------------------------------------------------


def test_custom_spec_round_trip(store):
    spec = {
        "name": "Intake Summary",
        "clinical": False,
        "risk_section": False,
        "sections": [
            {"key": "Presenting Concern", "heading": "Presenting Concern"},
            {"heading": "History"},
        ],
        "prompt_guidance": "Summarize the intake.",
    }
    saved = formats.save_custom(spec)
    assert saved["id"] == "intake-summary"
    assert [s["key"] for s in saved["sections"]] == ["presenting_concern", "history"]

    loaded = formats.get_spec("intake-summary")
    assert loaded == saved
    assert formats.is_known("intake-summary")
    ids = [s["id"] for s in formats.list_specs()]
    assert ids[:4] == ["DAP", "SOAP", "GROW", "meeting-memo"]
    assert "intake-summary" in ids


def test_custom_spec_schema_generates(store):
    saved = formats.save_custom(
        {"name": "Two Part", "sections": [{"key": "one"}, {"key": "two"}]}
    )
    schema = formats.build_extract_schema(saved)
    note = schema["properties"]["note"]
    assert note["required"] == ["one", "two", "interventions", "themes", "client_quotes"]
    assert "risk" not in note["properties"]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "sections": []},
        {"name": "x", "sections": [{"key": "risk"}]},
        {"name": "x", "sections": [{"key": "risk_present"}]},
        {"name": "x", "sections": [{"key": "themes"}]},
        {"name": "x", "sections": [{"key": "interventions"}]},
        {"name": "x", "sections": [{"key": "client_quotes"}]},
        {"sections": [{"key": "a"}]},
        {"name": "x", "sections": [{"key": "a"}, {"key": "A"}]},  # slug collision
    ],
)
def test_invalid_specs_are_rejected(store, bad):
    with pytest.raises(formats.InvalidFormatSpec):
        formats.save_custom(bad)


def test_unknown_format_raises():
    with pytest.raises(formats.UnknownFormat):
        formats.get_spec("does-not-exist")


def test_get_spec_or_default_falls_back():
    spec = formats.get_spec_or_default("does-not-exist")
    assert spec["id"] == "DAP"


def test_load_custom_ignores_malformed_json(store):
    (store.formats_dir() / "broken.json").write_text("{ not json", encoding="utf-8")
    assert formats.load_custom("broken") is None
    # A malformed file never appears in the summaries list.
    assert "broken" not in {s["id"] for s in formats.list_specs()}
