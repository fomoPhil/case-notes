"""Obsidian deep links must fire only when Obsidian has this vault registered.

Regression for the stacked "Vault not found" dialogs: execute_plan opened
obsidian:// URIs unconditionally, so any run against an unregistered vault
(temp vaults, renamed vaults, machines without Obsidian) spammed error
dialogs. The URI is still built and returned for display either way.
"""

import json
import subprocess

from debrief import vault


def _write_config(tmp_path, vault_paths):
    cfg = tmp_path / "obsidian.json"
    cfg.write_text(
        json.dumps({"vaults": {f"id{i}": {"path": str(p)} for i, p in enumerate(vault_paths)}}),
        encoding="utf-8",
    )
    return cfg


def test_available_false_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_OBSIDIAN_CONFIG", tmp_path / "nope.json")
    assert vault.obsidian_available() is False


def test_available_false_when_vault_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_OBSIDIAN_CONFIG", _write_config(tmp_path, [tmp_path / "other"]))
    assert vault.obsidian_available() is False


def test_available_true_when_vault_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_OBSIDIAN_CONFIG", _write_config(tmp_path, [vault.VAULT_DIR]))
    assert vault.obsidian_available() is True


def test_open_skipped_for_unregistered_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_OBSIDIAN_CONFIG", _write_config(tmp_path, [tmp_path / "other"]))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    uri = vault.obsidian_open_uri(vault.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md")
    assert calls == [], "must not launch obsidian:// for an unregistered vault"
    assert uri.startswith("obsidian://open?vault=")


def test_open_fires_for_registered_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_OBSIDIAN_CONFIG", _write_config(tmp_path, [vault.VAULT_DIR]))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    uri = vault.obsidian_open_uri(vault.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md")
    assert len(calls) == 1 and calls[0][0][1] == uri
