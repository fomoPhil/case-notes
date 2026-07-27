"""Distil one session source into a digest, so the note is written from evidence.

A 50 minute session is 6,000 to 9,000 spoken words. Handing that to a 12b local
model to write a note from is the worst of both worlds: the signal drowns, and
every later call that wants session context pays the full price again. So the
material is distilled once and the distillate is what gets read.

Three rungs, each written to disk the moment it is finished:

    0   transcript-raw.md   what the recorder heard, the archive
    1   transcript.md       the same words with clinical vocabulary corrected
    2   digest.json         the structured distillate, plus digest.md beside it

The rungs are their own checkpoint. build() asks the disk which rungs exist and
builds the ones that do not, so a run that dies partway through resumes by
carrying on rather than starting over. Nothing in this module trusts its own
memory of what it just wrote: every stage re-reads the file it needs.

Sources that arrive already interpreted (a meeting tool's write up, the
clinician's own notes) enter at rung 2. There is no transcript of them to build,
because nobody said those words.

## Who owns what

The model reads text and reports what it sees. Code owns every number and every
rule, and overwrites the model afterwards without asking:

    source_id, duration_min, diarized      copied from the Source
    segment start and end minutes          computed from rung 0 timestamps, or
                                           proportionally from a known duration,
                                           or left null. The map schema does not
                                           even offer the model a field to put a
                                           timestamp in.
    every quote                            must be found in the transcript, after
                                           whitespace, curly quote, and invisible
                                           character normalisation, or it is
                                           dropped and counted
    quotes at all                          only from a word for word source
    attribution confidence                 "low" whenever speakers were not
                                           separated, and "low" for any value
                                           outside the two the schema allows

Those four rules are spec section 6 rules 2 and 3 and the grounding requirement.
They are enforced here, in code, after the model returns. A prompt is guidance.
This is the part that has to hold.

Risk language is flagged for a human to look at. It is never assessed, never
scored, and never turned into a determination. That stays with the clinician.

Nothing here reads or writes the psychotherapy-notes folder that the vault keeps
walled off from every model and every feature.

No em dashes anywhere.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from . import llm, recstore, sources, stt
from .sources import Source, SourceError

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_MAP_PROMPT_PATH = _PROMPT_DIR / "digest_map.md"
_REDUCE_PROMPT_PATH = _PROMPT_DIR / "digest_reduce.md"


class LadderError(RuntimeError):
    """The ladder could not produce a digest. The message is safe to show."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# What one window of transcript yields. No start or end field anywhere: a model
# that is never asked for a timestamp can never invent one.
_MAP_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic": {"type": "string"},
        "summary": {"type": "string"},
        "quotes": {"type": "array", "items": {"type": "string"}},
        "interventions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "client_response": {"type": "string"},
                },
                "required": ["name", "client_response"],
            },
        },
        "risk_language": {"type": "array", "items": {"type": "string"}},
        "practices_or_homework": {"type": "array", "items": {"type": "string"}},
        "unresolved_threads": {"type": "array", "items": {"type": "string"}},
        "notable_shifts": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "topic",
        "summary",
        "quotes",
        "interventions",
        "risk_language",
        "practices_or_homework",
        "unresolved_threads",
        "notable_shifts",
    ],
}

# The digest, and the schema the reduce call is constrained to. The model is
# allowed to fill in the code-owned fields; the code overwrites them afterwards
# regardless of what it said, so the shape stays one schema rather than two that
# can drift apart.
DIGEST_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_id": {"type": "string"},
        "duration_min": {"type": ["integer", "null"]},
        "diarized": {"type": "boolean"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_min": {"type": ["integer", "null"]},
                    "end_min": {"type": ["integer", "null"]},
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["start_min", "end_min", "topic", "summary"],
            },
        },
        "themes": {"type": "array", "items": {"type": "string"}},
        "interventions_observed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "client_response": {"type": "string"},
                    "start_min": {"type": ["integer", "null"]},
                },
                "required": ["name", "client_response", "start_min"],
            },
        },
        "client_statements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "quote": {"type": "string"},
                    "start_min": {"type": ["integer", "null"]},
                    "attribution_confidence": {"type": "string", "enum": ["high", "low"]},
                },
                "required": ["quote", "start_min", "attribution_confidence"],
            },
        },
        "risk_language_present": {"type": "boolean"},
        "risk_excerpts": {"type": "array", "items": {"type": "string"}},
        "practices_or_homework": {"type": "array", "items": {"type": "string"}},
        "unresolved_threads": {"type": "array", "items": {"type": "string"}},
        "notable_shifts": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "source_id",
        "duration_min",
        "diarized",
        "segments",
        "themes",
        "interventions_observed",
        "client_statements",
        "risk_language_present",
        "risk_excerpts",
        "practices_or_homework",
        "unresolved_threads",
        "notable_shifts",
    ],
}

