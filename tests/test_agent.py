"""Scripted tests for debrief.agent.run_agent (no live model)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from debrief import agent


NOW = dt.datetime(2026, 7, 23, 10, 0, 0)


def _tool_msg(calls):
    """Build an assistant message carrying tool_calls.

    calls: list of (name, args_obj_or_raw_string, id).
    """
    tool_calls = []
    for name, args, cid in calls:
        raw = args if isinstance(args, str) else json.dumps(args)
        tool_calls.append(
            {"id": cid, "type": "function", "function": {"name": name, "arguments": raw}}
        )
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def _final_msg(text):
    return {"role": "assistant", "content": text, "tool_calls": []}


class _Script:
    """A scripted chat_tools: returns queued messages in order."""

    def __init__(self, messages):
        self._queue = list(messages)
        self.calls = 0

    def __call__(self, messages, tools, **kwargs):
        self.calls += 1
        if self._queue:
            return self._queue.pop(0)
        return _final_msg("Done.")


@pytest.fixture(autouse=True)
def _no_pdf(monkeypatch):
    # Keep worksheet staging hermetic and fast (no real PDF render).
    monkeypatch.setattr(agent.render, "pdf_available", lambda: False)


def test_happy_path_two_tool_calls_then_final(monkeypatch):
    monkeypatch.setattr(agent.vault, "list_clients", lambda: [
        {"client_id": "C-0001", "name": "Bob Smith", "framework": "CBT"}
    ])
    script = _Script([
        _tool_msg([("list_clients", {}, "c1")]),
        _tool_msg([("create_worksheet", {"title": "Box Breathing", "markdown_body": "Breathe."}, "c2")]),
        _final_msg("I prepared a box breathing worksheet. Nothing is saved until you approve."),
    ])
    monkeypatch.setattr(agent.llm, "chat_tools", script)

    out = agent.run_agent("make a box breathing worksheet", NOW)
    assert "box breathing" in out["final_text"].lower()
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    assert p["type"] == "worksheet"
    assert p["title"] == "Box Breathing"
    assert p["preview_pdf"] is None


def test_malformed_args_retry(monkeypatch):
    script = _Script([
        _tool_msg([("create_worksheet", "{not valid json", "c1")]),
        _tool_msg([("create_worksheet", {"title": "T", "markdown_body": "Body."}, "c2")]),
        _final_msg("Prepared."),
    ])
    monkeypatch.setattr(agent.llm, "chat_tools", script)

    out = agent.run_agent("make a worksheet", NOW)
    # The retry recovered: one worksheet proposal, and a bad-json step recorded.
    assert len(out["proposals"]) == 1
    assert any(t.get("error") == "bad json args" for t in out["transcript"])


def test_unknown_tool_returns_tool_error(monkeypatch):
    script = _Script([
        _tool_msg([("delete_everything", {}, "c1")]),
        _final_msg("I cannot do that."),
    ])
    monkeypatch.setattr(agent.llm, "chat_tools", script)

    out = agent.run_agent("delete the vault", NOW)
    assert out["proposals"] == []
    assert any(t.get("error") == "unknown tool" for t in out["transcript"])


def test_repeat_call_nudge(monkeypatch):
    monkeypatch.setattr(agent.vault, "list_clients", lambda: [])
    script = _Script([
        _tool_msg([("list_clients", {}, "c1")]),
        _tool_msg([("list_clients", {}, "c2")]),  # identical -> nudge
        _final_msg("No clients on file."),
    ])
    monkeypatch.setattr(agent.llm, "chat_tools", script)

    out = agent.run_agent("who are my clients", NOW)
    assert any(t.get("note") == "repeat nudge" for t in out["transcript"])


def test_max_turns_forced_finalize(monkeypatch):
    # Always return a tool call so the loop never finishes on its own.
    def _always_tool(messages, tools, **kwargs):
        return _tool_msg([("list_clients", {"n": _always_tool.i}, f"c{_always_tool.i}")])
    _always_tool.i = 0

    def _wrapped(messages, tools, **kwargs):
        _always_tool.i += 1
        return _tool_msg([("list_clients", {"n": _always_tool.i}, f"c{_always_tool.i}")])

    monkeypatch.setattr(agent.vault, "list_clients", lambda: [])
    monkeypatch.setattr(agent.llm, "chat_tools", _wrapped)
    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: "Forced summary. Nothing saved until you approve.")

    out = agent.run_agent("loop forever", NOW)
    assert "Forced summary" in out["final_text"]
    assert out["transcript"][-1].get("forced") is True


def test_proposals_accumulate(monkeypatch):
    script = _Script([
        _tool_msg([("create_worksheet", {"title": "One", "markdown_body": "A."}, "c1")]),
        _tool_msg([("create_worksheet", {"title": "Two", "markdown_body": "B."}, "c2")]),
        _tool_msg([("draft_email", {"client_id": "C-0001", "subject": "Hi", "body": "Hello", "attach_worksheet": True}, "c3")]),
        _final_msg("Prepared two worksheets and an email."),
    ])
    monkeypatch.setattr(agent.llm, "chat_tools", script)

    out = agent.run_agent("make two worksheets and email Bob", NOW)
    types = [p["type"] for p in out["proposals"]]
    assert types == ["worksheet", "worksheet", "email"]
    assert out["proposals"][2]["attach_worksheet"] is True


def test_agent_unavailable_when_transport_fails(monkeypatch):
    def _boom(messages, tools, **kwargs):
        raise RuntimeError("LM Studio request failed: connection refused")
    monkeypatch.setattr(agent.llm, "chat_tools", _boom)

    with pytest.raises(agent.AgentUnavailable) as exc:
        agent.run_agent("anything", NOW)
    assert exc.value.hint
