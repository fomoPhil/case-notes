"""Shared configuration for Debrief.

Everything is local. No secrets, no network beyond localhost LM Studio.

All values are plain module constants (other modules import these names at
import time). Each one can be overridden with an environment variable so a
different vault location, model server, model, calendar, or session length can
be set without editing code.
"""

import os
from pathlib import Path

# Repo root = parent of the debrief/ package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    """Read an environment variable as a filesystem path, else the default."""
    raw = os.environ.get(name)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return default


def _env_str(name: str, default: str) -> str:
    """Read an environment variable as a trimmed string, else the default."""
    raw = os.environ.get(name)
    if raw and raw.strip():
        return raw.strip()
    return default


def _env_int(name: str, default: int) -> int:
    """Read an environment variable as an int, falling back on parse failure."""
    raw = os.environ.get(name)
    if raw and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


# Vault lives at ./DebriefVault by default (scaffolded by vault.py). Override
# with DEBRIEF_VAULT_DIR to point at any location.
VAULT_DIR: Path = _env_path("DEBRIEF_VAULT_DIR", REPO_ROOT / "DebriefVault")

# Local model server, OpenAI-compatible. Base URL (no trailing slash). LM Studio
# defaults to http://localhost:1234/v1. Override with DEBRIEF_MODEL_URL.
MODEL_BASE_URL: str = _env_str("DEBRIEF_MODEL_URL", "http://localhost:1234/v1").rstrip("/")

# The chat-completions endpoint, derived so debrief/llm.py keeps working.
LMSTUDIO_URL: str = f"{MODEL_BASE_URL}/chat/completions"

# Model id to request. Override with DEBRIEF_MODEL.
MODEL: str = _env_str("DEBRIEF_MODEL", "gemma-4-12b-it-qat")

# Dedicated macOS calendar. Created if missing; we never touch other calendars.
CALENDAR_NAME: str = _env_str("DEBRIEF_CALENDAR_NAME", "Debrief")

# Default follow-up appointment length when the therapist does not say one.
DEFAULT_SESSION_MINUTES: int = _env_int("DEBRIEF_SESSION_MINUTES", 50)

# Max tool-calling turns the in-app agent may take before a forced finalize.
AGENT_MAX_TURNS: int = _env_int("DEBRIEF_AGENT_MAX_TURNS", 12)
