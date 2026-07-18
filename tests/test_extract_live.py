"""LIVE end-to-end test of debrief.extract against the local Gemma model.

Requires LM Studio running with gemma-4-12b-it-qat loaded. Marked `live` so a
plain `pytest` run can deselect it; run it directly with:

    .venv/bin/python -m pytest tests/test_extract_live.py -v -s
    .venv/bin/python tests/test_extract_live.py        # standalone script

It sends a realistic CBT + passive-SI debrief and asserts the extraction meets
the clinical + action contract, then prints the full note for human review.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from debrief.extract import extract

EM_DASH = "—"

# Fixed now for a deterministic resolved date (Saturday 2026-07-18 10:00).
NOW = datetime(2026, 7, 18, 10, 0)

MOCK_TRANSCRIPT = (
    "Okay, debrief for Bob. This was session fourteen, in person, about fifty minutes. "
    "We spent most of the time on his workplace stress. He came in describing another "
    "week of feeling completely overwhelmed at the office, and he said, quote, I am "
    "convinced everyone there thinks I am a fraud. Classic mind reading and that "
    "worthlessness core belief we have been tracking. We did some cognitive restructuring "
    "around that automatic thought, walked through a thought record together, and looked "
    "at the evidence for and against the idea that his manager is out to get him. He "
    "actually softened on it by the end and admitted the evidence was thin. I also want "
    "to note, he mentioned some passive suicidal ideation but denied any plan or intent, "
    "said he would never act on it and pointed to his kids as the reason. We did a brief "
    "safety check and he was future oriented by the end. For homework I assigned "
    "continued thought records when the fraud feeling spikes. Book him for next Tuesday "
    "at 3, and send him the thought record worksheet with a quick confirmation email."
)

CLIENT_CTX = {
    "name": "Bob Smith",
    "client_id": "C-0001",
    "framework": "CBT",
    "presenting_concerns": ["workplace stress", "worthlessness"],
    "themes": ["Work-Undermining"],
    "treatment_goals": [
        "Reduce frequency of self-critical automatic thoughts at work",
        "Build evidence-based reappraisal skills for the worthlessness core belief",
    ],
    "risk_flags": ["SI-passive history"],
    "last_session_summary": (
        "Session 13 focused on identifying cognitive distortions tied to his job. "
        "Introduced thought records. Client engaged but skeptical."
    ),
}


def _all_strings(obj):
    """Yield every string value found anywhere in a nested structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)


def run_extract() -> dict:
    return extract(MOCK_TRANSCRIPT, CLIENT_CTX, "CBT", NOW)


def assert_contract(result: dict) -> None:
    note = result["note"]

    # Risk must be caught and fully populated.
    assert note["risk_present"] is True, "risk_present must be true for passive SI"
    risk = note["risk"]
    assert isinstance(risk, dict), "risk object must be populated, not null"
    for field in ("assessed", "ideation", "plan_intent_means", "protective_factors", "interventions_taken"):
        assert field in risk, f"risk missing field {field}"
    assert str(risk["ideation"]).strip(), "risk.ideation must not be empty"
    assert str(risk["plan_intent_means"]).strip(), "risk.plan_intent_means must not be empty"

    # Audit trio present in the note.
    assert note["data"].strip(), "Data section empty"
    assert note["assessment"].strip(), "Assessment section empty"
    assert note["plan"].strip(), "Plan section empty"
    assert len(note["interventions"]) >= 1, "at least one named intervention required"
    assert 1 <= len(note["client_quotes"]) <= 3, "note must carry 1 to 3 client quotes"

    # Exactly two actions, one of each type.
    actions = result["actions"]
    assert len(actions) == 2, f"expected exactly 2 actions, got {len(actions)}: {actions}"
    types = sorted(a["type"] for a in actions)
    assert types == ["draft_client_email", "schedule_followup"], f"wrong action types: {types}"

    followup = next(a for a in actions if a["type"] == "schedule_followup")
    assert "tuesday" in str(followup["datetime_utterance"]).lower(), (
        f"datetime_utterance should mention Tuesday: {followup['datetime_utterance']!r}"
    )
    # The deterministic post-pass must have resolved it to an actual Tuesday.
    assert followup.get("resolved_datetime"), "schedule_followup missing resolved_datetime"
    resolved = datetime.fromisoformat(followup["resolved_datetime"])
    assert resolved.weekday() == 1, f"resolved datetime is not a Tuesday: {resolved}"

    # Suggestions: 2 or 3 options.
    suggestions = result["next_session_suggestions"]
    assert 2 <= len(suggestions) <= 3, f"expected 2 to 3 suggestions, got {len(suggestions)}"

    # No em dash anywhere in any string value.
    for s in _all_strings(result):
        assert EM_DASH not in s, f"em dash found in output string: {s!r}"


@pytest.mark.live
def test_extract_live():
    result = run_extract()
    print("\n===== FULL EXTRACTION RESULT =====")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("===== END =====\n")
    assert_contract(result)


if __name__ == "__main__":
    res = run_extract()
    print(json.dumps(res, indent=2, ensure_ascii=False))
    assert_contract(res)
    print("\nCONTRACT PASSED.")
