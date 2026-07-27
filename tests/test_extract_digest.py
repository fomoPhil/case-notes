"""Tests for extract()'s optional session-evidence digest.

Two things are being protected here and they pull in opposite directions.

The first is that a debrief with NO session recording must reach the model as
the exact bytes it always did. That is the hero flow, and a feature that shifts
it by a newline has already broken the thing it was meant to help. So the
no-digest prompt is checked against a hand written golden rather than against
whatever the code currently produces, and the schema is checked to be the shared
cached object itself rather than an equal copy of it.

The second is that when a digest IS present the evidence arrives labelled, with
the precedence stated, and with the attribution rule visible in the rendering:
what the recording confirmed the client said appears in quotation marks, and
everything else appears as an unattributed observation with no quotation marks
anywhere near it.

No model is ever called: debrief.llm.chat is stubbed in every test.
"""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

import debrief.extract as extract_mod
import debrief.llm as llm
import debrief.vocab as vocab

NOW = datetime(2026, 7, 25, 15, 0)

CTX = {"name": "Dana Ruiz", "diagnosis": "GAD"}

TRANSCRIPT = "Dana came in low today. We did some breathing."


def _digest(**overrides) -> dict:
    digest = {
        "source_id": "2026-07-25-1430",
        "duration_min": 52,
        "diarized": True,
        "segments": [
            {
                "start_min": 0,
                "end_min": 8,
                "topic": "Sleep since the move",
                "summary": "Client described broken sleep and early waking.",
            },
            {
                "start_min": 8,
                "end_min": 20,
                "topic": "The argument at home",
                "summary": "Client returned to the argument with her sister.",
            },
        ],
        "themes": ["Sleep", "Self blame"],
        "interventions_observed": [
            {
                "name": "Paced breathing",
                "client_response": "Settled a little.",
                "start_min": 12,
            }
        ],
        "client_statements": [
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 4,
                "attribution_confidence": "high",
            },
            {
                "quote": "I still think I am the problem in that house",
                "start_min": 18,
                "attribution_confidence": "low",
            },
        ],
        "risk_language_present": True,
        "risk_excerpts": ["I do not want to be here some days"],
        "practices_or_homework": ["Breathing before bed"],
        "unresolved_threads": ["The argument itself"],
        "notable_shifts": ["Softened when describing the exercise"],
    }
    digest.update(overrides)
    return digest


@pytest.fixture()
def calls(monkeypatch):
    """Every llm.chat call this test made, with its messages and its schema."""
    recorded: list[dict] = []

    def fake_chat(messages, schema=None, max_tokens=None, temperature=None):
        recorded.append({"messages": messages, "schema": schema})
        return {
            "note": {
                "data": "d",
                "assessment": "a",
                "plan": "p",
                "risk_present": False,
                "interventions": [],
                "themes": [],
                "client_quotes": [],
            },
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(llm, "chat", fake_chat)
    return recorded


def _user_message(calls: list[dict]) -> str:
    return calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# The no-digest path, which must not move
# ---------------------------------------------------------------------------


def test_no_digest_prompt_byte_identical(calls):
    """With no digest the user message is the message it has always been.

    The golden is written out here rather than generated, so a change to the
    prompt's shape fails this test instead of quietly redefining "identical".
    """
    extract_mod.extract(f"  {TRANSCRIPT}  ", CTX, "CBT", NOW)

    table = vocab.extract_framework_vocab("CBT", "therapy")
    golden = (
        f"ACTIVE FRAMEWORK: CBT. Use this vocabulary and no other framework's: {table}.\n\n"
        "CLIENT CONTEXT:\n"
        "Name: Dana Ruiz\n"
        "Diagnosis: GAD\n\n"
        f"SESSION DEBRIEF TRANSCRIPT:\n{TRANSCRIPT}\n\n"
        "Produce the JSON object now."
    )
    assert _user_message(calls) == golden
    assert "SESSION EVIDENCE" not in _user_message(calls)


def test_no_digest_prompt_byte_identical_without_framework_vocab(calls):
    """The same golden, fully literal, for a framework with no vocabulary row."""
    extract_mod.extract("Short debrief.", {}, "NOT-A-FRAMEWORK", NOW)

    assert _user_message(calls) == (
        "ACTIVE FRAMEWORK: NOT-A-FRAMEWORK.\n\n"
        "CLIENT CONTEXT:\n"
        "(no prior context on file)\n\n"
        "SESSION DEBRIEF TRANSCRIPT:\n"
        "Short debrief.\n\n"
        "Produce the JSON object now."
    )


def test_no_digest_uses_the_cached_schema_object_itself(calls):
    """Not an equal copy: the very object the cache holds."""
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW)
    assert calls[0]["schema"] is extract_mod._SCHEMA_CACHE["DAP"]
    assert "evidence_conflicts" not in calls[0]["schema"]["properties"]


