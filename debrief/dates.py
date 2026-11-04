"""Deterministic relative-date resolution.

The LLM never does date arithmetic. It emits the utterance it heard
("next Tuesday at 3") and this module turns that into an absolute datetime,
resolved against a caller-supplied `now`. The approval checklist then shows the
absolute datetime so the therapist can catch any error before anything runs.

Design: rules + small regexes (dateutil is available but its fuzzy parser is
unreliable on relative phrases, so we resolve explicitly). Unparseable -> None.

Conventions (from the architecture contract):
  - A weekday resolves to the next occurrence strictly AFTER today, never today.
  - "same time next week" = now + 7 days at the same clock time.
  - Bare "next week" = same weekday + 7 days at the 15:00 default.
  - Named periods: morning 9:00, afternoon 15:00, evening/night 18:00, noon 12:00.
  - An ambiguous hour 1-7 with no am/pm is PM (therapists book afternoons);
    8-11 is AM; 12 is noon.
  - When no time is stated at all, default to 15:00.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

# Default appointment time when the utterance names a day but no clock time.
_DEFAULT_HOUR = 15
_DEFAULT_MINUTE = 0

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "thur": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_PERIODS = {
    "morning": 9,
    "afternoon": 15,
    "noon": 12,
    "midday": 12,
    "evening": 18,
    "night": 18,
    "midnight": 0,
}

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}

# Longest weekday keys first so "tues" wins over "tue".
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b"
)


_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# A month name only counts next to a day number, so "we may meet" is not May.
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b")
# A bare day of the month needs its ordinal suffix: "the 14th", "Friday the 13th".
_ORDINAL_DAY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b")


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _explicit_date(text: str, now: datetime) -> datetime | None:
    """A named calendar date, as the next such date strictly after today."""
    month = day = None
    m = _MONTH_DAY_RE.search(text)
    if m:
        month, day = _MONTHS[m.group(1)], int(m.group(2))
    else:
        m = _DAY_MONTH_RE.search(text)
        if m:
            day, month = int(m.group(1)), _MONTHS[m.group(2)]
    if month is not None:
        for year in (now.year, now.year + 1):
            cand = _safe_date(year, month, day)
            if cand and cand.date() > now.date():
                return cand
        return None
    m = _ORDINAL_DAY_RE.search(text)
    if m:
        day = int(m.group(1))
        year, month = now.year, now.month
        for _ in range(13):
            cand = _safe_date(year, month, day)
            if cand and cand.date() > now.date():
                return cand
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return None


def _to_int(tok: str) -> int | None:
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def _apply_ampm(hour: int, ampm: str) -> int:
    if ampm == "pm":
        return hour if hour == 12 else hour + 12
    # am
    return 0 if hour == 12 else hour


def _default_ampm(hour: int) -> int:
    """Resolve an am/pm-less hour. 1-7 -> PM, 8-11 -> AM, 12 -> noon."""
    if 1 <= hour <= 7:
        return hour + 12
    return hour  # 8-12 stay as-is; 13-23 already 24h


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Find an explicit clock time. Returns (hour, minute) or None."""
    # With am/pm, e.g. "3pm", "3:30 pm", "at 10 am".
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        return _apply_ampm(hour, m.group(3)), minute
    # Colon time without am/pm, e.g. "3:30", "18:00".
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m:
        return _default_ampm(int(m.group(1))), int(m.group(2))
    # "half past three", "quarter past 2", "quarter to 4".
    m = re.search(r"\b(half|quarter)\s+(past|to)\s+(\w+)\b", text)
    if m:
        hour = _to_int(m.group(3))
        if hour is not None and 1 <= hour <= 12:
            if m.group(2) == "past":
                return _default_ampm(hour), 30 if m.group(1) == "half" else 15
            if m.group(1) == "quarter":
                return _default_ampm(hour - 1 or 12), 45
    # "at 3" style, bare hour after "at".
    m = re.search(r"\bat\s+(\d{1,2})\b", text)
    if m:
        return _default_ampm(int(m.group(1))), 0
    return None


def _parse_period(text: str) -> tuple[int, int] | None:
    for word, hour in _PERIODS.items():
        if re.search(rf"\b{word}\b", text):
            return hour, 0
    return None


