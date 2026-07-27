"""Intent + note extraction: the model's "brain" role.

One constrained LLM call turns a corrected transcript plus client context into
a schema-shaped dict (the session note in the active format, requested actions,
next-session options). A deterministic post-pass then resolves each
schedule_followup's spoken time phrase into an absolute ISO datetime. The model
NEVER computes dates.

The note format is data, not code: formats.build_extract_schema turns the
active FormatSpec into the constrained schema and formats.build_extract_system
assembles the matching system prompt. The module-level EXTRACT_SCHEMA symbol is
kept (generated from the DAP builtin) so older importers keep working.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

from . import dates, formats, llm, vocab
from .config import DEFAULT_SESSION_MINUTES

# Kept for backward compatibility: the DAP schema, now generated from the
# registry. build_extract_schema(get_spec("DAP")) is byte-identical to the old
# hand-written literal (guarded by a golden test).
EXTRACT_SCHEMA: dict = formats.build_extract_schema(formats.get_spec("DAP"))

# Generated schema + system prompt caches. Both are keyed by the RESOLVED spec id
# (not the requested format_id) and ONLY builtin specs are ever cached. Custom
# specs and unknown-id fallbacks are rebuilt every call, because a custom spec
# file (its sections or prompt layers) can change on disk between requests; a
# stale cached schema there would silently drop clinical content. The caller
# resolves the spec once and hands the same object to both helpers so the schema
# and system prompt can never disagree on the section list.
_SCHEMA_CACHE: dict[str, dict] = {}
_SYSTEM_CACHE: dict[tuple[str, str], str] = {}


def _schema_for(spec: dict) -> dict:
    sid = spec["id"]
    if sid in formats._BUILTIN_SPECS:
        if sid not in _SCHEMA_CACHE:
            _SCHEMA_CACHE[sid] = formats.build_extract_schema(spec)
        return _SCHEMA_CACHE[sid]
    return formats.build_extract_schema(spec)


def _system_for(spec: dict, profession: str, features: dict | None = None) -> str:
    # Custom prompt layers can change on disk, so only builtins are cached. The
    # cache is also bypassed whenever feature toggles are supplied, because a
    # disabled action type strips guidance from the prompt and must not be
    # served from an all-on cached copy.
    sid = spec["id"]
    if features is None and sid in formats._BUILTIN_SPECS:
        key = (sid, profession)
        if key not in _SYSTEM_CACHE:
            _SYSTEM_CACHE[key] = formats.build_extract_system(spec, profession)
        return _SYSTEM_CACHE[key]
    return formats.build_extract_system(spec, profession, features)


def _format_context(client_ctx: dict) -> str:
    """Render whatever the vault handed us into a readable context block.

    client_context() returns profile frontmatter + summary body + last session
    note text; we format known keys nicely and fall back to key: value lines.
    """
    lines: list[str] = []
    for key, value in client_ctx.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, default=str)
        label = key.replace("_", " ").title()
        lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(no prior context on file)"


# ---------------------------------------------------------------------------
# Session evidence: the digest, rendered for the extractor to read
# ---------------------------------------------------------------------------
#
# The clinician's debrief and the session recording answer two different
# questions, and collapsing them is the failure this whole block is shaped to
# avoid. The debrief knows what the session MEANT and what to do about it. The
# recording knows what was SAID, in what words, in what order. So the evidence
# arrives clearly labelled as evidence, with the precedence stated in the prompt
# and the conflicts recorded rather than resolved.
#
# Only a statement the ladder could tie to the client's own mouth is rendered
# inside quotation marks. Everything else is an unattributed observation with no
# quote marks anywhere near it, because a misattributed quote in a clinical
# record survives in the file forever and an unattributed observation does not.

_EVIDENCE_FRAMING = (
    "SESSION EVIDENCE (distilled from the session recording, not from the "
    "clinician):\n"
    "The clinician's debrief below is authoritative on meaning, priority, and "
    "clinical judgment. This session evidence is authoritative on exact wording "
    "and chronology. Where the two disagree, do not resolve it and do not pick "
    "one: write the note from the clinician's debrief and record the "
    "disagreement in plain language in evidence_conflicts.\n"
    "Only the statements listed under Client statements captured word for word "
    "were confirmed as this client speaking. Everything else below, including "
    "unattributed statements and flagged language, is an observation about the "
    "session: it may inform the note and may never be written as a client quote."
)

# One line, and only this line. The digest flags language for a human to look
# at; it never assesses, never scores, and never decides anything.
_RISK_EVIDENCE_LINE = (
    "Risk-related language was flagged in the session recording for your review. "
    "It is not an assessment."
)

# Risk excerpts are grounded in the transcript and nothing more. Unlike a client
# statement they are never put through the attribution check, so a sentence the
# CLINICIAN spoke can be sitting in that list. Rendering one in quotation marks
# under a prompt that says quoted text is the client speaking is how a
# therapist's own words end up in a clinical record as their client's.
_RISK_EXCERPT_HEADING = "Flagged language from the session (speaker unconfirmed):"

# What the model may add to the note when the two inputs disagree. Appended to
# a deep copy of the resolved schema, never to the cached object itself.
_CONFLICTS_KEY = "evidence_conflicts"
_CONFLICTS_PROPERTY: dict = {"type": "array", "items": {"type": "string"}}

# What a reference digest calls itself in its own body (ladder.REFERENCE_KIND).
# Named here rather than imported, exactly as the digest's other field names are:
# this module reads the digest's shape by name and does not otherwise depend on
# the module that builds it.
_REFERENCE_KIND = "reference"

# The fields that make a claim about a session: who said what, what was done to
# whom, and whether anything said bears on risk. Reference material may not
# carry any of them, so a digest holding even one of them is refused at the
# reference door rather than rendered through it.
_SESSION_CLAIM_FIELDS = (
    "client_statements",
    "interventions_observed",
    "risk_language_present",
    "risk_excerpts",
)

# The reference block's framing. Everything about it is the opposite of the
# session evidence framing above: there is no session, no client was in the
# room, and the note being drafted records that material was shared and what is
# in it. The last clause is the one doing the work.
_REFERENCE_FRAMING = (
    "REFERENCE MATERIAL (saved by the clinician, not a session with this "
    "client):\n"
    "The clinician saved this reference material for this client. Draft a note "
    "recording that the material was shared and what it covers. There was no "
    "session; invent nothing about one."
)


def _minute_label(value) -> str:
    """A minute for display, or a question mark when the ladder had none.

    An invented timestamp in a clinical record is worse than an absent one, so
    the digest leaves start_min null when it cannot compute it and this says so.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return str(int(value))


