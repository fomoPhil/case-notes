"""Live agent tests (require LM Studio + gemma loaded).

Run with:  DEBRIEF_MODEL=google/gemma-4-12b-it-qat .venv/bin/pytest -m live -q
These are skipped by the default `-m "not live"` selection.
"""

from __future__ import annotations

import datetime as dt

import pytest

from debrief import agent, llm

pytestmark = pytest.mark.live

NOW = dt.datetime(2026, 7, 23, 10, 0, 0)


def test_chat_tools_returns_native_tool_calls():
    """The loaded gemma emits native OpenAI tool_calls under LM Studio."""
    messages = [
        {"role": "system", "content": "You help by calling tools. Always use a tool when one fits."},
        {"role": "user", "content": "List the clients on file."},
    ]
    tools = [t for t in agent.TOOLS if t["function"]["name"] == "list_clients"]
    msg = llm.chat_tools(messages, tools)
    assert isinstance(msg, dict)
    assert msg.get("tool_calls"), f"expected tool_calls, got: {msg}"
    assert msg["tool_calls"][0]["function"]["name"] == "list_clients"


def test_run_agent_makes_worksheet_proposal():
    out = agent.run_agent(
        "Make a one page box breathing worksheet for before meetings.", NOW
    )
    worksheets = [p for p in out["proposals"] if p["type"] == "worksheet"]
    assert worksheets, f"expected a worksheet proposal, got: {out['proposals']}"
    body = worksheets[0]["markdown_body"]
    assert isinstance(body, str) and len(body.strip()) > 40
    assert "—" not in body