def test_no_digest_result_gains_no_conflicts_key(calls):
    """A run with no session evidence has no conflicts to have, and says nothing."""
    result = extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW)
    assert "evidence_conflicts" not in result


# ---------------------------------------------------------------------------
# The digest path
# ---------------------------------------------------------------------------


def test_digest_renders_labelled_evidence_block(calls):
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    user = _user_message(calls)

    # Labelled, and sitting between the client context and the debrief.
    assert "SESSION EVIDENCE" in user
    assert user.index("CLIENT CONTEXT:") < user.index("SESSION EVIDENCE")
    assert user.index("SESSION EVIDENCE") < user.index("SESSION DEBRIEF TRANSCRIPT:")

    # The precedence the whole feature turns on, stated in the prompt.
    assert "authoritative on meaning, priority, and clinical judgment" in user
    assert "authoritative on exact wording" in user
    assert "evidence_conflicts" in user

    # The timeline, with the minutes code computed.
    assert "Session timeline:" in user
    assert "- Minutes 0 to 8, Sleep since the move: Client described broken sleep" in user
    assert "- Minutes 8 to 20, The argument at home:" in user

    # Themes, interventions, threads, practices, shifts.
    assert "Themes:" in user and "- Self blame" in user
    assert "Interventions observed:" in user
    assert "- Paced breathing (minute 12): Settled a little." in user
    assert "Unresolved threads:" in user and "- The argument itself" in user
    assert "Practices or homework:" in user and "- Breathing before bed" in user
    assert "Notable shifts:" in user

    # The confirmed quote, in quotation marks, with its minute.
    assert '- "I have not been sleeping since the move" (minute 4)' in user

    # The risk flag: one line, never an assessment, plus the excerpt.
    assert (
        "Risk-related language was flagged in the session recording for your "
        "review. It is not an assessment." in user
    )
    assert "Flagged language from the session (speaker unconfirmed):" in user
    assert "- I do not want to be here some days" in user

    # And the debrief is still the debrief, unchanged and last.
    assert f"SESSION DEBRIEF TRANSCRIPT:\n{TRANSCRIPT}" in user


def test_low_confidence_renders_unattributed_no_quotemarks(calls):
    """Rule 3, at the prompt layer: a low attribution is never offered as a quote."""
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    user = _user_message(calls)

    low = "I still think I am the problem in that house"
    assert "Reported in session (unattributed):" in user
    assert f"- {low} (minute 18)" in user
    # Nowhere in the whole message does that sentence appear inside quote marks.
    assert f'"{low}' not in user
    assert f'{low}"' not in user

    # And the confirmed one is not demoted by the same pass.
    assert "Client statements captured word for word:" in user
    assert '"I have not been sleeping since the move"' in user


def test_risk_excerpts_are_never_quoted_and_never_claimed_as_the_client(calls):
    """A risk excerpt is grounded in the transcript, never attribution checked.

    Which means the sentence in that list may be the CLINICIAN's. Rendering it
    in quotation marks under a prompt that says quoted text is the client
    speaking is how a therapist's own words become their client's on the record.
    """
    excerpt = "I do not want to be here some days"
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    user = _user_message(calls)

    assert f'"{excerpt}' not in user
    assert f'{excerpt}"' not in user
    assert f"- {excerpt}" in user
    assert "Flagged language from the session (speaker unconfirmed):" in user

    # And the framing scopes its confirmation claim to the statements list
    # rather than to "anything in quotation marks".
    framing = user.split("Session timeline:")[0]
    assert (
        "Only the statements listed under Client statements captured word for "
        "word were confirmed as this client speaking" in framing
    )
    assert "including unattributed statements and flagged language" in framing


def test_only_confirmed_statements_carry_quotation_marks(calls):
    """One structural rule, checked across the whole evidence block at once."""
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    user = _user_message(calls)
    evidence = user.split("Session timeline:")[1].split("SESSION DEBRIEF TRANSCRIPT:")[0]

    quoted = [line for line in evidence.splitlines() if '"' in line]
    assert quoted == ['- "I have not been sleeping since the move" (minute 4)']