# The two attribution values the schema allows. Anything else becomes the safer
# one: an unattributed observation is recoverable, a misattributed quote is not.
_CONFIDENCE_VALUES = frozenset({"high", "low"})
_SAFE_CONFIDENCE = "low"

# What a failed map window leaves behind, so the gap is visible rather than
# silently absent from the timeline.
_FAILED_TOPIC = "(window failed)"
_FAILED_SUMMARY = "This part of the session could not be summarized."


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------

# Roughly 1,500 words per map window. Denominated in estimated tokens for the
# same reason stt.py is: a language written without spaces has no word count
# worth budgeting against.
_MAP_WINDOW_WORDS = 1500
_MAP_WINDOW_TOKENS = _MAP_WINDOW_WORDS * stt._TOKENS_PER_WORD

# The reduce call reads every window's output at once, so the window count is
# what bounds it. 24 windows at roughly 600 tokens each is about 15k tokens into
# a 64k context. A longer transcript widens its windows rather than adding more.
_MAX_MAP_WINDOWS = 24

# Output caps, both set well above the expected size rather than close to it.
# A window report runs about 600 tokens and a digest 1,200 to 2,000, but a reply
# cut off at the ceiling is not valid JSON, and the cost of that is silent: the
# window degrades to a placeholder, or the whole digest fails. Headroom is the
# cheaper mistake.
_MAP_MAX_TOKENS = 1400
_REDUCE_MAX_TOKENS = 4000

# Ceilings on what one window may contribute to the reduce input, and on the
# lists the digest carries, so neither can grow without bound.
_MAX_QUOTES_PER_WINDOW = 6
_MAX_LIST_ITEMS = 20


