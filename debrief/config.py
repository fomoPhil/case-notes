"""Shared configuration for Debrief.

Everything is local. No secrets, no network beyond localhost LM Studio.
"""

from pathlib import Path

# Repo root = parent of the debrief/ package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Obsidian vault lives at ./vault (scaffolded by vault.py).
VAULT_DIR: Path = REPO_ROOT / "vault"

# Local LM Studio OpenAI-compatible endpoint. Model is loaded and running.
LMSTUDIO_URL: str = "http://localhost:1234/v1/chat/completions"
MODEL: str = "gemma-4-12b-it-qat"

# Dedicated macOS calendar. Created if missing; we never touch other calendars.
CALENDAR_NAME: str = "Debrief"

# Default follow-up appointment length when the therapist does not say one.
DEFAULT_SESSION_MINUTES: int = 50
