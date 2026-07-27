"""Tests for debrief.sources: classification, parsing, and the consent gate.

Everything runs against a scratch vault in tmp_path, per tests/test_recstore.py.
The real DebriefVault at the repo root is never touched.

Two groups of tests carry the clinical weight here:

  the consent gate      no ingest path may run without a dated consent flag on
                        the client profile (spec rule 6)
  provenance            a source that is not word for word must never be stored
                        as verbatim, because a paraphrase filed as a client quote
                        stays in the record forever (spec 5.2)
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

import debrief.config as config
import debrief.records as records_mod
import debrief.recstore as recstore
import debrief.sources as sources
import debrief.vault as vault_mod

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
CLIENT = "C-0001"


@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)  # records.py binds it at import
    vault_mod.ensure_vault()
    return vault_dir


@pytest.fixture()
def consented_vault(scratch_vault):
    """A scratch vault where C-0001 has dated recording consent."""
    vault_mod.set_recording_consent(CLIENT, True)
    return scratch_vault


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_ffmpeg(recorder: list):
    """Stand in for the ffmpeg seam: record the argv, create the output file.

    ffmpeg is a machine dependency, not a test dependency. Every non-live test
    goes through this so the suite never needs a real encoder or real media.
    """

    def _run(args):
        recorder.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake extracted audio")
        return subprocess.CompletedProcess(args, 0, "", "")

    return _run


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_zoom_vtt_verbatim():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "zoom.vtt", fixture_text("zoom.vtt")
    )
    assert (kind, verbatim, produced_by) == ("transcript_file", True, "zoom")
    assert confidence == 1.0


def test_classify_srt_verbatim():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "plain.srt", fixture_text("plain.srt")
    )
    assert (kind, verbatim, produced_by) == ("transcript_file", True, "unknown")
    assert confidence == 1.0


def test_classify_otter_verbatim_high_confidence():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "otter-verbatim.txt", fixture_text("otter-verbatim.txt")
    )
    assert (kind, verbatim, produced_by) == ("transcript_file", True, "otter")
    assert confidence >= sources.CONFIRM_BELOW


def test_classify_otter_summary_not_verbatim():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "otter-summary.txt", fixture_text("otter-summary.txt")
    )
    assert (kind, verbatim, produced_by) == ("third_party_summary", False, "otter")
    assert confidence >= sources.CONFIRM_BELOW


def test_classify_granola_not_verbatim():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "granola-note.md", fixture_text("granola-note.md")
    )
    assert (kind, verbatim, produced_by) == ("third_party_summary", False, "granola")
    assert confidence >= sources.CONFIRM_BELOW


def test_classify_plain_speaker_text_verbatim():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "plain-transcript.txt", fixture_text("plain-transcript.txt")
    )
    assert (kind, verbatim, produced_by) == ("transcript_file", True, "unknown")
    assert confidence >= sources.CONFIRM_BELOW


def test_classify_prose_low_confidence_needs_confirmation():
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "clinician-notes.md", fixture_text("clinician-notes.md")
    )
    assert (kind, verbatim, produced_by) == ("clinician_notes", False, "clinician")
    assert confidence < sources.CONFIRM_BELOW, "prose must be confirmed, never assumed"


def test_classify_mixed_export_is_not_assumed_verbatim():
    """An export holding both a summary and a verbatim block is the dangerous one.

    The guess must be the side that cannot put an invented sentence in the record
    as a quote, and the confidence must be low enough to ask.
    """
    text = fixture_text("otter-summary.txt") + "\n" + fixture_text("otter-verbatim.txt")
    kind, verbatim, _, confidence = sources.classify(None, text)
    assert (kind, verbatim) == ("third_party_summary", False)
    assert confidence < sources.CONFIRM_BELOW


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def test_parse_vtt_timestamps_and_speakers():
    text, turns = sources.parse_transcript(FIXTURES / "zoom.vtt")
    assert len(turns) == 7
    assert turns[0]["start"] == 3.48
    assert turns[0]["end"] == 8.12
    assert turns[0]["speaker"] == "Dana Ruiz"
    assert turns[0]["text"].startswith("Thanks for making the time today.")
    assert turns[-1]["start"] == 173.1
    assert turns[-1]["end"] == 182.48
    assert turns[-1]["speaker"] == "Dana Ruiz"
    assert turns[3]["speaker"] == "Bob Smith"
    assert "Dana Ruiz:" in text and "-->" not in text


def test_parse_srt_timestamps():
    _, turns = sources.parse_transcript(FIXTURES / "plain.srt")
    assert len(turns) == 5
    assert turns[0]["start"] == 0.0
    assert turns[0]["end"] == 4.32
    assert turns[-1]["start"] == 90.4
    assert turns[-1]["end"] == 98.22
    assert all(turn["speaker"] is None for turn in turns), "this srt carries no labels"
    # A cue wrapped over two lines is one turn, joined back into one sentence.
    assert turns[1]["text"].endswith("and then the worrying starts.")


def test_parse_plain_text_no_timestamps():
    _, turns = sources.parse_transcript(fixture_text("plain-transcript.txt"))
    assert len(turns) == 8
    assert all(turn["start"] is None and turn["end"] is None for turn in turns)
    assert [turn["speaker"] for turn in turns[:2]] == ["Therapist", "Client"]
    assert turns[0]["text"] == "Where would you like to start today?"


def test_parse_otter_headers():
    _, turns = sources.parse_transcript(fixture_text("otter-verbatim.txt"))
    assert len(turns) == 8
    assert turns[0]["start"] == 3.0
    assert turns[0]["speaker"] == "Dana Ruiz"
    assert turns[-1]["start"] == 171.0
    assert turns[-1]["speaker"] == "Bob Smith"


def test_parse_prose_containing_an_arrow_is_still_prose():
    """An arrow inside a sentence must not be read as a subtitle cue.

    The cue reader would find no cues and archive nothing, which loses the
    material silently. It falls through to the prose reader instead.
    """
    _, turns = sources.parse_transcript("He drew it out for me: trigger --> thought --> spiral.")
    assert len(turns) == 1
    assert turns[0]["start"] is None
    assert "trigger --> thought" in turns[0]["text"]


def test_parse_prose_is_one_unlabelled_turn():
    _, turns = sources.parse_transcript("Just a paragraph of notes.\nAnd another line.")
    assert len(turns) == 1
    assert turns[0] == {
        "start": None,
        "end": None,
        "speaker": None,
        "text": "Just a paragraph of notes. And another line.",
    }


# ---------------------------------------------------------------------------
# Consent, spec rule 6
# ---------------------------------------------------------------------------


def test_consent_helpers_roundtrip(scratch_vault):
    profile = scratch_vault / "Clients" / CLIENT / "_Profile.md"
    before = profile.read_text(encoding="utf-8")
    body_before = before.split("---", 2)[2]

    assert vault_mod.has_recording_consent(CLIENT) is False

    vault_mod.set_recording_consent(CLIENT, True)
    fm, _ = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    assert fm["recording_consent"] is True
    assert str(fm["recording_consent_date"]) == dt.date.today().isoformat()
    assert vault_mod.has_recording_consent(CLIENT) is True
    assert profile.read_text(encoding="utf-8").split("---", 2)[2] == body_before

    vault_mod.set_recording_consent(CLIENT, False)
    fm, _ = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    assert fm["recording_consent"] is False
    assert str(fm["recording_consent_date"]) == dt.date.today().isoformat(), (
        "withdrawing keeps the old date: it is an audit trail, not a secret"
    )
    assert vault_mod.has_recording_consent(CLIENT) is False
    assert profile.read_text(encoding="utf-8").split("---", 2)[2] == body_before


def test_consent_regrant_writes_a_fresh_date(scratch_vault):
    profile = scratch_vault / "Clients" / CLIENT / "_Profile.md"
    vault_mod.set_recording_consent(CLIENT, True)
    # Backdate the stored consent, then withdraw and grant again.
    fm, body = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    fm["recording_consent_date"] = "2020-01-01"
    vault_mod._atomic_write(profile, vault_mod._dump_frontmatter(fm) + "\n" + body)

    vault_mod.set_recording_consent(CLIENT, False)
    fm, _ = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    assert str(fm["recording_consent_date"]) == "2020-01-01"

    vault_mod.set_recording_consent(CLIENT, True)
    fm, _ = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    assert str(fm["recording_consent_date"]) == dt.date.today().isoformat()


def test_ingest_requires_consent(scratch_vault, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")

    # No flag at all.
    with pytest.raises(sources.ConsentError):
        sources.ingest_file(src, CLIENT)
    with pytest.raises(sources.ConsentError):
        sources.ingest_text("pasted words", CLIENT, "pasted_transcript")

    # Flag set by hand without a date is not consent.
    profile = scratch_vault / "Clients" / CLIENT / "_Profile.md"
    fm, body = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    fm["recording_consent"] = True
    fm.pop("recording_consent_date", None)
    vault_mod._atomic_write(profile, vault_mod._dump_frontmatter(fm) + "\n" + body)

    assert vault_mod.has_recording_consent(CLIENT) is False
    with pytest.raises(sources.ConsentError):
        sources.ingest_file(src, CLIENT)

    # An unreadable date is no better than a missing one.
    fm["recording_consent_date"] = "sometime last spring"
    vault_mod._atomic_write(profile, vault_mod._dump_frontmatter(fm) + "\n" + body)
    assert vault_mod.has_recording_consent(CLIENT) is False
    with pytest.raises(sources.ConsentError):
        sources.ingest_file(src, CLIENT)

    assert not recstore.recordings_root().exists(), "a refused ingest writes nothing"


def test_consent_error_copy_is_plain(scratch_vault, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("Some notes.\n", encoding="utf-8")
    with pytest.raises(sources.ConsentError) as excinfo:
        sources.ingest_file(src, CLIENT)
    message = str(excinfo.value).lower()
    assert "consent" in message
    for banned in ("law", "legal", "jurisdiction", "state", "attorney", "illegal"):
        assert banned not in message, f"consent copy must not talk about {banned}"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_ingest_transcript_file_writes_rung0_and_meta(consented_vault):
    source, needs_confirmation = sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)

    assert needs_confirmation is False
    assert recstore.rungs_present(CLIENT, source.id) == {0}

    raw = recstore.read_rung(CLIENT, source.id, 0)
    assert raw.splitlines()[0] == (
        "[00:03] Dana Ruiz: Thanks for making the time today. "
        "How has the week been since we last spoke?"
    )
    assert "[02:53] Dana Ruiz:" in raw

    meta = recstore.read_meta(CLIENT, source.id)
    restored = sources.Source.from_meta(meta)
    assert restored == source
    assert restored.kind == "transcript_file"
    assert restored.verbatim is True
    assert restored.entry_rung == 0
    assert restored.diarized is True
    assert restored.produced_by == "zoom"
    assert restored.duration_min == 4
    assert restored.consent_ref == f"recording_consent {dt.date.today().isoformat()}"
    assert restored.audio_path is None


def test_ingest_text_pasted(consented_vault):
    text = (
        "We picked up where we left off. He talked about the review meeting again "
        "and how long the replaying lasted afterwards."
    )
    source, needs_confirmation = sources.ingest_text(text, CLIENT, "pasted_transcript")

    assert needs_confirmation is False
    assert source.kind == "pasted_transcript"
    assert source.verbatim is True
    assert source.entry_rung == 0
    assert source.diarized is False
    assert source.duration_min is None
    assert recstore.rungs_present(CLIENT, source.id) == {0}
    assert recstore.read_rung(CLIENT, source.id, 0).strip() == text
    assert not (recstore.source_dir(CLIENT, source.id) / "source-text.md").exists()


def test_ingest_text_clinician_notes_enters_rung2(consented_vault):
    source, needs_confirmation = sources.ingest_text(
        fixture_text("clinician-notes.md"), CLIENT, "clinician_notes"
    )
    assert needs_confirmation is False
    assert source.verbatim is False
    assert source.entry_rung == 2
    assert source.produced_by == "clinician"
    assert recstore.rungs_present(CLIENT, source.id) == set()
    assert (recstore.source_dir(CLIENT, source.id) / "source-text.md").is_file()


def test_ingest_text_rejects_a_kind_it_cannot_place(consented_vault):
    with pytest.raises(sources.SourceError):
        sources.ingest_text("words", CLIENT, "uploaded_audio")


def test_ingest_audio_stores_original_container(consented_vault, tmp_path):
    src = tmp_path / "session.m4a"
    src.write_bytes(b"fake m4a bytes")

    source, needs_confirmation = sources.ingest_file(src, CLIENT)

    assert needs_confirmation is False
    assert source.kind == "uploaded_audio"
    assert source.verbatim is True
    assert source.entry_rung == 0
    stored = recstore.audio_path(CLIENT, source.id)
    assert stored is not None and stored.name == "source.m4a"
    assert stored.read_bytes() == b"fake m4a bytes"
    assert source.audio_path == stored
    # Transcription is the job queue's work, so no rung is built yet.
    assert recstore.rungs_present(CLIENT, source.id) == set()
    assert recstore.read_meta(CLIENT, source.id)["kind"] == "uploaded_audio"


def test_ingest_video_extracts_audio_discards_video(consented_vault, monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(sources, "_run_ffmpeg", fake_ffmpeg(calls))

    src = tmp_path / "session.mp4"
    src.write_bytes(b"fake mp4 bytes")

    source, _ = sources.ingest_file(src, CLIENT)

    assert source.kind == "uploaded_video"
    assert source.verbatim is True
    assert len(calls) == 1
    extracted = Path(calls[0][-1])
    assert extracted.suffix == ".m4a"
    assert not extracted.exists(), "the temp extraction is cleaned up"

    stored = recstore.audio_path(CLIENT, source.id)
    assert stored is not None and stored.name == "source.m4a"
    assert stored.read_bytes() == b"fake extracted audio"

    # The video itself is never retained, anywhere under the vault.
    assert src.is_file(), "the clinician's own file is left alone"
    assert list(consented_vault.rglob("*.mp4")) == []
    assert sorted(p.name for p in recstore.source_dir(CLIENT, source.id).iterdir()) == [
        "meta.json",
        "source.m4a",
    ]


def test_ingest_video_surfaces_ffmpeg_failure(consented_vault, monkeypatch, tmp_path):
    def _fail(args):
        return subprocess.CompletedProcess(args, 1, "", "Invalid data found when processing input")

    monkeypatch.setattr(sources, "_run_ffmpeg", _fail)
    src = tmp_path / "broken.mov"
    src.write_bytes(b"not really a movie")

    with pytest.raises(sources.SourceError) as excinfo:
        sources.ingest_file(src, CLIENT)
    assert "Invalid data found" in str(excinfo.value)


def test_ingest_doc_enters_rung2(consented_vault, tmp_path):
    src = tmp_path / "clinician-notes.md"
    src.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")

    source, needs_confirmation = sources.ingest_file(src, CLIENT)

    assert source.kind == "clinician_notes"
    assert source.verbatim is False
    assert source.entry_rung == 2
    assert needs_confirmation is True, "prose is a guess, so the clinician confirms it"

    directory = recstore.source_dir(CLIENT, source.id)
    assert (directory / "source-text.md").is_file()
    assert not (directory / "transcript-raw.md").exists()
    assert recstore.rungs_present(CLIENT, source.id) == set()


def test_ingest_otter_verbatim_txt_enters_rung0_with_turns(consented_vault, tmp_path):
    src = tmp_path / "otter-verbatim.txt"
    src.write_text(fixture_text("otter-verbatim.txt"), encoding="utf-8")

    source, needs_confirmation = sources.ingest_file(src, CLIENT)

    assert source.kind == "transcript_file"
    assert source.produced_by == "otter"
    assert source.entry_rung == 0
    assert source.diarized is True
    assert source.duration_min == 3
    assert needs_confirmation is False
    raw = recstore.read_rung(CLIENT, source.id, 0)
    assert raw.startswith("[00:03] Dana Ruiz: Thanks for making the time today.")
    assert "[02:51] Bob Smith:" in raw


def test_declared_kind_overrides(consented_vault, tmp_path):
    src = tmp_path / "otter-verbatim.txt"
    src.write_text(fixture_text("otter-verbatim.txt"), encoding="utf-8")

    source, needs_confirmation = sources.ingest_file(
        src, CLIENT, declared_kind="clinician_notes"
    )

    assert source.kind == "clinician_notes"
    assert source.verbatim is False
    assert source.entry_rung == 2
    assert source.produced_by == "clinician"
    assert needs_confirmation is False, "the clinician said so, there is nothing to ask"
    assert (recstore.source_dir(CLIENT, source.id) / "source-text.md").is_file()
    assert not (recstore.source_dir(CLIENT, source.id) / "transcript-raw.md").exists()


def test_declared_kind_must_be_a_real_kind(consented_vault, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("Some notes.\n", encoding="utf-8")
    with pytest.raises(sources.SourceError):
        sources.ingest_file(src, CLIENT, declared_kind="whatever_i_like")


def test_unreadable_extension_is_refused(consented_vault, tmp_path):
    src = tmp_path / "session.pages"
    src.write_bytes(b"not a supported document")
    with pytest.raises(sources.SourceError):
        sources.ingest_file(src, CLIENT)
    assert not recstore.recordings_root().exists()


def test_source_id_collision_suffix(consented_vault, monkeypatch, tmp_path):
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)

    src = tmp_path / "notes.md"
    src.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")

    first, _ = sources.ingest_file(src, CLIENT)
    second, _ = sources.ingest_file(src, CLIENT)
    third, _ = sources.ingest_file(src, CLIENT)

    assert [first.id, second.id, third.id] == [
        "2026-07-25-1430",
        "2026-07-25-1430-2",
        "2026-07-25-1430-3",
    ]


def test_ingest_writes_only_under_recordings(consented_vault, tmp_path):
    """No ingest path may write anywhere else in the vault.

    The psychotherapy-notes folder the vault walls off from every feature is the
    reason this test exists, and Recordings/ is the only tree ingestion owns.
    """
    before = {p for p in consented_vault.rglob("*") if p.is_file()}

    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    audio = tmp_path / "session.m4a"
    audio.write_bytes(b"fake m4a bytes")

    sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)
    sources.ingest_file(doc, CLIENT)
    sources.ingest_file(audio, CLIENT)
    sources.ingest_text("pasted words", CLIENT, "pasted_transcript")

    new = {p for p in consented_vault.rglob("*") if p.is_file()} - before
    assert new, "the ingests did write something"
    recordings = consented_vault / "Recordings"
    for path in new:
        assert recordings in path.parents, f"{path} was written outside Recordings/"


# ---------------------------------------------------------------------------
# The Source record itself
# ---------------------------------------------------------------------------


def _source(**overrides) -> dict:
    base = {
        "id": "2026-07-25-1430",
        "client_id": CLIENT,
        "kind": "transcript_file",
        "verbatim": True,
        "produced_by": "zoom",
        "entry_rung": 0,
        "captured_at": dt.datetime(2026, 7, 25, 14, 30),
        "duration_min": 52,
        "diarized": True,
        "partial": False,
        "audio_path": None,
        "consent_ref": "recording_consent 2026-07-25",
    }
    base.update(overrides)
    return base


def test_source_verbatim_rung_invariant():
    with pytest.raises(ValueError):
        sources.Source(**_source(verbatim=True, entry_rung=2))
    with pytest.raises(ValueError):
        sources.Source(**_source(kind="clinician_notes", verbatim=False, entry_rung=0))
    with pytest.raises(ValueError):
        sources.Source(**_source(kind="clinician_notes", verbatim=True, entry_rung=0))
    with pytest.raises(ValueError):
        sources.Source(**_source(kind="not_a_kind"))


def test_source_meta_roundtrip_is_json_safe():
    import json

    source = sources.Source(**_source(audio_path=Path("/tmp/x/source.m4a")))
    meta = source.to_meta()
    assert json.loads(json.dumps(meta)) == meta
    assert meta["captured_at"] == "2026-07-25T14:30:00"
    assert meta["audio_path"] == "/tmp/x/source.m4a"
    assert sources.Source.from_meta(meta) == source
