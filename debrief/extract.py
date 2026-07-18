"""Intent + DAP note extraction: the model's "brain" role.

One constrained LLM call turns a corrected transcript plus client context into
an EXTRACT_SCHEMA-shaped dict (DAP note, requested actions, next-session
options). A deterministic post-pass then resolves each schedule_followup's
spoken time phrase into an absolute ISO datetime. The model NEVER computes
dates.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import dates, llm
from .config import DEFAULT_SESSION_MINUTES

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_system.md"

# Framework-authentic vocabulary, mirrored from prompts/extract_system.md so the
# active framework can be injected explicitly into the user message.
_FRAMEWORK_VOCAB = {
    "CBT": "cognitive restructuring, automatic thoughts, cognitive distortions, thought records, behavioral activation, graded exposure, Socratic questioning",
    "ACT": "cognitive defusion, willingness, values clarification, committed action, self-as-context, acceptance, mindfulness",
    "DBT": "diary card, chain analysis, target behaviors, the four skills modules, validation",
    "FAMILY SYSTEMS": "subsystems, boundaries, enmeshment, enactment, differentiation, triangulation, genogram",
    "EMDR": "target memory, negative and positive cognitions (NC/PC), SUDs 0 to 10, VOC 1 to 7, bilateral stimulation, body scan",
    "PSYCHODYNAMIC": "transference, countertransference, defenses, interpretation, insight, working through",
}

# JSON Schema for the single constrained call. Actions use a unified nullable
# shape (all fields present, per-type fields populated) for reliable strict
# structured output; the post-pass and downstream code key off action["type"].
EXTRACT_SCHEMA: dict = {
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


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


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


def _build_user_message(transcript: str, client_ctx: dict, framework: str) -> str:
    fw_key = (framework or "").strip().upper()
    vocab = _FRAMEWORK_VOCAB.get(fw_key, "")
    vocab_line = (
        f"ACTIVE FRAMEWORK: {framework}. Use this vocabulary and no other framework's: {vocab}."
        if vocab
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
) -> dict:
    """Run the single constrained extraction call and resolve action dates.

    Returns an EXTRACT_SCHEMA-shaped dict, with each schedule_followup action
    carrying an added "resolved_datetime" (ISO string or None).
    """
    messages = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "user", "content": _build_user_message(transcript, client_ctx, framework)},
    ]
    result = llm.chat(messages, schema=EXTRACT_SCHEMA, max_tokens=2000, temperature=0.2)
    if not isinstance(result, dict):
        raise RuntimeError(f"extract expected a dict, got {type(result)}")
    _resolve_action_dates(result, now)
    return result