def _pack(sentences: list[str], budget: int) -> list[tuple[int, int]]:
    """Pack sentences into disjoint windows of roughly `budget` tokens.

    Half open index ranges that tile the whole list: contiguous, nothing
    dropped, nothing counted twice. Every window holds at least one sentence, so
    a single oversized sentence gets a window of its own rather than an empty
    one. Same shape as the packer in stt.py, with the budget passed in.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    used = 0
    for index, sentence in enumerate(sentences):
        cost = stt._estimate_tokens(sentence)
        if used and used + cost > budget:
            windows.append((start, index))
            start = index
            used = 0
        used += cost
    if start < len(sentences):
        windows.append((start, len(sentences)))
    return windows


def _window_budget(sentences: list[str]) -> int:
    """Tokens per window: the default, widened when a transcript is very long.

    Widening rather than adding windows is what keeps the reduce call bounded.
    """
    total = sum(stt._estimate_tokens(sentence) for sentence in sentences)
    fair_share = math.ceil(total / _MAX_MAP_WINDOWS) if total else 0
    return max(_MAP_WINDOW_TOKENS, fair_share)


def _cap(windows: list[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    """Merge adjacent windows until there are at most `limit` of them.

    A widened budget gets the count close, but sentence boundaries mean it can
    still land a window or two over. This is the hard guarantee, and it merges
    rather than truncates: a bounded reduce call must never be bought by
    dropping the end of a session.
    """
    if len(windows) <= limit:
        return windows
    per_group = math.ceil(len(windows) / limit)
    return [
        (group[0][0], group[-1][1])
        for group in (
            windows[index : index + per_group] for index in range(0, len(windows), per_group)
        )
    ]


def _sentence_offsets(sentences: list[str]) -> tuple[list[int], int]:
    """Character offset of each sentence in " ".join(sentences), plus the total."""
    offsets: list[int] = []
    position = 0
    for sentence in sentences:
        offsets.append(position)
        position += len(sentence) + 1
    return offsets, max(position - 1, 1)


# ---------------------------------------------------------------------------
# Timestamps, which only code is allowed to produce
# ---------------------------------------------------------------------------


def _minute_lookup(
    total_chars: int,
    timed_lines: list[tuple[float, str]] | None,
    duration_min: int | None,
) -> Callable[[int], int | None]:
    """Return a function mapping a character position to a minute, or to None.

    Three tiers, best first. When rung 0 carried timestamps, a running position
    over those lines says which one a given point falls in and its stamp is used.
    Failing that, a known duration gives a proportional estimate. Failing both,
    every position is None, because an invented minute in a clinical record is
    worse than an absent one.
    """
    marks: list[tuple[float, float]] = []
    if timed_lines:
        lengths = [max(len(text or ""), 1) for _, text in timed_lines]
        span = sum(lengths)
        running = 0
        for (seconds, _), length in zip(timed_lines, lengths):
            marks.append((running / span, float(seconds)))
            running += length

    def _fraction(position: int) -> float:
        if total_chars <= 0:
            return 0.0
        return min(max(position / total_chars, 0.0), 1.0)

    if marks:

        def _at(position: int) -> int | None:
            fraction = _fraction(position)
            seconds = marks[0][1]
            for mark_fraction, mark_seconds in marks:
                if mark_fraction > fraction:
                    break
                seconds = mark_seconds
            return int(seconds // 60)

        return _at

    if duration_min:

        def _at(position: int) -> int | None:
            return int(_fraction(position) * int(duration_min))

        return _at

    return lambda position: None


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

# Characters that look like quotation marks or dashes but are not the ones the
# transcript holds. Normalised so retyping a real quote with a curly apostrophe
# does not lose it, and so a fabricated one cannot hide its difference in a
# character that renders the same. Written as escapes: these are exactly the
# characters that are invisible or confusable in source, which is the whole
# reason they are listed here.
_QUOTE_MAP = {ord(ch): "'" for ch in "\u2018\u2019\u201a\u201b\u02bc\u02b9\u2032\u00b4\u0060"}
_QUOTE_MAP.update({ord(ch): '"' for ch in "\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb"})
_DASH_MAP = {ord(ch): "-" for ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2043\u2212\ufe58\ufe63\uff0d"}

# Invisible characters. None of them is whitespace by str.isspace, so each one
# would otherwise survive normalisation and break an honest substring match, or
# hide a difference in a dishonest one.
_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f")


def _normalize_with_index(text: str) -> tuple[str, list[int]]:
    """Normalise text for comparison, keeping a map back to the original.

    Compatibility normalisation folds the exotic spaces, invisible characters are
    dropped, curly quotes and every width of dash are flattened, runs of
    whitespace collapse to one space, and the result is casefolded. index[i] is
    the position in `text` that produced the normalised character at i, so a
    match found in the normalised string can be located in the real one.
    """
    chars: list[str] = []
    index: list[int] = []
    space_pending = False
    for position, raw in enumerate(text or ""):
        if raw in _INVISIBLE:
            continue
        piece = unicodedata.normalize("NFKC", raw).translate(_QUOTE_MAP).translate(_DASH_MAP)
        for char in piece:
            if char.isspace():
                space_pending = bool(chars)
                continue
            if space_pending:
                chars.append(" ")
                index.append(position)
                space_pending = False
            for folded in char.casefold():
                chars.append(folded)
                index.append(position)
    return "".join(chars), index


def _normalize(text: str) -> str:
    """The normalised form of a candidate quote, without the position map."""
    return _normalize_with_index(text)[0]


class _Grounding:
    """The transcript, in the one form quotes are checked against.

    A quote counts as grounded only when it appears in here, which is the whole
    point: the digest may report what was said, never what would have fitted.
    """

    def __init__(self, text: str) -> None:
        self.haystack, self._index = _normalize_with_index(text)

    def locate(self, quote: str) -> int | None:
        """Character position of a quote in the original text, or None.

        None means the words are not in the transcript, whatever the model said.
        """
        needle = _normalize(quote).strip()
        if not needle:
            return None
        found = self.haystack.find(needle)
        if found < 0:
            return None
        return self._index[found] if found < len(self._index) else 0


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _report(
    progress: Callable[[str, int, int], None] | None, stage: str, done: int, total: int
) -> None:
    """Tell the caller which stage is running and how far through it is."""
    if progress is not None:
        progress(stage, int(done), int(total))


def _boundary(between_rungs: Callable[[], None] | None) -> None:
    """A point where it is safe to hand the model over to somebody else.

    The ladder just calls it. Whether it blocks, and for how long, is the job
    queue's business.
    """
    if between_rungs is not None:
        between_rungs()


def _string_list(value, limit: int = _MAX_LIST_ITEMS) -> list[str]:
    """Coerce whatever the model returned into a clean list of short strings."""
    if not isinstance(value, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _prompt(path: Path) -> str:
    """Read a prompt file at call time, so an edit needs no restart."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LadderError(f"the digest prompt at {path.name} could not be read: {exc}")


