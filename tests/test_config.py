"""Tests for debrief.config env-var overrides. Reloads the module per case."""

from __future__ import annotations

import importlib
from pathlib import Path

import debrief.config as config


def _reload():
    return importlib.reload(config)


def test_defaults(monkeypatch):
    for var in (
        "DEBRIEF_VAULT_DIR",
        "DEBRIEF_MODEL_URL",
        "DEBRIEF_MODEL",
        "DEBRIEF_CALENDAR_NAME",
        "DEBRIEF_SESSION_MINUTES",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = _reload()
    try:
        assert cfg.MODEL_BASE_URL == "http://localhost:1234/v1"
        assert cfg.LMSTUDIO_URL == "http://localhost:1234/v1/chat/completions"
        assert cfg.MODEL == "gemma-4-12b-it-qat"
        assert cfg.CALENDAR_NAME == "Debrief"
        assert cfg.DEFAULT_SESSION_MINUTES == 50
        assert cfg.VAULT_DIR == cfg.REPO_ROOT / "DebriefVault"
    finally:
        _reload()  # restore defaults for other tests


def test_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DEBRIEF_VAULT_DIR", str(tmp_path / "myvault"))
    monkeypatch.setenv("DEBRIEF_MODEL_URL", "http://localhost:9999/v1/")
    monkeypatch.setenv("DEBRIEF_MODEL", "gemma-custom")
    monkeypatch.setenv("DEBRIEF_CALENDAR_NAME", "Work")
    monkeypatch.setenv("DEBRIEF_SESSION_MINUTES", "30")
    cfg = _reload()
    try:
        assert cfg.VAULT_DIR == Path(str(tmp_path / "myvault"))
        # Trailing slash stripped, chat endpoint derived from the base.
        assert cfg.MODEL_BASE_URL == "http://localhost:9999/v1"
        assert cfg.LMSTUDIO_URL == "http://localhost:9999/v1/chat/completions"
        assert cfg.MODEL == "gemma-custom"
        assert cfg.CALENDAR_NAME == "Work"
        assert cfg.DEFAULT_SESSION_MINUTES == 30
    finally:
        _reload()


def test_bad_int_falls_back(monkeypatch):
    monkeypatch.setenv("DEBRIEF_SESSION_MINUTES", "not-a-number")
    cfg = _reload()
    try:
        assert cfg.DEFAULT_SESSION_MINUTES == 50
    finally:
        _reload()
