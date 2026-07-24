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


# ---------------------------------------------------------------------------
# Finding 2: path-traversal guard on custom format ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "../secret",
        "sub/dir",
        "..",
        "a/../../b",
        "with space",
    ],
)
def test_traversal_id_raises_unknown_format(store, bad_id):
    # A raw id that is not a bare slug must never resolve to a file on disk.
    with pytest.raises(formats.UnknownFormat):
        formats.load_custom(bad_id)
    # get_spec surfaces the same failure, and is_known reports False.
    with pytest.raises(formats.UnknownFormat):
        formats.get_spec(bad_id)
    assert formats.is_known(bad_id) is False


# ---------------------------------------------------------------------------
# Finding 5: list_specs never advertises an unselectable format
# ---------------------------------------------------------------------------


def test_list_specs_excludes_filename_id_mismatch(store):
    import json

    # A hand-dropped file whose stem is not the spec's slugified id. get_spec is
    # by filename stem, so advertising the internal id would be a dead option.
    spec = {"name": "My Format", "sections": [{"key": "one"}]}
    (store.formats_dir() / "My Format.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    ids = [s["id"] for s in formats.list_specs()]
    assert "my-format" not in ids
    assert "My Format" not in ids

    # A file whose stem is a valid slug but disagrees with the internal id is
    # also skipped (stem "alpha" but content resolves to id "beta").
    (store.formats_dir() / "alpha.json").write_text(
        json.dumps({"name": "Beta", "sections": [{"key": "one"}]}), encoding="utf-8"
    )
    ids2 = [s["id"] for s in formats.list_specs()]
    assert "alpha" not in ids2 and "beta" not in ids2


# ---------------------------------------------------------------------------
# Finding 1: the extract schema cache is keyed by RESOLVED spec id and never
# caches custom specs or unknown-id fallbacks.
# ---------------------------------------------------------------------------


def _canned_extract_result() -> dict:
    return {
        "note": {},
        "actions": [],
        "unsupported_requests": [],
        "next_session_suggestions": [],
    }


def test_extract_sees_custom_spec_edit_between_calls(store, monkeypatch):
    from debrief import extract as extract_mod
    from debrief import llm

    seen: dict = {}

    def fake_chat(messages, schema=None, **kw):
        seen["schema"] = schema
        return _canned_extract_result()

    monkeypatch.setattr(llm, "chat", fake_chat)

    formats.save_custom({"name": "Custom Flow", "sections": [{"key": "one"}]})
    extract_mod.extract(
        "t", {}, "CBT", dt.datetime(2026, 7, 18, 12, 0), format_id="custom-flow"
    )
    first = set(seen["schema"]["properties"]["note"]["properties"])
    assert "one" in first and "two" not in first

    # Edit the same custom spec file on disk, then re-run: a stale cached schema
    # would still carry "one" and silently drop the new section's content.
    formats.save_custom({"name": "Custom Flow", "sections": [{"key": "two"}]})
    extract_mod.extract(
        "t", {}, "CBT", dt.datetime(2026, 7, 18, 12, 0), format_id="custom-flow"
    )
    second = set(seen["schema"]["properties"]["note"]["properties"])
    assert "two" in second and "one" not in second


def test_extract_unknown_id_does_not_poison_cache(store, monkeypatch):
    from debrief import extract as extract_mod
    from debrief import llm

    def fake_chat(messages, schema=None, **kw):
        return _canned_extract_result()

    monkeypatch.setattr(llm, "chat", fake_chat)

    extract_mod.extract(
        "t", {}, "CBT", dt.datetime(2026, 7, 18, 12, 0), format_id="does-not-exist"
    )
    # The unknown id falls back to DAP but must never become a cache key.
    assert "does-not-exist" not in extract_mod._SCHEMA_CACHE
    assert "does-not-exist" not in {k[0] for k in extract_mod._SYSTEM_CACHE}
    # The resolved builtin (DAP) is what gets cached.
    assert "DAP" in extract_mod._SCHEMA_CACHE


# ---------------------------------------------------------------------------
# Security: no external spec may claim a builtin id (cache poisoning + shadow)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"id": "meeting-memo", "sections": [{"key": "a"}]},
        {"name": "Meeting Memo", "sections": [{"key": "a"}]},
    ],
)
def test_validate_spec_rejects_builtin_id(spec):
    with pytest.raises(formats.InvalidFormatSpec):
        formats._validate_spec(spec)


def test_save_custom_suffixes_builtin_id_collision(store):
    # A spec that resolves to the builtin id "meeting-memo" must NOT overwrite or
    # shadow the builtin; save_custom suffixes the id and returns the final slug.
    saved = formats.save_custom(
        {"name": "Meeting Memo", "sections": [{"key": "one", "heading": "One"}]}
    )
    assert saved["id"] == "meeting-memo-2"
    # The builtin is untouched and still resolves to the builtin spec.
    assert formats.get_spec("meeting-memo")["name"] == "Meeting memo"
    # The suffixed custom is a real, selectable format.
    assert formats.get_spec("meeting-memo-2")["id"] == "meeting-memo-2"


def test_load_custom_rejects_hand_dropped_builtin_id_file(store):
    import json

    # A hand-dropped file claiming a builtin id is rejected on load (returns None)
    # rather than silently shadowing the builtin's schema.
    (store.formats_dir() / "meeting-memo.json").write_text(
        json.dumps({"id": "meeting-memo", "sections": [{"key": "x"}]}),
        encoding="utf-8",
    )
    assert formats.load_custom("meeting-memo") is None


def test_validate_spec_caps_section_count(store):
    many = {"name": "Too Many", "sections": [{"key": f"s{i}"} for i in range(13)]}
    with pytest.raises(formats.InvalidFormatSpec):
        formats.save_custom(many)


def test_validate_spec_caps_description_length(store):
    spec = {
        "name": "Long Desc",
        "sections": [{"key": "a", "heading": "A", "description": "x" * 501}],
    }
    with pytest.raises(formats.InvalidFormatSpec):
        formats.save_custom(spec)
