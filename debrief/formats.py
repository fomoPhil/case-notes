"""Note formats as data: the format registry and the single schema generator.

Before this module the DAP note was hardcoded in three places at once:
extract.py (EXTRACT_SCHEMA), prompts/extract_system.md (the note guidance), and
vault.py (the ## Data / ## Assessment / ## Plan headings). Adding SOAP or a
custom template meant editing all three by hand. Every note format now lives
here as a FormatSpec dict, and one generator, build_extract_schema, turns any
spec into the constrained JSON Schema the extractor calls with.

A FormatSpec is:

    id              stable slug used in settings and note frontmatter
    name            display name for the UI
    clinical        whether clinical / risk framing applies
    sections        [{key, heading, description}] in note order
    style_rules     one-line prose about tone / structure for the prompt
    prompt_guidance the format-specific note guidance injected into the system
                    prompt (for DAP this is migrated verbatim from the old
                    prompts/extract_system.md DAP section)
    risk_section    when True the schema carries risk_present + a risk object
                    and the writer renders a ## Risk block

Builtins ship in code; custom specs persist as _Settings/formats/<id>.json and
are validated on both save and load. Section keys are slugified and may never
collide with a reserved schema key. An unknown format id raises UnknownFormat.

GOLDEN CONTRACT: build_extract_schema(get_spec("DAP")) is byte-for-byte the old
hand-written EXTRACT_SCHEMA. A frozen-literal golden test guards it.

No em dashes anywhere.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import settings_store, vocab

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_system.md"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownFormat(KeyError):
    """Raised when a format id is not a builtin and has no custom spec on file."""


class InvalidFormatSpec(ValueError):
    """Raised when a spec fails validation (bad keys, reserved keys, no sections)."""


# ---------------------------------------------------------------------------
# Reserved keys and the shared schema fragments
# ---------------------------------------------------------------------------

# Section keys may never shadow one of the shared note fields. If a custom
# template names a section "risk" or "themes" the generated schema would collide
# with the built-in field of the same name, so these are rejected at import.
RESERVED_KEYS = frozenset(
    {"interventions", "themes", "client_quotes", "risk", "risk_present"}
)

# The risk object schema, copied verbatim from the old extract.py:37-53. When a
# spec is clinical (risk_section True) this is spliced into the note object
# unchanged so the golden DAP schema stays byte-identical.
_RISK_SCHEMA: dict = {
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
}

# The top-level actions / unsupported_requests / next_session_suggestions shell
# is format-agnostic and unchanged from the old EXTRACT_SCHEMA.
_ACTIONS_SCHEMA: dict = {
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
}

# The risk guidance prose lives in the base prompt template, delimited by these
# markers so build_extract_system can strip it for non-clinical formats.
_RISK_MARKER_START = "<!-- RISK:START -->"
_RISK_MARKER_END = "<!-- RISK:END -->"

# The per-action guidance blocks are delimited the same way so build_extract_system
# can strip a whole action type's guidance when the feature is toggled off. The
# schema enum is left unchanged; this only shapes the prompt.
_CAL_MARKER_START = "<!-- ACTION-CALENDAR:START -->"
_CAL_MARKER_END = "<!-- ACTION-CALENDAR:END -->"
_EMAIL_MARKER_START = "<!-- ACTION-EMAIL:START -->"
_EMAIL_MARKER_END = "<!-- ACTION-EMAIL:END -->"

# Injection tokens in the base prompt template.
_TOKEN_FORMAT_GUIDANCE = "{{FORMAT_GUIDANCE}}"
_TOKEN_VOCAB_TABLE = "{{VOCAB_TABLE}}"


# ---------------------------------------------------------------------------
# Builtin format specs
# ---------------------------------------------------------------------------

# DAP guidance is migrated VERBATIM from the old prompts/extract_system.md so the
# extractor behaves identically after the refactor.
_DAP_GUIDANCE = """## The note (the `note` object)

**Data** (`data`): Objective account of what happened and what the client reported this session. Ground every statement in the transcript. Carry 1 to 3 verbatim client quotes into the `client_quotes` list and weave at least one into the Data narrative. No interpretation here, just what was observed and reported.

**Assessment** (`assessment`): Your clinical interpretation tied to the client's treatment-plan goals. Use progress-or-barriers language: state whether the client is progressing toward, maintaining, or facing barriers to specific goals, and why. Reference themes and the working framework.

