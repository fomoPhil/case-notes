"""Tests for debrief.recstore: the Recordings/ storage layer.

Everything runs against a scratch vault in tmp_path. The real DebriefVault at the
repo root is never touched: recstore reads config.VAULT_DIR at call time, so
repointing config (plus vault and records, which bind VAULT_DIR at import) is the
whole fixture.

The load-bearing tests here are the destructive ones. sweep_retention deletes
audio without asking, so it gets three tests: it removes only expired audio, it
does nothing when retention is off, and unreadable provenance means skip rather
than delete.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

import debrief.config as config
import debrief.records as records_mod
import debrief.recstore as recstore
import debrief.settings_store as settings_store
import debrief.vault as vault_mod

SID = "2026-07-25-1430"


@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)  # records.py binds it at import
    vault_mod.ensure_vault()
    return vault_dir


def _seed_source(client_id, source_id, captured_at=None, audio_ext=".m4a", tmp_path=None):
    """Create a full source: audio, rungs 0 and 1, and meta."""
    recstore.write_rung(client_id, source_id, 0, "raw transcript\n")
    recstore.write_rung(client_id, source_id, 1, "corrected transcript\n")
    if audio_ext:
        src = tmp_path / f"incoming{audio_ext}"
        src.write_bytes(b"fake audio bytes")
        recstore.write_audio(client_id, source_id, src)
    meta = {"kind": "uploaded_audio", "verbatim": True, "produced_by": "debrief"}
    if captured_at is not None:
        meta["captured_at"] = captured_at
    recstore.write_meta(client_id, source_id, meta)
    return recstore.source_dir(client_id, source_id)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_source_dir_layout(scratch_vault):
    assert recstore.recordings_root() == scratch_vault / "Recordings"
    d = recstore.source_dir("C-0001", SID)
    assert d == scratch_vault / "Recordings" / "C-0001" / SID
    assert not d.exists(), "source_dir describes a path, it does not create one"
    assert not recstore.recordings_root().exists()


def test_source_dir_rejects_traversal(scratch_vault):
    bad = ("..", "../..", "a/b", "a/../../b", "/etc/passwd", "", ".", ".hidden", "a\\b", None)
    for value in bad:
        with pytest.raises(ValueError):
            recstore.source_dir(value, SID)
        with pytest.raises(ValueError):
            recstore.source_dir("C-0001", value)
    # An id that merely contains dots is fine, as long as it is not traversal.
    ok = recstore.source_dir("C-0001", "2026-07-25-1430.part2")
    assert recstore.recordings_root().resolve() in ok.resolve().parents


# ---------------------------------------------------------------------------
# Writing rungs
# ---------------------------------------------------------------------------


def test_write_rung_atomic(scratch_vault, monkeypatch):
    recstore.write_rung("C-0001", SID, 0, "original transcript\n")
    target = recstore.source_dir("C-0001", SID) / "transcript-raw.md"

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        recstore.write_rung("C-0001", SID, 0, "replacement transcript\n")

    # A reader never sees a half-written file: the old content is intact and the
    # temp file did not survive as litter.
    assert target.read_text(encoding="utf-8") == "original transcript\n"
    assert list(target.parent.glob("*.tmp")) == []
    assert [p.name for p in target.parent.iterdir()] == ["transcript-raw.md"]

    # The next write (real os.replace again) lands.
    recstore.write_rung("C-0001", SID, 0, "replacement transcript\n")
    assert target.read_text(encoding="utf-8") == "replacement transcript\n"


def test_rung2_requires_rendered_md(scratch_vault):
    digest = json.dumps({"summary": "one session"}, indent=2)
    with pytest.raises(ValueError):
        recstore.write_rung("C-0001", SID, 2, digest)
    d = recstore.source_dir("C-0001", SID)
    assert not (d / "digest.json").exists(), "a refused rung 2 writes nothing"
    assert not (d / "digest.md").exists()

    path = recstore.write_rung("C-0001", SID, 2, digest, rendered_md="# Session digest\n")
    assert path.name == "digest.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"summary": "one session"}
    assert (d / "digest.md").read_text(encoding="utf-8") == "# Session digest\n"
    assert recstore.read_rung("C-0001", SID, 2) == digest


def test_rungs_present_map(scratch_vault):
    assert recstore.rungs_present("C-0001", SID) == set()
    assert recstore.read_rung("C-0001", SID, 0) is None

    recstore.write_rung("C-0001", SID, 0, "raw\n")
    assert recstore.rungs_present("C-0001", SID) == {0}
    recstore.write_rung("C-0001", SID, 1, "corrected\n")
    assert recstore.rungs_present("C-0001", SID) == {0, 1}
    recstore.write_rung("C-0001", SID, 2, "{}", rendered_md="# d\n")
    assert recstore.rungs_present("C-0001", SID) == {0, 1, 2}

    assert recstore.read_rung("C-0001", SID, 1) == "corrected\n"
    with pytest.raises(ValueError):
        recstore.write_rung("C-0001", SID, 3, "no such rung")
    with pytest.raises(ValueError):
        recstore.read_rung("C-0001", SID, 3)


def test_meta_roundtrip(scratch_vault, tmp_path):
    assert recstore.read_meta("C-0001", SID) is None
    meta = {
        "kind": "uploaded_audio",
        "verbatim": True,
        "produced_by": "debrief",
        "captured_at": "2026-07-25T14:30:00",
    }
    path = recstore.write_meta("C-0001", SID, meta)
    assert path.name == "meta.json"
    assert recstore.read_meta("C-0001", SID) == meta

    # Source text (a document that enters at rung 2) is its own file.
    text_path = recstore.write_source_text("C-0001", SID, "Clinician's own notes.\n")
    assert text_path.name == "source-text.md"
    assert text_path.read_text(encoding="utf-8") == "Clinician's own notes.\n"

    # Malformed meta reads as absent rather than raising.
    path.write_text("{not json", encoding="utf-8")
    assert recstore.read_meta("C-0001", SID) is None


def test_audio_write_and_path(scratch_vault, tmp_path):
    assert recstore.audio_path("C-0001", SID) is None
    src = tmp_path / "recording.m4a"
    src.write_bytes(b"fake audio bytes")
    dest = recstore.write_audio("C-0001", SID, src)
    assert dest.name == "source.m4a", "the original container is kept"
    assert dest.read_bytes() == b"fake audio bytes"
    assert recstore.audio_path("C-0001", SID) == dest


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_sources_orders_and_filters(scratch_vault, tmp_path):
    _seed_source("C-0001", "2026-07-20-0900", captured_at="2026-07-20T09:00:00", tmp_path=tmp_path)
    _seed_source("C-0001", "2026-07-25-1430", captured_at="2026-07-25T14:30:00", tmp_path=tmp_path)
    _seed_source(
        "C-0002",
        "2026-07-22-1100",
        captured_at="2026-07-22T11:00:00",
        audio_ext=None,
        tmp_path=tmp_path,
    )
    # A folder with rungs but no meta.json is not a source yet: skip it, do not crash.
    recstore.write_rung("C-0001", "2026-07-26-0800", 0, "orphan\n")

    everything = recstore.list_sources()
    assert [s["source_id"] for s in everything] == [
        "2026-07-25-1430",
        "2026-07-22-1100",
        "2026-07-20-0900",
    ], "newest first, across clients"

    newest = everything[0]
    assert newest["client_id"] == "C-0001"
    assert newest["kind"] == "uploaded_audio", "meta keys ride along"
    assert newest["rungs"] == [0, 1]
    assert newest["has_audio"] is True
    assert [s for s in everything if s["source_id"] == "2026-07-22-1100"][0]["has_audio"] is False

    mine = recstore.list_sources("C-0001")
    assert [s["source_id"] for s in mine] == ["2026-07-25-1430", "2026-07-20-0900"]
    assert recstore.list_sources("C-9999") == []


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_delete_audio_keeps_rungs_and_meta(scratch_vault, tmp_path):
    d = _seed_source("C-0001", SID, captured_at="2026-07-25T14:30:00", tmp_path=tmp_path)

    assert recstore.delete_audio("C-0001", SID) is True
    assert recstore.audio_path("C-0001", SID) is None
    assert not (d / "source.m4a").exists()
    assert (d / "transcript-raw.md").read_text(encoding="utf-8") == "raw transcript\n"
    assert (d / "transcript.md").read_text(encoding="utf-8") == "corrected transcript\n"
    assert recstore.read_meta("C-0001", SID)["kind"] == "uploaded_audio"
    assert recstore.rungs_present("C-0001", SID) == {0, 1}

    assert recstore.delete_audio("C-0001", SID) is False, "nothing left to delete"


def test_delete_source_goes_to_trash(scratch_vault, tmp_path):
    d = _seed_source("C-0001", SID, captured_at="2026-07-25T14:30:00", tmp_path=tmp_path)

    assert recstore.delete_source("C-0001", SID) is True
    assert not d.exists()

    buckets = records_mod.list_trash()
    assert [b["original"] for b in buckets] == [f"Recordings/C-0001/{SID}"]
    assert buckets[0]["kind"] == "folder"
    assert buckets[0]["file_count"] == 4, "audio, both transcripts, and meta rode along"

    assert recstore.delete_source("C-0001", SID) is False


def test_delete_client_sources_counts(scratch_vault, tmp_path):
    _seed_source("C-0001", "2026-07-20-0900", captured_at="2026-07-20T09:00:00", tmp_path=tmp_path)
    _seed_source("C-0001", "2026-07-25-1430", captured_at="2026-07-25T14:30:00", tmp_path=tmp_path)
    _seed_source("C-0002", "2026-07-22-1100", captured_at="2026-07-22T11:00:00", tmp_path=tmp_path)

    assert recstore.delete_client_sources("C-0001") == 2
    assert not (recstore.recordings_root() / "C-0001").exists()
    assert recstore.list_sources() == recstore.list_sources("C-0002"), "C-0002 untouched"
    assert len(recstore.list_sources("C-0002")) == 1

    assert [b["original"] for b in records_mod.list_trash()] == ["Recordings/C-0001"]
    assert recstore.delete_client_sources("C-0001") == 0, "already gone"


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 7, 25, 12, 0, 0)


def test_sweep_retention_deletes_only_expired_audio(scratch_vault, tmp_path):
    settings_store.save({"recording_retention_days": 30})
    old_dir = _seed_source(
        "C-0001",
        "2026-06-15-0900",
        captured_at=(_NOW - dt.timedelta(days=40)).isoformat(),
        tmp_path=tmp_path,
    )
    # A timezone-aware captured_at must still be comparable to a naive now, or
    # retention would silently never fire.
    aware_dir = _seed_source(
        "C-0001",
        "2026-06-16-0900",
        captured_at=(_NOW - dt.timedelta(days=40)).astimezone().isoformat(),
        tmp_path=tmp_path,
    )
    fresh_dir = _seed_source(
        "C-0001",
        "2026-07-20-0900",
        captured_at=(_NOW - dt.timedelta(days=5)).isoformat(),
        tmp_path=tmp_path,
    )

    assert recstore.sweep_retention(_NOW) == 2

    assert not (old_dir / "source.m4a").exists(), "expired audio goes"
    assert not (aware_dir / "source.m4a").exists()
    assert (fresh_dir / "source.m4a").exists(), "audio inside the window stays"

    # The evidence trail survives losing the audio. This is the whole point.
    assert (old_dir / "transcript-raw.md").read_text(encoding="utf-8") == "raw transcript\n"
    assert (old_dir / "transcript.md").read_text(encoding="utf-8") == "corrected transcript\n"
    assert recstore.rungs_present("C-0001", "2026-06-15-0900") == {0, 1}

    old_meta = recstore.read_meta("C-0001", "2026-06-15-0900")
    assert old_meta["kind"] == "uploaded_audio", "the rest of the provenance is unchanged"
    assert old_meta["audio_deleted_at"].startswith("2026-07-25T")
    assert "audio_deleted_at" not in recstore.read_meta("C-0001", "2026-07-20-0900")

    # A second sweep finds nothing left to do.
    assert recstore.sweep_retention(_NOW) == 0


def test_sweep_retention_zero_keeps_everything(scratch_vault, tmp_path):
    old_dir = _seed_source(
        "C-0001",
        "2026-06-15-0900",
        captured_at=(_NOW - dt.timedelta(days=400)).isoformat(),
        tmp_path=tmp_path,
    )
    # 0 is the default and it means keep forever.
    assert settings_store.load().get("recording_retention_days", 0) == 0
    assert recstore.sweep_retention(_NOW) == 0
    assert (old_dir / "source.m4a").exists()

    settings_store.save({"recording_retention_days": 0})
    assert recstore.sweep_retention(_NOW) == 0
    assert (old_dir / "source.m4a").exists()


def test_sweep_retention_never_outruns_transcription(scratch_vault, tmp_path):
    settings_store.save({"recording_retention_days": 30})
    expired = (_NOW - dt.timedelta(days=40)).isoformat()

    # A source whose ladder never ran or failed: audio, no rung 0. The audio is
    # not a redundant copy of a transcript here, it is the only copy of the
    # session, and thirty days is exactly how long a failed job goes unnoticed.
    never_ran = recstore.source_dir("C-0001", "2026-06-15-0900")
    src = tmp_path / "incoming.m4a"
    src.write_bytes(b"fake audio bytes")
    recstore.write_audio("C-0001", "2026-06-15-0900", src)
    recstore.write_meta(
        "C-0001",
        "2026-06-15-0900",
        {"kind": "uploaded_audio", "verbatim": True, "entry_rung": 0, "captured_at": expired},
    )

    # A source that WAS transcribed, same age, same settings.
    transcribed = _seed_source(
        "C-0001", "2026-06-16-0900", captured_at=expired, tmp_path=tmp_path
    )
    recstore.write_meta(
        "C-0001",
        "2026-06-16-0900",
        {"kind": "uploaded_audio", "verbatim": True, "entry_rung": 0, "captured_at": expired},
    )

    assert recstore.sweep_retention(_NOW) == 1

    assert (never_ran / "source.m4a").exists(), "an untranscribed session keeps its only copy"
    assert not (transcribed / "source.m4a").exists(), "a transcribed one still expires"
    assert "audio_deleted_at" not in recstore.read_meta("C-0001", "2026-06-15-0900")

    # And it is still skipped on every later boot, not just the first.
    assert recstore.sweep_retention(_NOW + dt.timedelta(days=365)) == 0
    assert (never_ran / "source.m4a").exists()

    # Once the ladder does run, the audio becomes expendable again.
    recstore.write_rung("C-0001", "2026-06-15-0900", 0, "raw transcript\n")
    assert recstore.sweep_retention(_NOW) == 1
    assert not (never_ran / "source.m4a").exists()
    assert (never_ran / "transcript-raw.md").exists()


def test_sweep_retention_bad_meta_skipped(scratch_vault, tmp_path):
    settings_store.save({"recording_retention_days": 30})
    garbage = _seed_source("C-0001", "2026-06-15-0900", captured_at="not a date", tmp_path=tmp_path)
    missing = _seed_source("C-0001", "2026-06-16-0900", captured_at=None, tmp_path=tmp_path)
    empty = _seed_source("C-0001", "2026-06-17-0900", captured_at="", tmp_path=tmp_path)

    # Unreadable or unanswerable provenance never authorizes a deletion.
    assert recstore.sweep_retention(_NOW) == 0
    for d in (garbage, missing, empty):
        assert (d / "source.m4a").exists()
        assert (d / "transcript.md").exists()
