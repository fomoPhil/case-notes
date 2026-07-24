"""Tests for debrief.settings_store. Temp-dir based: never touches the real vault.

Paths derive from config.VAULT_DIR at call time, so repointing the vault (as the
vault tests do) repoints the settings store too.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the settings store at a temp vault and return the module."""
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")

    import debrief.settings_store as settings_store

    return settings_store


def test_ensure_settings_scaffolds(store):
    store.ensure_settings()
    base = store.settings_dir()
    assert (base / "settings.json").is_file()
    assert (base / "dictionary.md").is_file()
    assert (base / "formats").is_dir()
    assert (base / "profile").is_dir()


def test_ensure_settings_idempotent(store):
    store.ensure_settings()
    path = store.settings_dir() / "settings.json"
    before = path.read_text(encoding="utf-8")
    # A user edit must survive a second scaffold.
    store.save({"note_format": "SOAP"})
    store.ensure_settings()
    after = path.read_text(encoding="utf-8")
    assert '"SOAP"' in after
    assert before != after  # scaffold did not clobber the saved change


def test_defaults_when_no_file(store):
    settings = store.load()
    assert settings["profession"] == "therapy"
    assert settings["note_format"] == "DAP"
    assert settings["stt_engine"] == "parakeet"
    assert settings["features"] == {
        "calendar": True,
        "email": True,
        "verify": True,
        "assistant": True,
    }
    assert settings["version"] == 1


def test_save_and_load_round_trip(store):
    store.ensure_settings()
    store.save({"note_format": "SOAP", "features": {"verify": False}})
    settings = store.load()
    assert settings["note_format"] == "SOAP"
    # A nested patch backfills the untouched feature flags from DEFAULTS.
    assert settings["features"] == {
        "calendar": True,
        "email": True,
        "verify": False,
        "assistant": True,
    }


def test_partial_file_backfills_from_defaults(store):
    store.ensure_settings()
    # Write a file missing the features block entirely.
    path = store.settings_dir() / "settings.json"
    path.write_text(json.dumps({"profession": "slp"}), encoding="utf-8")
    settings = store.load()
    assert settings["profession"] == "slp"
    assert settings["features"]["assistant"] is True
    assert settings["stt_engine"] == "parakeet"


def test_malformed_json_falls_back_to_defaults(store):
    store.ensure_settings()
    path = store.settings_dir() / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    settings = store.load()
    assert settings["profession"] == "therapy"
    assert settings["note_format"] == "DAP"


def test_env_override_wins(store, monkeypatch):
    store.ensure_settings()
    store.save({"profession": "therapy"})
    monkeypatch.setenv("DEBRIEF_PROFESSION", "coaching")
    monkeypatch.setenv("DEBRIEF_STT_ENGINE", "mlx-whisper")
    settings = store.load()
    assert settings["profession"] == "coaching"
    assert settings["stt_engine"] == "mlx-whisper"


def test_dictionary_round_trip(store):
    store.ensure_settings()
    assert store.read_dictionary() == ""
    store.write_dictionary("Zoloft is sertraline\nMLU means mean length of utterance")
    assert "sertraline" in store.read_dictionary()


def test_validate_patch_rejects_bad_values(store):
    # Profession is validated at the API layer against the vocab registry; the
    # store validates note_format and stt_engine only.
    for bad in (
        {"note_format": "HAIKU"},
        {"stt_engine": "ears"},
    ):
        with pytest.raises(ValueError):
            store.validate_patch(bad)


def test_validate_patch_accepts_good_values(store):
    store.validate_patch({"note_format": "SOAP", "stt_engine": "mlx-whisper"})