def _next_weekday(now: datetime, target_idx: int) -> datetime:
    """Date of the next given weekday strictly after today."""
    days_ahead = (target_idx - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


def _combine(base_dt: datetime, hour: int, minute: int, now: datetime) -> datetime:
    return datetime.combine(
        base_dt.date(), time(hour=hour, minute=minute), tzinfo=now.tzinfo
    )


def resolve_utterance(utterance: str, now: datetime) -> datetime | None:
    """Resolve a spoken relative date/time against `now`.

    Returns an absolute datetime (carrying now's tzinfo) or None if the phrase
    has no recognizable date component.
    """
    if not utterance or not utterance.strip():
        return None

    # Normalize: lowercase, collapse "p.m." -> "pm", squash whitespace.
    text = utterance.strip().lower().replace(".", "")
    text = re.sub(r"\s+", " ", text)

    clock = _parse_clock(text)
    period = _parse_period(text)

    def resolve_time() -> tuple[int, int]:
        if clock:
            return clock
        if period:
            return period
        return _DEFAULT_HOUR, _DEFAULT_MINUTE

    # 0. A named date ("July 30th", "the 14th", "Wednesday the 11th") wins over
    # a weekday, because the number is the more specific thing said.
    named = _explicit_date(text, now)
    if named is not None:
        if "same time" in text:
            return _combine(named, now.hour, now.minute, now)
        h, mi = resolve_time()
        return _combine(named, h, mi, now)

    # "the week after next" and "a fortnight" are both two weeks out.
    if "week after next" in text or "fortnight" in text:
        base = now + timedelta(days=14)
        if "same time" in text:
            return _combine(base, now.hour, now.minute, now)
        h, mi = resolve_time()
        return _combine(base, h, mi, now)

    # 1. "same time ..." keeps now's clock time.
    if "same time" in text:
        wd = _WEEKDAY_RE.search(text)
        m = re.search(r"\bin\s+(\w+)\s+(day|days|week|weeks)\b", text)
        count = _to_int(m.group(1)) if m else None
        if wd:
            base = _next_weekday(now, _WEEKDAYS[wd.group(1)])
        elif count is not None:
            unit = timedelta(weeks=1) if m.group(2).startswith("week") else timedelta(days=1)
            base = now + count * unit
        else:
            base = now + timedelta(days=7)
        return _combine(base, now.hour, now.minute, now)

    # 2. "N weeks from <weekday|today|now>".
    m = re.search(r"\b(\w+)\s+weeks?\s+from\s+(\w+)\b", text)
    if m:
        count = _to_int(m.group(1))
        ref = m.group(2)
        if count is not None:
            if ref in _WEEKDAYS:
                base = _next_weekday(now, _WEEKDAYS[ref]) + timedelta(weeks=count)
                h, mi = resolve_time()
                return _combine(base, h, mi, now)
            if ref == "tomorrow":
                base = now + timedelta(days=1, weeks=count)
                h, mi = resolve_time()
                return _combine(base, h, mi, now)
            if ref in ("today", "now"):
                base = now + timedelta(weeks=count)
                h, mi = resolve_time()
                return _combine(base, h, mi, now)

    # 3. "in N days" / "in N weeks".
    m = re.search(r"\bin\s+(\w+)\s+(day|days|week|weeks)\b", text)
    if m:
        count = _to_int(m.group(1))
        if count is not None:
            delta = timedelta(weeks=count) if m.group(2).startswith("week") else timedelta(days=count)
            base = now + delta
            h, mi = resolve_time()
            return _combine(base, h, mi, now)

    # 4. tomorrow / today / tonight.
    if "tomorrow" in text:
        base = now + timedelta(days=1)
        h, mi = resolve_time()
        return _combine(base, h, mi, now)
    if "tonight" in text or "today" in text:
        base = now
        # "tonight" implies evening unless a clock time is given.
        if not clock and not period and "tonight" in text:
            h, mi = 18, 0
        else:
            h, mi = resolve_time()
        return _combine(base, h, mi, now)

    # 5. Bare "next week" (no weekday named): same weekday + 7 days.
    if "next week" in text and not _WEEKDAY_RE.search(text):
        base = now + timedelta(days=7)
        h, mi = resolve_time()
        return _combine(base, h, mi, now)

    # 6. A weekday, optionally prefixed "next".
    wd = _WEEKDAY_RE.search(text)
    if wd:
        base = _next_weekday(now, _WEEKDAYS[wd.group(1)])
        h, mi = resolve_time()
        return _combine(base, h, mi, now)

    return None