**Plan** (`plan`): Name the specific intervention(s) used or assigned, using vocabulary authentic to the active framework (see the vocabulary table below). State the client's specific response to those interventions, and the next clinical step or homework. Never use directive language toward the therapist.

**The audit-critical trio must appear in every note**: (1) a named intervention, (2) the client's specific response to it, (3) progress-toward-goal or barriers. A note missing any of these is not acceptable.

`interventions`: list the named interventions used this session (framework-authentic short labels, for example "cognitive restructuring", "thought record").
`themes`: list the recurring clinical themes touched this session.
`client_quotes`: 1 to 3 verbatim fragments from the transcript."""

_SOAP_GUIDANCE = """## The note (the `note` object)

**Subjective** (`subjective`): What the client reported in their own terms this session: presenting concerns, symptoms, and history as described. Carry 1 to 3 verbatim client quotes into the `client_quotes` list and weave at least one into the Subjective narrative.

**Objective** (`objective`): Observable, measurable facts from the session: presentation, affect, mental-status observations, and any scores or measures. No interpretation here.

**Assessment** (`assessment`): Your clinical interpretation tied to the client's treatment-plan goals. State whether the client is progressing toward, maintaining, or facing barriers to specific goals, and why. Reference themes and the working framework.

**Plan** (`plan`): Name the specific intervention(s) used or assigned, using vocabulary authentic to the active framework. State the client's response and the next clinical step or homework. Never use directive language toward the clinician.

`interventions`: list the named interventions used this session.
`themes`: list the recurring clinical themes touched this session.
`client_quotes`: 1 to 3 verbatim fragments from the transcript."""

_GROW_GUIDANCE = """## The note (the `note` object)

**Goal** (`goal`): The outcome the client wants from this work, in their own framing. Carry 1 to 3 verbatim client quotes into the `client_quotes` list and weave at least one into the Goal narrative.

**Reality** (`reality`): The client's current situation: what is happening now, what has been tried, and the obstacles named this session. Ground every statement in the transcript.

**Options** (`options`): The possibilities the client and coach explored, without prescribing one. Keep these as options the client generated or considered.

**Way forward** (`way_forward`): The concrete next steps and accountability the client committed to. State what the client will do and by when, in the client's own commitment.

`interventions`: list the coaching techniques or models used this session (for example "GROW", "scaling question").
`themes`: list the recurring themes touched this session.
`client_quotes`: 1 to 3 verbatim fragments from the transcript."""

_MEETING_MEMO_GUIDANCE = """## The note (the `note` object)

**Attendees** (`attendees`): Who took part in the meeting, as named in the transcript.

**Discussion** (`discussion`): A factual account of what was discussed, grounded in the transcript. No interpretation beyond what was said.

**Decisions** (`decisions`): The decisions reached in the meeting, stated plainly.

**Action items** (`action_items`): The concrete follow-up tasks agreed, with owners and any dates mentioned, in the participants' own commitment.

