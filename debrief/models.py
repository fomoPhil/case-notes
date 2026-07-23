"""Model server auto-detection for Debrief.

Probes the two local model servers Debrief knows about, without any
configuration:

  - LM Studio, OpenAI-compatible, at http://localhost:1234/v1/models
  - Ollama at http://localhost:11434/api/tags

detect_servers() reports what is reachable and which gemma model each one has.
pick_gemma() chooses the one to use, preferring LM Studio because the in-app
agent only speaks LM Studio's tool-call dialect today (Ollama support is
deferred). Detection still REPORTS an Ollama gemma so the UI and doctor can tell
the user it exists.
"""

from __future__ import annotations

import requests

# Short probe timeout: a missing server should fail fast, not hang the UI.
_PROBE_TIMEOUT = 1.5

_LMSTUDIO_BASE = "http://localhost:1234/v1"
_OLLAMA_BASE = "http://localhost:11434"


def _first_gemma(model_ids: list[str]) -> str | None:
    """Return the first model id containing 'gemma' (case-insensitive)."""
    for mid in model_ids:
        if "gemma" in mid.lower():
            return mid
    return None


def _probe_lmstudio() -> dict:
    """Probe LM Studio's OpenAI-compatible /v1/models endpoint."""
    server = {
        "provider": "lmstudio",
        "base_url": _LMSTUDIO_BASE,
        "reachable": False,
        "models": [],
        "gemma_model": None,
    }
    try:
        resp = requests.get(f"{_LMSTUDIO_BASE}/models", timeout=_PROBE_TIMEOUT)
    except requests.RequestException:
        return server
    if resp.status_code != 200:
        return server
    try:
        data = resp.json()
    except ValueError:
        return server
    models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    server["reachable"] = True
    server["models"] = models
    server["gemma_model"] = _first_gemma(models)
    return server


def _probe_ollama() -> dict:
    """Probe Ollama's /api/tags endpoint."""
    server = {
        "provider": "ollama",
        "base_url": _OLLAMA_BASE,
        "reachable": False,
        "models": [],
        "gemma_model": None,
    }
    try:
        resp = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=_PROBE_TIMEOUT)
    except requests.RequestException:
        return server
    if resp.status_code != 200:
        return server
    try:
        data = resp.json()
    except ValueError:
        return server
    models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    server["reachable"] = True
    server["models"] = models
    server["gemma_model"] = _first_gemma(models)
    return server


def detect_servers() -> list[dict]:
    """Probe both known local model servers.

    Returns a list of dicts, one per provider, each with:
      provider, base_url, reachable, models (list[str]), gemma_model (str|None).
    LM Studio is listed first.
    """
    return [_probe_lmstudio(), _probe_ollama()]


def pick_gemma() -> dict | None:
    """Pick a reachable gemma model to drive the agent, preferring LM Studio.

    Returns {"base_url", "model"} or None if no reachable server has a gemma.
    Only LM Studio is selectable today (agent tool-call support). An Ollama
    gemma is detected and reported by detect_servers() but not returned here.
    """
    for server in detect_servers():
        if (
            server["provider"] == "lmstudio"
            and server["reachable"]
            and server["gemma_model"]
        ):
            return {"base_url": server["base_url"], "model": server["gemma_model"]}
    return None
