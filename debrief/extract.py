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

import json
from datetime import datetime

from . import dates, formats, llm, vocab
from .config import DEFAULT_SESSION_MINUTES

# Kept for backward compatibility: the DAP schema, now generated from the
# registry. build_extract_schema(get_spec("DAP")) is byte-identical to the old
# hand-written literal (guarded by a golden test).
EXTRACT_SCHEMA: dict = formats.build_extract_schema(formats.get_spec("DAP"))

# Generated schema + system prompt caches, keyed by (format_id, profession) so a
# repeated call for the same format does not rebuild them.
_SCHEMA_CACHE: dict[str, dict] = {}
_SYSTEM_CACHE: dict[tuple[str, str], str] = {}


def _schema_for(format_id: str) -> dict:
    if format_id not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[format_id] = formats.build_extract_schema(
            formats.get_spec_or_default(format_id)
        )
    return _SCHEMA_CACHE[format_id]


def _system_for(format_id: str, profession: str) -> str:
    # Custom prompt layers can change on disk, so only builtins are cached.
    spec = formats.get_spec_or_default(format_id)
    key = (spec["id"], profession)
    if spec["id"] in formats._BUILTIN_SPECS:
        if key not in _SYSTEM_CACHE:
            _SYSTEM_CACHE[key] = formats.build_extract_system(spec, profession)
        return _SYSTEM_CACHE[key]
    return formats.build_extract_system(spec, profession)


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


def _build_user_message(
    transcript: str, client_ctx: dict, framework: str, profession: str = "therapy"
) -> str:
    fw_vocab = vocab.extract_framework_vocab(framework, profession)
    vocab_line = (
        f"ACTIVE FRAMEWORK: {framework}. Use this vocabulary and no other framework's: {fw_vocab}."
        if fw_vocab
        else f"ACTIVE FRAMEWORK: {framework}."
    )
    return (
        f"{vocab_line}\n\n"
        f"CLIENT CONTEXT:\n{_format_context(client_ctx)}\n\n"
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
) -> dict:
    """Run the single constrained extraction call and resolve action dates.

    The schema and system prompt are generated per call from the active format
    (format_id) and profession, so switching formats needs no code change here.
    Returns a schema-shaped dict, with each schedule_followup action carrying an
    added "resolved_datetime" (ISO string or None).
    """
    schema = _schema_for(format_id)
    system = _system_for(format_id, profession)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_user_message(transcript, client_ctx, framework, profession),
        },
    ]
    result = llm.chat(messages, schema=schema, max_tokens=2000, temperature=0.2)
    if not isinstance(result, dict):
        raise RuntimeError(f"extract expected a dict, got {type(result)}")
    _resolve_action_dates(result, now)
    return result
