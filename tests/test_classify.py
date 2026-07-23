"""Tests for debrief.classify (llm.chat mocked)."""

from __future__ import annotations

from debrief import classify


def test_session_debrief_route(monkeypatch):
    monkeypatch.setattr(
        classify.llm, "chat",
        lambda *a, **k: {"route": "session_debrief", "client_hint": "Bob"},
    )
    out = classify.classify("Today Bob reported a hard week and we practiced restructuring.", True)
    assert out["route"] == "session_debrief"
    assert out["client_hint"] == "Bob"


def test_assistant_route(monkeypatch):
    monkeypatch.setattr(
        classify.llm, "chat",
        lambda *a, **k: {"route": "assistant", "client_hint": ""},
    )
    out = classify.classify("Make a box breathing worksheet.", False)
    assert out["route"] == "assistant"
    assert out["client_hint"] == ""


def test_empty_transcript_defaults_to_assistant(monkeypatch):
    called = {"n": 0}

    def _chat(*a, **k):
        called["n"] += 1
        return {"route": "session_debrief", "client_hint": ""}

    monkeypatch.setattr(classify.llm, "chat", _chat)
    out = classify.classify("   ", True)
    assert out["route"] == "assistant"
    assert called["n"] == 0  # short-circuits without a model call


def test_model_failure_defaults_to_assistant(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(classify.llm, "chat", _boom)
    out = classify.classify("Anything at all.", True)
    assert out["route"] == "assistant"


def test_bad_route_value_coerced(monkeypatch):
    monkeypatch.setattr(
        classify.llm, "chat",
        lambda *a, **k: {"route": "nonsense", "client_hint": "X"},
    )
    out = classify.classify("something", False)
    assert out["route"] == "assistant"