def _context_block(client_ctx: dict | None) -> str:
    """Render whatever the vault knows about this client as readable lines."""
    lines: list[str] = []
    for key, value in (client_ctx or {}).items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, default=str)
        lines.append(f"{key.replace('_', ' ').title()}: {value}")
    return "\n".join(lines) if lines else "(no prior context on file)"


# ---------------------------------------------------------------------------
# The map and reduce calls
# ---------------------------------------------------------------------------


def _run_map(window_text: str, ordinal: int, total: int, context: str) -> dict | None:
    """Summarise one window. None when the call failed or answered with nothing.

    One bad window costs its segment, not the digest. The caller turns a None
    into a visible placeholder in the timeline.
    """
    user = (
        f"CLIENT CONTEXT:\n{context}\n\n"
        f"SESSION TRANSCRIPT, PART {ordinal} OF {total}:\n{window_text}\n\n"
        "Produce the JSON object now."
    )
    try:
        result = llm.chat(
            [
                {"role": "system", "content": _prompt(_MAP_PROMPT_PATH)},
                {"role": "user", "content": user},
            ],
            schema=_MAP_SCHEMA,
            max_tokens=_MAP_MAX_TOKENS,
            temperature=0.2,
        )
    except LadderError:
        raise
    except Exception:  # noqa: BLE001 - one bad window must not fail the digest
        return None
    return result if isinstance(result, dict) else None


def _map_payload(maps: list[dict]) -> str:
    """Serialise the map outputs for the reduce call, bounded and in order."""
    blocks: list[dict] = []
    for ordinal, entry in enumerate(maps, start=1):
        result = entry["result"]
        block: dict = {
            "window": ordinal,
            "start_min": entry["start_min"],
            "end_min": entry["end_min"],
        }
        if result is None:
            block["failed"] = True
        else:
            block.update(
                {
                    "topic": str(result.get("topic") or "").strip(),
                    "summary": str(result.get("summary") or "").strip(),
                    "quotes": _string_list(result.get("quotes"), _MAX_QUOTES_PER_WINDOW),
                    "interventions": [
                        {
                            "name": str((item or {}).get("name") or "").strip(),
                            "client_response": str(
                                (item or {}).get("client_response") or ""
                            ).strip(),
                        }
                        for item in (result.get("interventions") or [])
                        if isinstance(item, dict) and str(item.get("name") or "").strip()
                    ],
                    "risk_language": _string_list(result.get("risk_language")),
                    "practices_or_homework": _string_list(result.get("practices_or_homework")),
                    "unresolved_threads": _string_list(result.get("unresolved_threads")),
                    "notable_shifts": _string_list(result.get("notable_shifts")),
                }
            )
        blocks.append(block)
    return json.dumps(blocks, ensure_ascii=False, indent=2, default=str)


def _run_reduce(maps: list[dict], source: Source, context: str) -> dict:
    """One call over every window's output. Raises LadderError when it fails.

    The digest is the deliverable, so unlike a map window there is nothing to
    degrade to here. The job surfaces the failure and the rungs already built
    stay on disk.
    """
    user = (
        f"CLIENT CONTEXT:\n{context}\n\n"
        f"SESSION LENGTH: {source.duration_min if source.duration_min else 'not known'}\n"
        f"SPEAKERS SEPARATED: {'yes' if source.diarized else 'no'}\n\n"
        "SESSION WINDOW REPORTS, in order. The start_min and end_min values were "
        "computed by the app from the recording itself, so copy them, never "
        "adjust them, and never write a timestamp of your own:\n"
        f"{_map_payload(maps)}\n\n"
        "Produce the JSON object now."
    )
    try:
        result = llm.chat(
            [
                {"role": "system", "content": _prompt(_REDUCE_PROMPT_PATH)},
                {"role": "user", "content": user},
            ],
            schema=DIGEST_SCHEMA,
            max_tokens=_REDUCE_MAX_TOKENS,
            temperature=0.2,
        )
    except LadderError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as the module's own error
        raise LadderError(f"the session digest could not be assembled: {exc}") from exc
    if not isinstance(result, dict):
        raise LadderError("the session digest could not be assembled: the model returned no object.")
    return result


