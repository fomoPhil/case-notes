"""Tests for debrief.models detection. HTTP is monkeypatched, never live."""

from __future__ import annotations

import requests

import debrief.models as models


class _FakeResp:
    def __init__(self, payload, status=200, raise_exc=None):
        self._payload = payload
        self.status_code = status
        self._raise_exc = raise_exc

    def json(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._payload


def _make_get(lmstudio=None, ollama=None):
    """Build a fake requests.get keyed on which server URL is probed.

    Each of lmstudio/ollama is a _FakeResp, or an Exception to raise, or None
    to simulate an unreachable server (connection error).
    """
    def fake_get(url, timeout=None):
        if "1234" in url:
            target = lmstudio
        elif "11434" in url:
            target = ollama
        else:
            raise AssertionError(f"unexpected probe url {url}")
        if target is None:
            raise requests.ConnectionError("refused")
        if isinstance(target, Exception):
            raise target
        return target
    return fake_get


_LMSTUDIO_OK = {"data": [{"id": "gemma-4-12b-it-qat"}, {"id": "some-embed-model"}]}
_LMSTUDIO_NO_GEMMA = {"data": [{"id": "llama-3-8b"}, {"id": "some-embed-model"}]}
_OLLAMA_OK = {"models": [{"name": "gemma2:latest"}, {"name": "mistral:latest"}]}


def test_lmstudio_only(monkeypatch):
    monkeypatch.setattr(requests, "get", _make_get(lmstudio=_FakeResp(_LMSTUDIO_OK)))
    servers = models.detect_servers()
    lm = servers[0]
    ol = servers[1]
    assert lm["provider"] == "lmstudio"
    assert lm["reachable"] is True
    assert lm["gemma_model"] == "gemma-4-12b-it-qat"
    assert ol["provider"] == "ollama"
    assert ol["reachable"] is False
    picked = models.pick_gemma()
    assert picked == {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"}


def test_ollama_only(monkeypatch):
    monkeypatch.setattr(requests, "get", _make_get(ollama=_FakeResp(_OLLAMA_OK)))
    servers = models.detect_servers()
    lm, ol = servers
    assert lm["reachable"] is False
    assert ol["reachable"] is True
    assert ol["gemma_model"] == "gemma2:latest"
    # Ollama gemma is reported but never selected (agent support deferred).
    assert models.pick_gemma() is None


def test_both_up(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        _make_get(lmstudio=_FakeResp(_LMSTUDIO_OK), ollama=_FakeResp(_OLLAMA_OK)),
    )
    servers = models.detect_servers()
    assert all(s["reachable"] for s in servers)
    # LM Studio preferred.
    assert models.pick_gemma()["base_url"] == "http://localhost:1234/v1"


def test_both_down(monkeypatch):
    monkeypatch.setattr(requests, "get", _make_get())
    servers = models.detect_servers()
    assert not any(s["reachable"] for s in servers)
    assert models.pick_gemma() is None


def test_gemma_absent(monkeypatch):
    monkeypatch.setattr(
        requests, "get", _make_get(lmstudio=_FakeResp(_LMSTUDIO_NO_GEMMA))
    )
    servers = models.detect_servers()
    lm = servers[0]
    assert lm["reachable"] is True
    assert lm["gemma_model"] is None
    assert models.pick_gemma() is None
