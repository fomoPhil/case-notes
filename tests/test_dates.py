"""Deterministic tests for debrief.dates.resolve_utterance.

FIXED fake now: Saturday 2026-07-18 at 10:00 local. Every expectation is
computed by hand against that anchor so the suite never depends on wall-clock.
"""

from datetime import datetime

import pytest

from debrief.dates import resolve_utterance

# Anchor: Saturday, July 18 2026, 10:00. (weekday() == 5)
NOW = datetime(2026, 7, 18, 10, 0)


def test_now_is_saturday():
    assert NOW.weekday() == 5


@pytest.mark.parametrize(
    "utterance, expected",
    [
        # Core cases named in the contract.
        ("next Tuesday at 3", datetime(2026, 7, 21, 15, 0)),
        ("tomorrow at 10", datetime(2026, 7, 19, 10, 0)),
        ("same time next week", datetime(2026, 7, 25, 10, 0)),
        ("two weeks from Friday", datetime(2026, 8, 7, 15, 0)),
        ("Tuesday morning", datetime(2026, 7, 21, 9, 0)),
        ("next week", datetime(2026, 7, 25, 15, 0)),
        # Ambiguous-hour PM rule (1-7 -> PM).
        ("Monday at 4", datetime(2026, 7, 20, 16, 0)),
        ("Friday at 3", datetime(2026, 7, 24, 15, 0)),
        # Ambiguous-hour AM rule (8-11 -> AM).
        ("Monday at 9", datetime(2026, 7, 20, 9, 0)),
        ("Wednesday at 11", datetime(2026, 7, 22, 11, 0)),
        # 12 -> noon.
        ("Friday at 12", datetime(2026, 7, 24, 12, 0)),
        # Explicit am/pm overrides the default rule.
        ("Monday at 3pm", datetime(2026, 7, 20, 15, 0)),
        ("Monday at 3 pm", datetime(2026, 7, 20, 15, 0)),
        ("Tuesday at 9 am", datetime(2026, 7, 21, 9, 0)),
        # Colon minutes.
        ("Wednesday at 10:30", datetime(2026, 7, 22, 10, 30)),
        ("Thursday at 2:15", datetime(2026, 7, 23, 14, 15)),
        # Named periods.
        ("tomorrow morning", datetime(2026, 7, 19, 9, 0)),
        ("Friday afternoon", datetime(2026, 7, 24, 15, 0)),
        ("Tuesday evening", datetime(2026, 7, 21, 18, 0)),
        ("tonight", datetime(2026, 7, 18, 18, 0)),
        # "next <weekday>" is the same as a bare weekday: strictly after today.
        ("next Tuesday", datetime(2026, 7, 21, 15, 0)),
        ("next Saturday", datetime(2026, 7, 25, 15, 0)),
        ("Saturday", datetime(2026, 7, 25, 15, 0)),
        # Relative day/week counts.
        ("in 3 days", datetime(2026, 7, 21, 15, 0)),
        ("in two weeks", datetime(2026, 8, 1, 15, 0)),
        ("in 10 days at 2", datetime(2026, 7, 28, 14, 0)),
        # "N weeks from <weekday>".
        ("three weeks from Monday", datetime(2026, 8, 10, 15, 0)),
        ("a week from Friday", datetime(2026, 7, 31, 15, 0)),
        ("two weeks from Friday morning", datetime(2026, 8, 7, 9, 0)),
        # "same time" with a weekday reference.
        ("same time next Monday", datetime(2026, 7, 20, 10, 0)),
        # Abbreviations.
        ("tue at 3", datetime(2026, 7, 21, 15, 0)),
        ("fri at 4pm", datetime(2026, 7, 24, 16, 0)),
    ],
)
def test_resolve(utterance, expected):
    got = resolve_utterance(utterance, NOW)
    assert got == expected, f"{utterance!r} -> {got}, expected {expected}"


@pytest.mark.parametrize(
    "utterance",
    [
        "",
        "   ",
        "whenever works for you",
        "at 3",              # a time with no day is not resolvable
        "sometime soon",
        "the usual",
        None,
    ],
)
def test_unparseable_returns_none(utterance):
    assert resolve_utterance(utterance, NOW) is None


def test_next_weekday_is_strictly_after_today():
    # Today is Saturday; "Saturday" must jump a full week, not return today.
    got = resolve_utterance("Saturday", NOW)
    assert got.date() == datetime(2026, 7, 25).date()
    assert got != NOW


def test_tzinfo_is_carried():
    from datetime import timezone
    aware_now = NOW.replace(tzinfo=timezone.utc)
    got = resolve_utterance("next Tuesday at 3", aware_now)
    assert got.tzinfo == timezone.utc
