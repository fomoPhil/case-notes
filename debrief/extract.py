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
    features: dict | None = None,
) -> dict:
    """Run the single constrained extraction call and resolve action dates.

    The schema and system prompt are generated per call from the active format
    (format_id) and profession, so switching formats needs no code change here.
    features (default None = all on) drops guidance for any disabled action type
    from the system prompt while leaving the schema enum stable. Returns a
    schema-shaped dict, with each schedule_followup action carrying an added
    "resolved_datetime" (ISO string or None).
    """
    # Resolve the spec ONCE so the schema and system prompt share one section
    # list, then key the caches by the resolved id.
    spec = formats.get_spec_or_default(format_id)
    schema = _schema_for(spec)
    system = _system_for(spec, profession, features)
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