# ---------------------------------------------------------------------------
# The digest itself
# ---------------------------------------------------------------------------


def build_digest(
    corrected_text: str,
    source: Source,
    client_ctx: dict,
    timed_lines: list[tuple[float, str]] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Map-reduce a transcript into the digest. No disk io.

    Each window of roughly 1,500 words gets one constrained call reporting what
    it saw. One further call reads every window's report and writes the digest.
    Code then overwrites every field it owns, drops every quote it cannot find in
    the transcript, and applies the provenance rules. progress is reported under
    the "digesting" stage, once per window plus once for the reduce.

    Raises LadderError when there is nothing to distil or the reduce call fails.
    """
    text = (corrected_text or "").strip()
    if not text:
        raise LadderError("there is nothing to distil: this source has no text.")

    sentences = stt._split_sentences(text)
    if not sentences:
        raise LadderError("there is nothing to distil: this source has no text.")

    # One reference string for the whole build: the sentences rejoined. Window
    # spans, quote positions, and the grounding haystack are all measured
    # against this exact string, so a quote's minute and its segment's minute
    # can never come from two different rulers.
    joined = " ".join(sentences)
    offsets, total_chars = _sentence_offsets(sentences)
    windows = _cap(_pack(sentences, _window_budget(sentences)), _MAX_MAP_WINDOWS)
    minute_at = _minute_lookup(total_chars, timed_lines, source.duration_min)

    total_steps = len(windows) + 1
    _report(progress, "digesting", 0, total_steps)

    context = _context_block(client_ctx)
    maps: list[dict] = []
    for done, (start, end) in enumerate(windows, start=1):
        window_text = " ".join(sentences[start:end])
        end_position = offsets[end - 1] + len(sentences[end - 1])
        maps.append(
            {
                "start_char": offsets[start],
                "end_char": end_position,
                "start_min": minute_at(offsets[start]),
                "end_min": minute_at(end_position),
                "result": _run_map(window_text, done, len(windows), context),
            }
        )
        _report(progress, "digesting", done, total_steps)

    reduced = _run_reduce(maps, source, context)
    _report(progress, "digesting", total_steps, total_steps)

    return _assemble(reduced, maps, source, joined)


def _segments_from_maps(maps: list[dict]) -> list[dict]:
    """The timeline. Topic and summary from the model, minutes from code."""
    segments: list[dict] = []
    for entry in maps:
        result = entry["result"]
        if result is None:
            topic, summary = _FAILED_TOPIC, _FAILED_SUMMARY
        else:
            topic = str(result.get("topic") or "").strip() or "(no topic)"
            summary = str(result.get("summary") or "").strip()
        segments.append(
            {
                "start_min": entry["start_min"],
                "end_min": entry["end_min"],
                "topic": topic,
                "summary": summary,
            }
        )
    return segments


def _intervention_minutes(maps: list[dict]) -> dict[str, int | None]:
    """When each named intervention was first reported, by the window it was in.

    The reduce call consolidates interventions across windows but has no way to
    know where in the session they happened, so the minute comes from here.
    """
    minutes: dict[str, int | None] = {}
    for entry in maps:
        result = entry["result"]
        if not isinstance(result, dict):
            continue
        for item in result.get("interventions") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and name.casefold() not in minutes:
                minutes[name.casefold()] = entry["start_min"]
    return minutes


def _assemble(reduced: dict, maps: list[dict], source: Source, text: str) -> dict:
    """Turn the reduce output into the digest, enforcing every rule in code.

    Nothing the model said about a code-owned field survives this function.
    """
    grounding = _Grounding(text)
    dropped = 0

    # Rule 2. A source that is not word for word may never produce an attributed
    # quote or an excerpt, because a paraphrase filed as somebody's words stays
    # in the record forever. The risk flag survives; the quotes do not.
    if not source.verbatim:
        statements: list[dict] = []
        excerpts: list[str] = []
    else:
        statements, dropped_statements = _ground_statements(
            reduced.get("client_statements"), grounding, maps, source
        )
        candidates = _string_list(reduced.get("risk_excerpts"))
        if not candidates:
            candidates = _string_list(
                [
                    item
                    for entry in maps
                    if isinstance(entry["result"], dict)
                    for item in (entry["result"].get("risk_language") or [])
                ]
            )
        excerpts, dropped_excerpts = _ground_excerpts(candidates, grounding)
        dropped = dropped_statements + dropped_excerpts

    map_risk = any(
        bool(_string_list((entry["result"] or {}).get("risk_language")))
        for entry in maps
        if isinstance(entry["result"], dict)
    )

    return {
        "source_id": source.id,
        "duration_min": source.duration_min,
        "diarized": bool(source.diarized),
        "segments": _segments_from_maps(maps),
        "themes": _string_list(reduced.get("themes")),
        "interventions_observed": _interventions(reduced.get("interventions_observed"), maps),
        "client_statements": statements,
        "risk_language_present": bool(reduced.get("risk_language_present")) or map_risk,
        "risk_excerpts": excerpts,
        "practices_or_homework": _string_list(reduced.get("practices_or_homework")),
        "unresolved_threads": _string_list(reduced.get("unresolved_threads")),
        "notable_shifts": _string_list(reduced.get("notable_shifts")),
        "_dropped_ungrounded": dropped,
    }


def _interventions(value, maps: list[dict]) -> list[dict]:
    """The consolidated interventions, with code-owned minutes."""
    minutes = _intervention_minutes(maps)
    out: list[dict] = []
    seen: set[str] = set()
    for item in value if isinstance(value, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(
            {
                "name": name,
                "client_response": str(item.get("client_response") or "").strip(),
                "start_min": minutes.get(name.casefold()),
            }
        )
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return out


def _quote_minute(position: int, maps: list[dict]) -> int | None:
    """The minute a character position falls in: the window that contains it.

    Windows carry the character span they were cut from, so this is a lookup
    rather than an estimate. A position past the last window (which only happens
    on an empty map list) has no minute at all.
    """
    for entry in maps:
        if entry.get("start_char", 0) <= position < entry.get("end_char", 0):
            return entry["start_min"]
    return maps[-1]["start_min"] if maps else None


def _ground_statements(
    value, grounding: _Grounding, maps: list[dict], source: Source
) -> tuple[list[dict], int]:
    """Keep only the quotes that are actually in the transcript.

    A quote the model composed rather than copied is dropped and counted, so the
    UI can say how many rather than quietly showing fewer. Rule 3 applies here
    too: with speakers not separated, every attribution is low confidence,
    because guessing who spoke is the one mistake that cannot be walked back.
    """
    kept: list[dict] = []
    dropped = 0
    seen: set[str] = set()
    for item in value if isinstance(value, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        key = _normalize(quote)
        if key in seen:
            continue
        position = grounding.locate(quote)
        if position is None:
            dropped += 1
            continue
        seen.add(key)
        confidence = str(item.get("attribution_confidence") or "").strip().casefold()
        if confidence not in _CONFIDENCE_VALUES or not source.diarized:
            confidence = _SAFE_CONFIDENCE
        kept.append(
            {
                "quote": quote,
                "start_min": _quote_minute(position, maps),
                "attribution_confidence": confidence,
            }
        )
        if len(kept) >= _MAX_LIST_ITEMS:
            break
    return kept, dropped


def _ground_excerpts(candidates: list[str], grounding: _Grounding) -> tuple[list[str], int]:
    """Keep only the risk excerpts that are actually in the transcript."""
    kept: list[str] = []
    dropped = 0
    for candidate in candidates:
        if grounding.locate(candidate) is None:
            dropped += 1
            continue
        kept.append(candidate)
    return kept, dropped


# ---------------------------------------------------------------------------
# Rung 0 text
# ---------------------------------------------------------------------------

# The stamp render_turns writes at the head of a timed line: [MM:SS] or [H:MM:SS].
_STAMP_RE = re.compile(r"^\[(\d{1,3}:\d{2}(?::\d{2})?)\]\s*")


def _read_rung0(raw: str) -> tuple[list[tuple[float, str]], str]:
    """Split the rung 0 archive into its timestamps and its plain words.

    Correction wants the words on their own: a stamp or a speaker label in the
    stream is noise the model would try to correct. The stamps are kept here
    instead, so the digest can still say when something was said.
    """
    timed: list[tuple[float, str]] = []
    plain: list[str] = []
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        seconds = None
        match = _STAMP_RE.match(stripped)
        if match:
            seconds = sources._seconds(match.group(1))
            stripped = stripped[match.end() :].strip()
        _, body = sources._split_speaker(stripped)
        body = body.strip()
        if not body:
            continue
        plain.append(body)
        if seconds is not None:
            timed.append((seconds, body))
    return timed, "\n".join(plain)


def _source_text(client_id: str, source_id: str) -> str | None:
    """The document text of a source that entered at rung 2, or None."""
    try:
        path = recstore.source_dir(client_id, source_id) / recstore._SOURCE_TEXT
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reading and writing the digest
# ---------------------------------------------------------------------------


def load_digest(client_id: str, source_id: str) -> dict | None:
    """The stored digest, or None when it is missing or unreadable."""
    try:
        raw = recstore.read_rung(client_id, source_id, 2)
    except ValueError:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_digest(client_id: str, source_id: str, digest: dict) -> None:
    """Land digest.md and digest.json, atomically and in that order."""
    recstore.write_rung(
        client_id,
        source_id,
        2,
        json.dumps(digest, indent=2, ensure_ascii=False, default=str) + "\n",
        rendered_md=render_digest_md(digest),
    )


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(
    source: Source,
    client_ctx: dict,
    progress: Callable[[str, int, int], None] | None = None,
    between_rungs: Callable[[], None] | None = None,
) -> dict:
    """Build every rung this source is missing, in order, and return the digest.

    The disk decides what to do: rungs_present is read at the start, and a rung
    that is already there is not rebuilt. Every stage then re-reads the file it
    needs rather than trusting what an earlier stage held in memory, which is
    what makes resuming an interrupted run identical to running it fresh.

    progress(stage, done, total) reports "transcribing", "correcting", and
    "digesting". between_rungs() is called at each rung boundary, immediately
    before this run starts a rung, which is where the job queue can hand the
    model to somebody who is waiting.

    Raises SourceError when there is nothing to build from, and LadderError when
    the digest itself cannot be assembled.
    """
    client_id, source_id = source.client_id, source.id
    present = recstore.rungs_present(client_id, source_id)

    if 2 in present:
        existing = load_digest(client_id, source_id)
        if existing is not None:
            return existing
        # digest.json is there but unreadable, so rung 2 is not actually done.
        present.discard(2)

    if source.entry_rung != 0:
        # Nobody said these words, so there is no transcript to build. The
        # document text is the whole input and the digest is the only rung.
        text = _source_text(client_id, source_id)
        if not (text or "").strip():
            raise SourceError(
                "There is no stored text for this source, so there is nothing to "
                "build a digest from."
            )
        _boundary(between_rungs)
        digest = build_digest(text, source, client_ctx, timed_lines=None, progress=progress)
        _write_digest(client_id, source_id, digest)
        return digest

    if 0 not in present and 1 not in present:
        audio = recstore.audio_path(client_id, source_id)
        if audio is None:
            raise SourceError(
                "There is no audio and no transcript for this source, so there is "
                "nothing to build a digest from."
            )
        _boundary(between_rungs)
        _report(progress, "transcribing", 0, 1)
        text, segments = stt.transcribe_timed(str(audio))
        turns = [
            {
                "start": segment.get("start"),
                "end": segment.get("end"),
                "speaker": None,
                "text": segment.get("text") or "",
            }
            for segment in segments or []
        ]
        recstore.write_rung(client_id, source_id, 0, sources.render_turns(turns) or text)
        _report(progress, "transcribing", 1, 1)

    # Read back from disk, never from the variable above: this is the same code
    # path a resumed run takes.
    timed_lines, plain = _read_rung0(recstore.read_rung(client_id, source_id, 0) or "")

    if 1 not in present:
        if not plain.strip():
            raise SourceError(
                "The transcript for this source is empty, so there is nothing to "
                "build a digest from."
            )
        _boundary(between_rungs)
        corrected = stt.correct_long_transcript(
            plain,
            client_ctx=client_ctx,
            progress=(lambda done, total: _report(progress, "correcting", done, total)),
        )
        recstore.write_rung(client_id, source_id, 1, corrected)

    corrected = recstore.read_rung(client_id, source_id, 1) or ""
    _boundary(between_rungs)
    digest = build_digest(
        corrected, source, client_ctx, timed_lines=timed_lines, progress=progress
    )
    _write_digest(client_id, source_id, digest)
    return digest


# ---------------------------------------------------------------------------
# The human view
# ---------------------------------------------------------------------------


def _minute(value) -> str:
    """A minute for display, or a question mark when it is not known."""
    return str(int(value)) if isinstance(value, (int, float)) else "?"


def _cell(value) -> str:
    """One table cell: single line, with pipes escaped so the table holds."""
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _bullets(items, empty: str) -> list[str]:
    """A bullet list, or one line saying there is nothing to list."""
    rows = [f"- {str(item).strip()}" for item in items or [] if str(item).strip()]
    return rows or [empty]


def render_digest_md(digest: dict) -> str:
    """Render the digest as the readable markdown saved beside digest.json.

    digest.json is the source of truth and this is a view of it. Everything the
    digest holds appears here, including the count of quotes that were dropped
    for not appearing in the transcript, so the human view never looks tidier
    than the evidence actually is.
    """
    digest = digest or {}
    duration = digest.get("duration_min")
    lines: list[str] = [
        f"# Session digest {digest.get('source_id') or '(unknown source)'}",
        "",
        f"Duration: {_minute(duration) + ' minutes' if duration else 'not known'}. "
        f"Speakers separated: {'yes' if digest.get('diarized') else 'no'}.",
        "",
        "## Timeline",
        "",
    ]

    segments = digest.get("segments") or []
    if segments:
        lines += ["| Start | End | Topic | What happened |", "| --- | --- | --- | --- |"]
        for segment in segments:
            segment = segment if isinstance(segment, dict) else {}
            lines.append(
                f"| {_minute(segment.get('start_min'))} "
                f"| {_minute(segment.get('end_min'))} "
                f"| {_cell(segment.get('topic'))} "
                f"| {_cell(segment.get('summary'))} |"
            )
    else:
        lines.append("No segments were produced for this source.")

    lines += ["", "## Themes", ""]
    lines += _bullets(digest.get("themes"), "None recorded.")

    lines += ["", "## Interventions observed", ""]
    interventions = [
        item for item in (digest.get("interventions_observed") or []) if isinstance(item, dict)
    ]
    if interventions:
        for item in interventions:
            when = item.get("start_min")
            stamp = f" (minute {_minute(when)})" if when is not None else ""
            response = str(item.get("client_response") or "").strip()
            tail = f": {response}" if response else ""
            lines.append(f"- {str(item.get('name') or '').strip()}{stamp}{tail}")
    else:
        lines.append("None recorded.")

    lines += ["", "## Client statements", ""]
    statements = [
        item for item in (digest.get("client_statements") or []) if isinstance(item, dict)
    ]
    if statements:
        for item in statements:
            when = item.get("start_min")
            stamp = f", minute {_minute(when)}" if when is not None else ""
            confidence = str(item.get("attribution_confidence") or _SAFE_CONFIDENCE)
            lines.append(
                f'- "{str(item.get("quote") or "").strip()}" '
                f"(attribution {confidence}{stamp})"
            )
    else:
        lines.append("No word for word client statements were captured from this source.")

    dropped = digest.get("_dropped_ungrounded") or 0
    if dropped:
        lines += [
            "",
            f"{dropped} candidate quote(s) were dropped because those words were "
            "not found in the transcript.",
        ]

    lines += ["", "## Risk language", ""]
    if digest.get("risk_language_present"):
        lines.append(
            "Language that may bear on risk appeared in this session. This is a "
            "flag for your attention, not an assessment."
        )
        lines.append("")
        excerpts = digest.get("risk_excerpts") or []
        lines += _bullets(
            [f'"{str(item).strip()}"' for item in excerpts],
            "No word for word excerpt is available from this source.",
        )
    else:
        lines.append("None noted.")

    lines += ["", "## Practices or homework", ""]
    lines += _bullets(digest.get("practices_or_homework"), "None recorded.")

    lines += ["", "## Unresolved threads", ""]
    lines += _bullets(digest.get("unresolved_threads"), "None recorded.")

    lines += ["", "## Notable shifts", ""]
    lines += _bullets(digest.get("notable_shifts"), "None recorded.")

    return "\n".join(lines).rstrip() + "\n"
