"""Core guardrail tests against the real debrief package.

Three invariants the system must never lose:
  1. The action space is a closed enum: the model can only propose the two
     whitelisted external actions, nothing else.
  2. Datetime resolution is deterministic Python, never model output.
  3. An unknown client is rejected, not silently invented.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from debrief.dates import resolve_utterance
from debrief.extract import EXTRACT_SCHEMA


def test_action_space_is_a_closed_enum():
    """The model cannot propose external actions beyond the whitelist."""
    action_type = EXTRACT_SCHEMA["properties"]["actions"]["items"]["properties"]["type"]
    assert action_type["enum"] == ["schedule_followup", "draft_client_email"]


def test_datetime_resolution_is_not_model_owned():
    """Spoken time phrases resolve through deterministic Python code."""
    # Anchor: Saturday, July 18 2026, 9:00 (matches the contract in dates.py).
    now = datetime(2026, 7, 18, 9, 0)
    assert resolve_utterance("next Tuesday at 3", now) == datetime(2026, 7, 21, 15, 0)
    assert resolve_utterance("tomorrow at 10", now) == datetime(2026, 7, 19, 10, 0)
    assert resolve_utterance("complete gibberish phrase", now) is None


def test_unknown_client_is_rejected(tmp_path, monkeypatch):
    """client_context must raise for a client id that does not exist."""
    import debrief.config as config
    import debrief.vault as vault_mod

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path / "vault")
    vault_mod.ensure_vault()

    with pytest.raises(FileNotFoundError):
        vault_mod.client_context("not-a-client")
