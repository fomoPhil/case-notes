"""In-app agent: a tool-calling loop over the vault, staging proposals.

run_agent(user_text, now, client_hint) drives the local Gemma model through a
closed set of tools. Read tools (list_clients, read_client_file, search_vault)
execute live against the vault. Write tools (create_worksheet, draft_email) do
NOT touch the vault or Mail: they STAGE a proposal and return a synthetic
success result. Nothing is filed or sent until the app's /api/assistant/execute
endpoint runs the therapist-approved proposals.

Robustness for a local 12B model:
  - malformed tool-argument JSON -> a tool error asking for valid JSON (bounded
    retries per tool call),
  - unknown tool -> a tool error naming the closed tool list,
  - an identical repeated call -> an injected nudge instead of re-running it,
  - a transport or protocol failure -> AgentUnavailable carrying a doctor hint,
  - a turn cap -> one forced, tool-free finalize call.

No em dashes anywhere in generated copy.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from . import llm, vault
from .config import AGENT_MAX_TURNS

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "agent_system.md"

# Where worksheet preview PDFs are staged before approval. Outside the vault.

_MAX_ARG_RETRIES = 2

_DOCTOR_HINT = (
    "The local model did not respond. Open LM Studio and load the gemma model "
    "(lms load gemma-4-12b-it-qat --context-length 64000 -y), then try again."
)


class AgentUnavailable(RuntimeError):
    """Raised when the model server is unreachable or misbehaving mid-loop."""

    def __init__(self, message: str, hint: str = _DOCTOR_HINT):
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format). Closed set.
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_clients",
            "description": "List the clients on file with their id, name, and framework.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_client_file",
            "description": (
                "Read one file inside a client's folder, for example "
                "'_Profile.md', 'Treatment-Plan.md', or "
                "'Sessions/2026-07-14-session.md'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string", "description": "Client id like C-0001."},
                    "filename": {
                        "type": "string",
                        "description": "Path relative to the client folder.",
                    },
                },
                "required": ["client_id", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": (
                "Case-insensitive search across client profiles, session notes, "
                "Templates, and Interventions. Returns paths, titles, snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_worksheet",
            "description": (
                "Stage a worksheet (Markdown) for the therapist to approve. Does "
                "not save anything. Omit client_id to file it in the shared library."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "markdown_body": {
                        "type": "string",
                        "description": "The worksheet content as clean Markdown.",
                    },
                    "client_id": {
                        "type": "string",
                        "description": "Optional client id to file it under.",
                    },
                },
                "required": ["title", "markdown_body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_email",
            "description": (
                "Stage a Mail draft to a client for the therapist to approve. Never "
                "sends. Set attach_worksheet true to attach a worksheet you created "
                "in this same request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "attach_worksheet": {"type": "boolean"},
                },
                "required": ["client_id", "subject", "body"],
            },
        },
    },
]

_READ_TOOLS = {"list_clients", "read_client_file", "search_vault"}
_WRITE_TOOLS = {"create_worksheet", "draft_email"}
_KNOWN_TOOLS = _READ_TOOLS | _WRITE_TOOLS


def _no_em_dash(text: str) -> str:
    return (text or "").replace("—", "-").replace("―", "-")


def _load_system_prompt(now: _dt.datetime) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    now_str = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
    return template.replace("{now}", now_str)


# ---------------------------------------------------------------------------
# Live read-tool execution
# ---------------------------------------------------------------------------


def _exec_read_tool(name: str, args: dict) -> str:
    """Run a read tool against the vault and return a JSON string result."""
    if name == "list_clients":
        clients = vault.list_clients()
        slim = [
            {
                "client_id": c.get("client_id"),
                "name": c.get("name"),
                "framework": c.get("framework"),
            }
            for c in clients
        ]
        return json.dumps(slim, ensure_ascii=False)

    if name == "read_client_file":
        client_id = args.get("client_id")
        filename = args.get("filename")
        try:
            text = vault.read_client_file(client_id, filename)
        except vault.VaultPathError as exc:
            return json.dumps({"error": f"path rejected: {exc}"})
        except FileNotFoundError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"path": f"Clients/{client_id}/{filename}", "content": text}, ensure_ascii=False)

    if name == "search_vault":
        query = args.get("query", "")
        hits = vault.search_vault(query)
        return json.dumps({"query": query, "results": hits}, ensure_ascii=False)

    return json.dumps({"error": f"unknown read tool {name!r}"})


# ---------------------------------------------------------------------------
# Write-tool staging (proposals only; no vault or Mail side effects)
# ---------------------------------------------------------------------------


def _stage_worksheet(args: dict, proposals: list[dict]) -> str:
    title = _no_em_dash(str(args.get("title") or "Worksheet").strip())
    body = _no_em_dash(str(args.get("markdown_body") or "").strip())
    client_id = args.get("client_id") or None
    if isinstance(client_id, str):
        client_id = client_id.strip() or None

    proposals.append(
        {
            "type": "worksheet",
            "title": title,
            "markdown_body": body,
            "client_id": client_id,
        }
    )
    where = f"client {client_id}" if client_id else "the shared library"
    return json.dumps(
        {
            "status": "staged",
            "detail": (
                f"Worksheet '{title}' is staged for {where}. It is NOT saved yet; "
                "the therapist approves it in the app."
            ),
        }
    )


def _stage_email(args: dict, proposals: list[dict]) -> str:
    client_id = str(args.get("client_id") or "").strip()
    subject = _no_em_dash(str(args.get("subject") or "").strip())
    body = _no_em_dash(str(args.get("body") or "").strip())
    attach = bool(args.get("attach_worksheet"))
    if not client_id:
        return json.dumps({"error": "draft_email needs a client_id."})
    proposals.append(
        {
            "type": "email",
            "client_id": client_id,
            "subject": subject,
            "body": body,
            "attach_worksheet": attach,
        }
    )
    return json.dumps(
        {
            "status": "staged",
            "detail": (
                f"Email draft to {client_id} is staged. It is NOT sent; the "
                "therapist reviews the Mail draft before anything leaves the Mac."
            ),
        }
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _forced_finalize(messages: list) -> str:
    """One tool-free call asking the model to summarize what it prepared."""
    convo = list(messages)
    convo.append(
        {
            "role": "user",
            "content": (
                "Stop using tools now. In two or three sentences, tell the "
                "therapist what you prepared and remind them nothing is saved "
                "until they approve it. Do not use em dashes."
            ),
        }
    )
    try:
        out = llm.chat(convo, max_tokens=400, temperature=0.2)
        if isinstance(out, str) and out.strip():
            return _no_em_dash(out.strip())
    except (RuntimeError, Exception):  # noqa: BLE001
        pass
    return "I prepared what I could. Nothing is saved until you approve it."


def run_agent(user_text: str, now: _dt.datetime, client_hint: str | None = None) -> dict:
    """Run the agent for one open-ended request. Fresh context every call.

    Returns {"final_text": str, "proposals": list[dict], "transcript": list[dict]}.
    Raises AgentUnavailable when the model server cannot be reached.
    """
    system = _load_system_prompt(now)
    user = _no_em_dash(str(user_text or "").strip())
    if client_hint:
        user += f"\n\n(Context: the therapist currently has client {client_hint} selected.)"

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    proposals: list[dict] = []
    transcript: list[dict] = []
    seen_calls: set[str] = set()
    arg_retries: dict[str, int] = {}
    final_text = ""

    for _turn in range(AGENT_MAX_TURNS):
        try:
            message = llm.chat_tools(messages, TOOLS)
        except RuntimeError as exc:
            raise AgentUnavailable(f"Agent model call failed: {exc}") from exc

        tool_calls = message.get("tool_calls") or []
        # Always append the assistant message so tool results stay well-formed.
        messages.append(message)

        if not tool_calls:
            final_text = _no_em_dash((message.get("content") or "").strip())
            transcript.append({"step": "final", "text": final_text})
            break

        for call in tool_calls:
            call_id = call.get("id") or ""
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments")

            # Unknown tool.
            if name not in _KNOWN_TOOLS:
                result = json.dumps(
                    {
                        "error": (
                            f"unknown tool {name!r}. Use only: "
                            f"{', '.join(sorted(_KNOWN_TOOLS))}."
                        )
                    }
                )
                transcript.append({"step": "tool", "name": name, "error": "unknown tool"})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                continue

            # Parse arguments (may be a JSON string or already a dict).
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (ValueError, TypeError):
                    count = arg_retries.get(name, 0) + 1
                    arg_retries[name] = count
                    if count > _MAX_ARG_RETRIES:
                        result = json.dumps(
                            {
                                "error": (
                                    f"arguments for {name} were not valid JSON "
                                    f"{count} times. Skip this tool or finish."
                                )
                            }
                        )
                    else:
                        result = json.dumps(
                            {
                                "error": (
                                    f"the arguments for {name} were not valid JSON. "
                                    "Call it again with a valid JSON object."
                                )
                            }
                        )
                    transcript.append({"step": "tool", "name": name, "error": "bad json args"})
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": result}
                    )
                    continue

            # Repeat-call guard (same tool, same normalized args).
            fingerprint = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if fingerprint in seen_calls:
                result = json.dumps(
                    {
                        "note": (
                            f"You already called {name} with these exact arguments "
                            "and have the result. Do not repeat it. Use what you have "
                            "or finish with a final message."
                        )
                    }
                )
                transcript.append({"step": "tool", "name": name, "note": "repeat nudge"})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                continue
            seen_calls.add(fingerprint)

            # Dispatch.
            if name in _READ_TOOLS:
                result = _exec_read_tool(name, args)
            elif name == "create_worksheet":
                result = _stage_worksheet(args, proposals)
            elif name == "draft_email":
                result = _stage_email(args, proposals)
            else:  # defensive; unreachable given the guard above
                result = json.dumps({"error": f"unhandled tool {name!r}"})

            transcript.append({"step": "tool", "name": name, "args": args})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
    else:
        # Turn cap reached without a tool-free final message.
        final_text = _forced_finalize(messages)
        transcript.append({"step": "final", "text": final_text, "forced": True})

    if not final_text:
        final_text = _forced_finalize(messages)

    return {"final_text": final_text, "proposals": proposals, "transcript": transcript}