`interventions`: leave empty unless a specific method or framework was explicitly named.
`themes`: list the recurring topics touched this meeting.
`client_quotes`: 1 to 3 verbatim fragments from the transcript, if any stand out."""


_BUILTIN_SPECS: dict = {
    "DAP": {
        "id": "DAP",
        "name": "DAP note",
        "clinical": True,
        "sections": [
            {"key": "data", "heading": "Data", "description": "Objective account of what happened and what the client reported."},
            {"key": "assessment", "heading": "Assessment", "description": "Clinical interpretation tied to treatment-plan goals."},
            {"key": "plan", "heading": "Plan", "description": "Interventions used or assigned, the client response, and next steps."},
        ],
        "style_rules": "Professional clinical prose, third person, past tense.",
        "prompt_guidance": _DAP_GUIDANCE,
        "risk_section": True,
    },
    "SOAP": {
        "id": "SOAP",
        "name": "SOAP note",
        "clinical": True,
        "sections": [
            {"key": "subjective", "heading": "Subjective", "description": "What the client reported in their own terms."},
            {"key": "objective", "heading": "Objective", "description": "Observable, measurable facts from the session."},
            {"key": "assessment", "heading": "Assessment", "description": "Clinical interpretation tied to treatment-plan goals."},
            {"key": "plan", "heading": "Plan", "description": "Interventions used or assigned, the client response, and next steps."},
        ],
        "style_rules": "Professional clinical prose, third person, past tense.",
        "prompt_guidance": _SOAP_GUIDANCE,
        "risk_section": True,
    },
    "GROW": {
        "id": "GROW",
        "name": "GROW model",
        "clinical": False,
        "sections": [
            {"key": "goal", "heading": "Goal", "description": "The outcome the client wants from this work."},
            {"key": "reality", "heading": "Reality", "description": "The client's current situation and obstacles."},
            {"key": "options", "heading": "Options", "description": "Possibilities the client and coach explored."},
            {"key": "way_forward", "heading": "Way Forward", "description": "Concrete next steps and accountability the client committed to."},
        ],
        "style_rules": "Clear coaching prose, third person, past tense. Options only, never directives.",
        "prompt_guidance": _GROW_GUIDANCE,
        "risk_section": False,
    },
    "meeting-memo": {
        "id": "meeting-memo",
        "name": "Meeting memo",
        "clinical": False,
        "sections": [
            {"key": "attendees", "heading": "Attendees", "description": "Who took part in the meeting."},
            {"key": "discussion", "heading": "Discussion", "description": "A factual account of what was discussed."},
            {"key": "decisions", "heading": "Decisions", "description": "The decisions reached in the meeting."},
            {"key": "action_items", "heading": "Action Items", "description": "Concrete follow-up tasks agreed, with owners and dates."},
        ],
        "style_rules": "Plain professional prose. Factual and concise.",
        "prompt_guidance": _MEETING_MEMO_GUIDANCE,
        "risk_section": False,
    },
}

# The default format used when a note's frontmatter names an unknown or legacy id.
DEFAULT_FORMAT_ID = "DAP"


# ---------------------------------------------------------------------------
# Slugify + validation
# ---------------------------------------------------------------------------


def slugify_key(raw: str) -> str:
    """Turn a section label into a safe schema key: lowercase, words joined by _."""
    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def slugify_id(raw: str) -> str:
    """Turn a format name into a safe id: lowercase, words joined by a hyphen."""
    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _validate_spec(spec: dict) -> dict:
    """Return a normalized, validated copy of a spec or raise InvalidFormatSpec.

    Section keys are slugified, must be non-empty, unique, and may never be a
    reserved schema key. At least one section is required.
    """
    if not isinstance(spec, dict):
        raise InvalidFormatSpec("format spec must be an object")

    fid = slugify_id(str(spec.get("id") or spec.get("name") or ""))
    if not fid:
        raise InvalidFormatSpec("format spec needs an id or name")

    raw_sections = spec.get("sections") or []
    if not isinstance(raw_sections, list) or not raw_sections:
        raise InvalidFormatSpec("format spec needs at least one section")

    sections: list[dict] = []
    seen: set[str] = set()
    for entry in raw_sections:
        if not isinstance(entry, dict):
            raise InvalidFormatSpec("each section must be an object")
        key = slugify_key(entry.get("key") or entry.get("heading") or "")
        if not key:
            raise InvalidFormatSpec("each section needs a key or heading")
        if key in RESERVED_KEYS:
            raise InvalidFormatSpec(f"section key {key!r} is reserved")
        if key in seen:
            raise InvalidFormatSpec(f"duplicate section key {key!r}")
        seen.add(key)
        heading = str(entry.get("heading") or key.replace("_", " ").title()).strip()
        sections.append(
            {
                "key": key,
                "heading": heading,
                "description": str(entry.get("description") or "").strip(),
            }
        )

    return {
        "id": fid,
        "name": str(spec.get("name") or fid).strip(),
        "clinical": bool(spec.get("clinical", False)),
        "sections": sections,
        "style_rules": str(spec.get("style_rules") or "").strip(),
        "prompt_guidance": str(spec.get("prompt_guidance") or "").strip(),
        "risk_section": bool(spec.get("risk_section", False)),
    }


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------


def _custom_path(format_id: str) -> Path:
    """Map a format id to its spec file, refusing anything that is not a bare slug.

    A raw id like "../../../etc/passwd" would otherwise resolve to a .json file
    outside _Settings/formats. Any id that does not round-trip through slugify_id
    (path separators, dots, leading/trailing junk) is rejected up front, so no
    directory traversal can reach the filesystem.
    """
    fid = (format_id or "").strip()
    if not fid or fid != slugify_id(fid):
        raise UnknownFormat(f"unsafe or unknown note format id: {format_id!r}")
    return settings_store.formats_dir() / f"{fid}.json"


def load_custom(format_id: str) -> dict | None:
    """Return a validated custom spec from _Settings/formats/<id>.json, or None.

    Returns None for an unknown-but-safe id and for a malformed/invalid file; a
    traversal-style id raises UnknownFormat via _custom_path.
    """
    path = _custom_path(format_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return _validate_spec(raw)
    except InvalidFormatSpec:
        return None


def save_custom(spec: dict) -> dict:
    """Validate a spec and persist it as _Settings/formats/<id>.json. Returns it."""
    validated = _validate_spec(spec)
    settings_store.formats_dir().mkdir(parents=True, exist_ok=True)
    settings_store._atomic_write(  # reuse the vault atomic writer via the store
        _custom_path(validated["id"]),
        json.dumps(validated, indent=2) + "\n",
    )
    return validated


def get_spec(format_id: str | None) -> dict:
    """Return the FormatSpec for an id: builtins first, then a custom spec on file.

    Raises UnknownFormat when neither exists. A None or empty id also raises so a
    caller never silently gets DAP by accident (callers that want a fallback use
    get_spec_or_default).
    """
    fid = (format_id or "").strip()
    if fid in _BUILTIN_SPECS:
        return copy.deepcopy(_BUILTIN_SPECS[fid])
    custom = load_custom(fid) if fid else None
    if custom is not None:
        return custom
    raise UnknownFormat(f"unknown note format: {format_id!r}")


def get_spec_or_default(format_id: str | None) -> dict:
    """Like get_spec but falls back to the DAP builtin for unknown/legacy ids."""
    try:
        return get_spec(format_id)
    except UnknownFormat:
        return copy.deepcopy(_BUILTIN_SPECS[DEFAULT_FORMAT_ID])


def is_known(format_id: str | None) -> bool:
    """True when a format id resolves to a builtin or a valid custom spec."""
    try:
        get_spec(format_id)
        return True
    except UnknownFormat:
        return False


def list_specs() -> list[dict]:
    """Return [{id, name, clinical}] summaries: builtins in order, then customs.

    A custom file is advertised only when its filename stem matches the spec's
    own id (spec["id"] == path.stem). Lookup elsewhere is by filename stem, so a
    hand-dropped "My Format.json" whose spec id slugifies to "my-format" would be
    listed as "my-format" yet get_spec("my-format") would miss the file, leaving
    an advertised-but-unselectable format. Such mismatches are silently skipped.
    Dedup is on spec["id"].
    """
    out: list[dict] = []
    seen: set[str] = set()
    for fid, spec in _BUILTIN_SPECS.items():
        out.append({"id": fid, "name": spec["name"], "clinical": spec["clinical"]})
        seen.add(fid)
    try:
        custom_files = sorted(settings_store.formats_dir().glob("*.json"))
    except OSError:
        custom_files = []
    for path in custom_files:
        stem = path.stem
        try:
            spec = load_custom(stem)
        except UnknownFormat:
            # A filename that is not a bare slug can never be looked up.
            continue
        if spec is None:
            continue
        if spec["id"] != stem:
            # Filename stem and internal id disagree: get_spec(stem) would not
            # return this spec's id, so advertising it would be a dead option.
            continue
        if spec["id"] in seen:
            continue
        out.append({"id": spec["id"], "name": spec["name"], "clinical": spec["clinical"]})
        seen.add(spec["id"])
    return out


# ---------------------------------------------------------------------------
# The one schema generator
# ---------------------------------------------------------------------------


def build_extract_schema(spec: dict) -> dict:
    """Turn a FormatSpec into the constrained JSON Schema for the extract call.

    The note object is one required string per section, plus the shared
    interventions / themes / client_quotes arrays, plus the risk_present flag and
    risk object when the spec is clinical (risk_section True). The top-level
    actions / unsupported_requests / next_session_suggestions shell is fixed.

    GOLDEN CONTRACT: build_extract_schema(get_spec("DAP")) deep-equals the old
    hand-written EXTRACT_SCHEMA.
    """
    note_properties: dict = {}
    for section in spec["sections"]:
        note_properties[section["key"]] = {"type": "string"}

    note_required: list[str] = [s["key"] for s in spec["sections"]]

    if spec.get("risk_section"):
        note_properties["risk_present"] = {"type": "boolean"}
        note_properties["risk"] = copy.deepcopy(_RISK_SCHEMA)
        note_required += ["risk_present", "risk"]

    note_properties["interventions"] = {"type": "array", "items": {"type": "string"}}
    note_properties["themes"] = {"type": "array", "items": {"type": "string"}}
    note_properties["client_quotes"] = {"type": "array", "items": {"type": "string"}}
    note_required += ["interventions", "themes", "client_quotes"]

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "note": {
                "type": "object",
                "additionalProperties": False,
                "properties": note_properties,
                "required": note_required,
            },
            "actions": copy.deepcopy(_ACTIONS_SCHEMA),
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


# ---------------------------------------------------------------------------
# The system prompt builder
# ---------------------------------------------------------------------------


def _load_base_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _apply_risk_region(template: str, include_risk: bool) -> str:
    """Keep or drop the risk guidance block delimited by the RISK markers."""
    start = template.find(_RISK_MARKER_START)
    end = template.find(_RISK_MARKER_END)
    if start == -1 or end == -1:
        return template
    end += len(_RISK_MARKER_END)
    if include_risk:
        inner = template[start + len(_RISK_MARKER_START) : template.find(_RISK_MARKER_END)]
        return template[:start] + inner.strip("\n") + template[end:]
    # Drop the whole block and the blank line that follows it.
    tail = template[end:]
    if tail.startswith("\n\n"):
        tail = tail[1:]
    return template[:start].rstrip("\n") + "\n\n" + tail.lstrip("\n")


def _apply_marked_region(
    template: str, start_marker: str, end_marker: str, include: bool
) -> str:
    """Keep or drop every region delimited by start_marker / end_marker.

    include True removes just the marker lines and keeps the content; include
    False removes the whole region (collapsing the surrounding blank lines).
    Handles more than one region sharing the same marker pair.
    """
    while True:
        start = template.find(start_marker)
        if start == -1:
            break
        end = template.find(end_marker, start)
        if end == -1:
            break
        end_full = end + len(end_marker)
        if include:
            inner = template[start + len(start_marker) : end].strip("\n")
            template = template[:start].rstrip("\n") + "\n\n" + inner + "\n\n" + template[end_full:].lstrip("\n")
        else:
            template = template[:start].rstrip("\n") + "\n\n" + template[end_full:].lstrip("\n")
    return template


def _custom_prompt_layer(format_id: str) -> str:
    """Return an appended compiled prompt layer from _Settings/profile/<id>.prompt.md."""
    try:
        path = settings_store.profile_dir() / f"{format_id}.prompt.md"
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text


def build_extract_system(
    spec: dict, profession: str = "therapy", features: dict | None = None
) -> str:
    """Assemble the extraction system prompt for a format and profession.

    Base template (format-agnostic rules, actions, output discipline)
      + spec.prompt_guidance (this format's note structure)
      + vocab.extract_vocab_table(profession) (framework vocabulary)
      + risk guidance kept only when spec.risk_section
      + a custom compiled prompt layer when _Settings/profile/<id>.prompt.md exists.

    features (default None = all on) toggles the per-action guidance blocks: when
    features["calendar"] is False the schedule_followup guidance is stripped, and
    when features["email"] is False the draft_client_email guidance is stripped.
    The schema enum is never changed here.
    """
    feats = features or {}
    calendar_on = feats.get("calendar", True)
    email_on = feats.get("email", True)

    template = _load_base_template()
    template = _apply_risk_region(template, bool(spec.get("risk_section")))
    template = _apply_marked_region(
        template, _CAL_MARKER_START, _CAL_MARKER_END, calendar_on
    )
    template = _apply_marked_region(
        template, _EMAIL_MARKER_START, _EMAIL_MARKER_END, email_on
    )
    template = template.replace(_TOKEN_FORMAT_GUIDANCE, spec.get("prompt_guidance", "").strip())
    template = template.replace(_TOKEN_VOCAB_TABLE, vocab.extract_vocab_table(profession).strip())

    prompt = template.strip() + "\n"

    layer = _custom_prompt_layer(spec.get("id", ""))
    if layer:
        prompt += "\n## Additional guidance for this template\n\n" + layer + "\n"
    return prompt
