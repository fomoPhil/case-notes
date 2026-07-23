"""Route a transcript to the clinical debrief flow or the in-app assistant.

One small constrained model call decides whether the input is a first-person
recap of a session (session_debrief, handled by the deterministic clinical
pipeline) or a request/command/question (assistant, handled by the agent).
"""

from __future__ import annotations

from pathlib import Path

from . import llm

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "classify_system.md"

_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "route": {"type": "string", "enum": ["session_debrief", "assistant"]},
        "client_hint": {"type": "string"},
    },
    "required": ["route", "client_hint"],
}


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def classify(transcript: str, has_selected_client: bool) -> dict:
    """Return {"route": "session_debrief"|"assistant", "client_hint": str}.

    Falls back to the assistant route on any model failure, since the assistant
    is the safer default (it never writes to the clinical record without
    approval).
    """
    text = (transcript or "").strip()
    if not text:
        return {"route": "assistant", "client_hint": ""}

    selected = "true" if has_selected_client else "false"
    user = (
        f"A client is currently selected: {selected}.\n\n"
        f"INPUT:\n{text}\n\n"
        "Classify it now."
    )
    messages = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "user", "content": user},
    ]
    try:
        result = llm.chat(messages, schema=_SCHEMA, max_tokens=200, temperature=0.0)
    except Exception:  # noqa: BLE001 - default to the safe assistant route
        return {"route": "assistant", "client_hint": ""}

    if not isinstance(result, dict):
        return {"route": "assistant", "client_hint": ""}
    route = result.get("route")
    if route not in ("session_debrief", "assistant"):
        route = "assistant"
    hint = result.get("client_hint") or ""
    return {"route": route, "client_hint": str(hint).strip()}