def test_no_confirmed_statements_says_so_rather_than_going_quiet(calls):
    """An absent quote list has to read as an instruction, not as a blank.

    A silently missing section invites the model to supply what it thinks is
    missing, which is precisely how an invented quote reaches a clinical record.
    """
    digest = _digest(
        client_statements=[
            {
                "quote": "I am the problem in that house",
                "start_min": 18,
                "attribution_confidence": "low",
            }
        ]
    )
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=digest)
    user = _user_message(calls)

    assert "Client statements captured word for word:" in user
    assert "take no quote from this evidence" in user


def test_digest_schema_adds_conflicts_without_touching_cache(calls):
    """The schema grows a conflicts list. The shared cached object does not."""
    # Warm the cache, then photograph it.
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW)
    cached = extract_mod._SCHEMA_CACHE["DAP"]
    before_schema = copy.deepcopy(extract_mod._SCHEMA_CACHE)
    before_system = copy.deepcopy(extract_mod._SYSTEM_CACHE)

    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    schema = calls[1]["schema"]

    assert schema is not cached
    assert schema["properties"]["evidence_conflicts"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "evidence_conflicts" in schema["required"]
    # Every other property and requirement survived the copy.
    assert set(cached["properties"]) < set(schema["properties"])
    assert set(cached["required"]) < set(schema["required"])

    # The caches are exactly as they were, object identity included.
    assert extract_mod._SCHEMA_CACHE == before_schema
    assert extract_mod._SYSTEM_CACHE == before_system
    assert extract_mod._SCHEMA_CACHE["DAP"] is cached
    assert "evidence_conflicts" not in cached["properties"]
    assert "evidence_conflicts" not in cached["required"]

    # And the next no-digest call is served the pristine object again.
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW)
    assert calls[2]["schema"] is cached


def test_digest_leaves_the_system_prompt_alone(calls):
    """Session evidence is user-message material. The system prompt is untouched."""
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW)
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    assert calls[0]["messages"][0] == calls[1]["messages"][0]


def test_evidence_conflicts_always_present_in_result(monkeypatch):
    """A digest run always returns the list, whatever the model left out."""

    def bare_chat(messages, schema=None, max_tokens=None, temperature=None):
        return {"note": {}, "actions": []}

    monkeypatch.setattr(llm, "chat", bare_chat)
    result = extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    assert result["evidence_conflicts"] == []


def test_evidence_conflicts_survives_when_the_model_supplies_it(monkeypatch):
    def conflicted_chat(messages, schema=None, max_tokens=None, temperature=None):
        return {
            "note": {},
            "actions": [],
            "evidence_conflicts": ["The debrief says Tuesday, the recording says Thursday."],
        }

    monkeypatch.setattr(llm, "chat", conflicted_chat)
    result = extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=_digest())
    assert result["evidence_conflicts"] == [
        "The debrief says Tuesday, the recording says Thursday."
    ]


def test_a_missing_minute_renders_as_unknown_rather_than_invented(calls):
    """The ladder leaves start_min null when it cannot compute it. So does this."""
    digest = _digest(
        segments=[
            {"start_min": None, "end_min": None, "topic": "Whole session", "summary": "s"}
        ],
        client_statements=[
            {"quote": "a real sentence", "start_min": None, "attribution_confidence": "high"}
        ],
    )
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=digest)
    user = _user_message(calls)
    assert "- Minutes ? to ?, Whole session: s" in user
    assert '- "a real sentence"' in user
    assert "minute None" not in user


def test_an_empty_digest_renders_without_raising(calls):
    """A digest whose every list came back empty is still valid evidence."""
    empty = {
        "source_id": "s",
        "duration_min": None,
        "diarized": False,
        "segments": [],
        "themes": [],
        "interventions_observed": [],
        "client_statements": [],
        "risk_language_present": False,
        "risk_excerpts": [],
        "practices_or_homework": [],
        "unresolved_threads": [],
        "notable_shifts": [],
    }
    extract_mod.extract(TRANSCRIPT, CTX, "CBT", NOW, digest=empty)
    user = _user_message(calls)
    assert "SESSION EVIDENCE" in user
    assert "take no quote from this evidence" in user
    assert "Risk-related language" not in user
