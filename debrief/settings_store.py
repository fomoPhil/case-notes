"""Persistent user settings: the _Settings/ backbone under the vault.

Everything Debrief remembers between runs lives here, alongside the client
record it belongs to:

    <vault>/_Settings/
        settings.json      profession, note format, feature toggles, STT engine
        dictionary.md      the user's custom correction dictionary (free text)
        formats/           custom format specs (Phase C)
        profile/           compiled prompt layers (Phase E)

No settings persistence existed before this: the app read env vars only. Reads
are layered so a partial or missing file never crashes a caller:

    DEFAULTS  <  settings.json (deep-merged, file wins)  <  environment overrides

A malformed settings.json falls back to DEFAULTS rather than raising. All writes
are atomic (temp file + os.replace), reusing the vault helper. Paths derive from
config.VAULT_DIR at call time so a test that repoints the vault also repoints the
settings store. No em dashes anywhere.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import config
from .vault import _atomic_write

# ---------------------------------------------------------------------------
# Defaults + validation vocabulary
# ---------------------------------------------------------------------------

# The full default settings object. Every stored file is deep-merged over a copy
# of this, so a new key added here is backfilled onto older vaults automatically.
DEFAULTS: dict = {
    "profession": "therapy",
    "note_format": "DAP",
    "features": {
        "calendar": True,
        "email": True,
        "verify": True,
        "assistant": True,
    },
    "stt_engine": "parakeet",
    "version": 1,
}

# Valid STT engine ids. Phase D adds the mlx-whisper adapter; the id is accepted
# now so a saved choice survives across that phase.
ALLOWED_STT_ENGINES = {"parakeet", "mlx-whisper"}

# Valid note format ids. Phase C promotes this to the format registry; the launch
# set is accepted now so a saved choice validates.
ALLOWED_NOTE_FORMATS = {"DAP", "SOAP", "GROW", "meeting-memo"}

# Top-level settings keys that an environment variable overrides when it is set.
# GEMINI_MODEL is deliberately absent: it is a model id, not a stored setting.
_ENV_OVERRIDES = {
    "profession": "DEBRIEF_PROFESSION",
    "note_format": "DEBRIEF_NOTE_FORMAT",
    "stt_engine": "DEBRIEF_STT_ENGINE",
}

# The dictionary.md file is seeded empty so no placeholder text ever leaks into a
# correction prompt. The correction layer is appended only when it has content.
_DEFAULT_DICTIONARY = ""


# ---------------------------------------------------------------------------
# Paths (derived from the live vault dir at call time)
# ---------------------------------------------------------------------------


def settings_dir() -> Path:
    """The _Settings directory for the currently configured vault."""
    return config.VAULT_DIR / "_Settings"


def _settings_path() -> Path:
    return settings_dir() / "settings.json"


def _dictionary_path() -> Path:
    return settings_dir() / "dictionary.md"


def formats_dir() -> Path:
    return settings_dir() / "formats"


def profile_dir() -> Path:
    return settings_dir() / "profile"


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base with override applied recursively (override wins on leaves)."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_settings_file() -> dict:
    """Return the raw stored settings dict, or {} on missing/malformed/non-dict."""
    path = _settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _env_overrides() -> dict:
    """Top-level overrides for any env var in _ENV_OVERRIDES that is set."""
    out: dict = {}
    for key, env_name in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw and raw.strip():
            out[key] = raw.strip()
    return out


# ---------------------------------------------------------------------------
# Public read / write API
# ---------------------------------------------------------------------------


def load() -> dict:
    """Return the effective settings: DEFAULTS < file < environment overrides.

    Never raises: a missing or malformed settings.json falls back to DEFAULTS.
    """
    settings = copy.deepcopy(DEFAULTS)
    settings = _deep_merge(settings, _read_settings_file())
    settings = _deep_merge(settings, _env_overrides())
    return settings


def save(patch: dict) -> dict:
    """Deep-merge patch onto the stored settings and write atomically.

    The patch is merged onto (DEFAULTS < stored file), never onto the environment
    overrides, so an env var never gets baked into the persisted file. Returns the
    updated stored settings.
    """
    current = _deep_merge(copy.deepcopy(DEFAULTS), _read_settings_file())
    updated = _deep_merge(current, patch or {})
    _atomic_write(_settings_path(), json.dumps(updated, indent=2) + "\n")
    return updated


def read_dictionary() -> str:
    """Return the user's correction dictionary text, or "" when absent."""
    try:
        return _dictionary_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def write_dictionary(text: str) -> None:
    """Persist the user's correction dictionary text atomically."""
    _atomic_write(_dictionary_path(), text or "")


def validate_patch(patch: dict) -> None:
    """Raise ValueError when a settings patch carries an invalid known value.

    Validates note_format and stt_engine here (no external dependencies).
    Profession is validated at the API layer against the vocab registry so this
    store stays decoupled from the profession packs. Unknown keys pass through so
    future settings do not need a code change here.
    """
    if not isinstance(patch, dict):
        raise ValueError("settings patch must be an object")

    if "note_format" in patch and patch["note_format"] not in ALLOWED_NOTE_FORMATS:
        raise ValueError(f"unknown note_format: {patch['note_format']!r}")

    if "stt_engine" in patch and patch["stt_engine"] not in ALLOWED_STT_ENGINES:
        raise ValueError(f"unknown stt_engine: {patch['stt_engine']!r}")


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


def ensure_settings() -> None:
    """Create the _Settings scaffold if it does not exist. Idempotent.

    Called from vault.ensure_vault so a fresh vault boots with a settings file,
    an (empty) dictionary, and the formats/ and profile/ folders ready.
    """
    base = settings_dir()
    base.mkdir(parents=True, exist_ok=True)
    formats_dir().mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)

    if not _settings_path().exists():
        _atomic_write(_settings_path(), json.dumps(DEFAULTS, indent=2) + "\n")
    if not _dictionary_path().exists():
        _atomic_write(_dictionary_path(), _DEFAULT_DICTIONARY)