def _minute_suffix(value) -> str:
    return f" (minute {_minute_label(value)})" if value is not None else ""


def _evidence_list(value) -> list[str]:
    """Whatever the digest holds in a list field, as clean non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (str(item or "").strip() for item in value) if text]


def _render_digest_evidence(digest: dict) -> str:
    """Render a session digest as the labelled evidence block the prompt carries.

    Everything the digest knows appears here except the quotes it dropped, in
    the order a reader would want it: what happened and when, then the themes,
    then what was actually said, then what is still open, then the risk flag.

    The one structural rule: quotation marks appear around a client statement
    whose attribution_confidence is "high", and nowhere else in this block.
    Every other statement, and every risk excerpt, is rendered as an
    observation with no quotation marks, so nothing downstream can mistake it
    for somebody's exact words. Risk excerpts in particular are grounded in the
    transcript but never attribution checked, so the speaker is unknown by
    construction.
    """
    digest = digest or {}
    lines: list[str] = []

    segments = [item for item in (digest.get("segments") or []) if isinstance(item, dict)]
    if segments:
        lines.append("Session timeline:")
        for segment in segments:
            topic = str(segment.get("topic") or "").strip() or "(no topic)"
            summary = str(segment.get("summary") or "").strip()
            row = (
                f"- Minutes {_minute_label(segment.get('start_min'))} to "
                f"{_minute_label(segment.get('end_min'))}, {topic}"
            )
            lines.append(f"{row}: {summary}" if summary else row)
        lines.append("")

    themes = _evidence_list(digest.get("themes"))
    if themes:
        lines.append("Themes:")
        lines += [f"- {theme}" for theme in themes]
        lines.append("")

    interventions = [
        item
        for item in (digest.get("interventions_observed") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if interventions:
        lines.append("Interventions observed:")
        for item in interventions:
            response = str(item.get("client_response") or "").strip()
            lines.append(
                f"- {str(item.get('name')).strip()}"
                f"{_minute_suffix(item.get('start_min'))}"
                f"{f': {response}' if response else ''}"
            )
        lines.append("")

    statements = [
        item
        for item in (digest.get("client_statements") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    attributed = [
        item
        for item in statements
        if str(item.get("attribution_confidence") or "").strip().casefold() == "high"
    ]
    unattributed = [item for item in statements if item not in attributed]

    lines.append("Client statements captured word for word:")
    if attributed:
        for item in attributed:
            lines.append(
                f'- "{str(item.get("quote")).strip()}"'
                f"{_minute_suffix(item.get('start_min'))}"
            )
    else:
        lines.append(
            "- None. The recording confirmed no client quote, so take no quote "
            "from this evidence."
        )
    lines.append("")

    if unattributed:
        lines.append("Reported in session (unattributed):")
        for item in unattributed:
            lines.append(
                f"- {str(item.get('quote')).strip()}"
                f"{_minute_suffix(item.get('start_min'))}"
            )
        lines.append("")

    for label, key in (
        ("Unresolved threads:", "unresolved_threads"),
        ("Practices or homework:", "practices_or_homework"),
        ("Notable shifts:", "notable_shifts"),
    ):
        items = _evidence_list(digest.get(key))
        if items:
            lines.append(label)
            lines += [f"- {item}" for item in items]
            lines.append("")

    if digest.get("risk_language_present"):
        lines.append("Risk language:")
        lines.append(_RISK_EVIDENCE_LINE)
        excerpts = _evidence_list(digest.get("risk_excerpts"))
        if excerpts:
            lines.append(_RISK_EXCERPT_HEADING)
            lines += [f"- {excerpt}" for excerpt in excerpts]
        lines.append("")

    return "\n".join(lines).rstrip()


def _evidence_block(digest: dict) -> str:
    """The framing plus the rendered evidence, ready to slot into the message.

    Refuses a reference digest outright. Reference material is not a session
    with anybody, so nothing in it is evidence about this client, and the block
    above says the opposite in its first line. The pipeline already gates this
    before the call is built; this raise is what makes sure a future caller
    cannot route reference material through the session door in silence.
    """
    if str((digest or {}).get("kind") or "").strip().casefold() == _REFERENCE_KIND:
        raise ValueError(
            "reference material is never session evidence: a reference digest "
            "cannot be rendered as evidence about a client."
        )
    return f"{_EVIDENCE_FRAMING}\n\n{_render_digest_evidence(digest)}\n\n"


def _render_reference_material(digest: dict) -> str:
    """Render a reference digest as the material description the prompt carries.

    Only what the material itself is and covers: its title, what it is about,
    its timeline, its key points, what it suggests trying, and its topics. There
    is nothing here that can be read as somebody's words, because a reference
    digest has no field that holds them.

    The two fallbacks handle the rarer shape: a session digest whose source was
    flipped to reference material after it was built comes back from
    enforce_provenance as the descriptive fields only, under their session
    names. Those are the same facts under a different key, and the guard in
    _reference_block has already refused anything carrying a session claim.
    """
    digest = digest or {}
    lines: list[str] = []

    title = str(digest.get("title") or "").strip()
    if title:
        lines.append(f"Title: {title}")
    summary = str(digest.get("summary") or "").strip()
    if summary:
        lines.append(f"What it covers: {summary}")
    if lines:
        lines.append("")

    segments = [item for item in (digest.get("segments") or []) if isinstance(item, dict)]
    if segments:
        lines.append("Timeline:")
        for segment in segments:
            topic = str(segment.get("topic") or "").strip() or "(no topic)"
            text = str(segment.get("summary") or "").strip()
            row = (
                f"- Minutes {_minute_label(segment.get('start_min'))} to "
                f"{_minute_label(segment.get('end_min'))}, {topic}"
            )
            lines.append(f"{row}: {text}" if text else row)
        lines.append("")

    for label, items in (
        ("Key points:", _evidence_list(digest.get("key_points"))),
        (
            "Practices or suggestions:",
            _evidence_list(digest.get("practices_or_suggestions"))
            or _evidence_list(digest.get("practices_or_homework")),
        ),
        (
            "Topics:",
            _evidence_list(digest.get("topics")) or _evidence_list(digest.get("themes")),
        ),
    ):
        if items:
            lines.append(label)
            lines += [f"- {item}" for item in items]
            lines.append("")

    return "\n".join(lines).rstrip()


def _reference_block(digest: dict) -> str:
    """The reference framing plus the rendered material, for the draft path.

    Guarded on the way in: a digest carrying any field that makes a claim about
    a session is refused rather than rendered. That is what makes the end to end
    promise hold in code rather than in convention. A note drafted from
    reference material cannot quote a client, because a digest that could hold a
    client's words never reaches this renderer.
    """
    digest = digest or {}
    present = [field for field in _SESSION_CLAIM_FIELDS if field in digest]
    if present:
        raise ValueError(
            "reference material makes no claim about a session: this digest "
            f"still carries {', '.join(present)}."
        )
    return f"{_REFERENCE_FRAMING}\n\n{_render_reference_material(digest)}\n\n"


def _schema_with_conflicts(schema: dict) -> dict:
    """A copy of the resolved schema that can also carry evidence_conflicts.

    Deep copied, always. The schema handed in is very often the cached object
    shared by every DAP extraction in the process, and adding a property to it
    in place would leak a digest-only field into every later call, including the
    ones with no session evidence at all.
    """
    out = copy.deepcopy(schema)
    properties = out.setdefault("properties", {})
    properties[_CONFLICTS_KEY] = copy.deepcopy(_CONFLICTS_PROPERTY)
    required = list(out.get("required") or [])
    if _CONFLICTS_KEY not in required:
        required.append(_CONFLICTS_KEY)
    out["required"] = required
    return out


def _build_user_message(
    transcript: str,
    client_ctx: dict,
    framework: str,
    profession: str = "therapy",
    digest: dict | None = None,
    reference: dict | None = None,
) -> str:
    fw_vocab = vocab.extract_framework_vocab(framework, profession)
    vocab_line = (
        f"ACTIVE FRAMEWORK: {framework}. Use this vocabulary and no other framework's: {fw_vocab}."
        if fw_vocab
        else f"ACTIVE FRAMEWORK: {framework}."
    )
    # With neither a digest nor reference material this evaluates to the empty
    # string, so the message is the one this function has always built,
    # character for character.
    if digest is not None:
        evidence = _evidence_block(digest)
    elif reference is not None:
        evidence = _reference_block(reference)
    else:
        evidence = ""
    return (
        f"{vocab_line}\n\n"
        f"CLIENT CONTEXT:\n{_format_context(client_ctx)}\n\n"
        f"{evidence}"
        f"SESSION DEBRIEF TRANSCRIPT:\n{transcript.strip()}\n\n"
        "Produce the JSON object now."
    )


def _resolve_action_dates(result: dict, now: datetime) -> None:
    """Fill resolved_datetime (ISO) for each schedule_followup, in place."""
    for action in result.get("actions", []):
        if action.get("type") != "schedule_followup":
            continue
        # Default the duration if the model left it null.
        if not action.get("duration_min"):
            action["duration_min"] = DEFAULT_SESSION_MINUTES
        utterance = action.get("datetime_utterance")
        resolved = dates.resolve_utterance(utterance, now) if utterance else None
        action["resolved_datetime"] = resolved.isoformat() if resolved else None


def extract(
    transcript: str,
    client_ctx: dict,
    framework: str,
    now: datetime,
    format_id: str = "DAP",
    profession: str = "therapy",
    features: dict | None = None,
    spec: dict | None = None,
    digest: dict | None = None,
    reference: dict | None = None,
) -> dict:
    """Run the single constrained extraction call and resolve action dates.

    The schema and system prompt are generated per call from the active format
    (format_id) and profession, so switching formats needs no code change here.
    features (default None = all on) drops guidance for any disabled action type
    from the system prompt while leaving the schema enum stable. Returns a
    schema-shaped dict, with each schedule_followup action carrying an added
    "resolved_datetime" (ISO string or None).

    spec, when given, is a FormatSpec object used directly and overrides the
    format_id lookup entirely. This is how the template-import dry run exercises
    a candidate format that has NOT been saved to disk yet: the compiler hands the
    transient spec straight in so nothing is persisted. A spec passed this way is
    never cached (its id may not resolve to a builtin), matching the custom-spec
    rule below.

    digest, when given, is a session digest (ladder rung 2) from a recording of
    the session itself. It is rendered into the user message as a clearly
    labelled evidence block between the client context and the debrief, and the
    schema gains an evidence_conflicts list for the disagreements the model must
    record rather than resolve. This is the ONLY thing the digest changes: with
    digest=None every byte of this call is what it has always been, which is the
    regression that protects the hero flow. The digest never carries the raw or
    corrected transcript, per spec section 6 rule 1.

    reference, when given, is a REFERENCE digest: material the clinician saved
    for this client that is not a session with them. It is rendered through a
    different block entirely, framed as material that was shared rather than as
    evidence about anybody, and the schema does NOT gain evidence_conflicts:
    there is no second account of a session to disagree with. A call may carry
    session evidence or reference material, never both.
    """
    if digest is not None and reference is not None:
        raise ValueError(
            "a call carries session evidence or reference material, never both."
        )
    # Resolve the spec ONCE so the schema and system prompt share one section
    # list, then key the caches by the resolved id. A caller-supplied spec wins.
    supplied = spec is not None
    if spec is None:
        spec = formats.get_spec_or_default(format_id)
    if supplied:
        # A caller-supplied transient spec (a preview candidate that has not been
        # saved) is NEVER cached and NEVER served from cache. Its id can collide
        # with a builtin id (for example a candidate that claims "meeting-memo"),
        # and a cache keyed on that id would both poison the real builtin's
        # schema/system entries and, on a warm cache, hand the builtin schema
        # back to the preview instead of the candidate's own sections. Build both
        # directly every call.
        schema = formats.build_extract_schema(spec)
        system = formats.build_extract_system(spec, profession, features)
    else:
        schema = _schema_for(spec)
        system = _system_for(spec, profession, features)
    if digest is not None:
        # A copy, so the shared cached schema keeps the shape every other
        # extraction in this process is constrained to.
        schema = _schema_with_conflicts(schema)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_user_message(
                transcript, client_ctx, framework, profession, digest, reference
            ),
        },
    ]
    result = llm.chat(messages, schema=schema, max_tokens=2000, temperature=0.2)
    if not isinstance(result, dict):
        raise RuntimeError(f"extract expected a dict, got {type(result)}")
    _resolve_action_dates(result, now)
    if digest is not None and not isinstance(result.get(_CONFLICTS_KEY), list):
        # The pipeline appends to this list deterministically, so it has to be
        # there whatever the model did with a schema field it was free to omit.
        result[_CONFLICTS_KEY] = []
    return result
