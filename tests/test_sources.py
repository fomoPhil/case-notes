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


def test_classify_plain_speaker_text_verbatim_but_asks():
    """Speaker labels with no timestamps are a guess, not a fact.

    A pasted transcript and a clinician typing up remembered dialogue after the
    session produce the same shape, and only one of them is word for word.
    """
    kind, verbatim, produced_by, confidence = sources.classify(
        FIXTURES / "plain-transcript.txt", fixture_text("plain-transcript.txt")
    )
    assert (kind, verbatim, produced_by) == ("transcript_file", True, "unknown")
    assert confidence == 0.75
    assert confidence < sources.CONFIRM_BELOW


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


def test_classify_one_summary_heading_over_a_transcript_asks():
    """A Fathom style export: Key Takeaways above the transcript, one heading.

    One generated section is enough. Structure cannot tell which sentences came
    from a person, so the whole file is treated as a write up and confirmed.
    """
    kind, verbatim, _, confidence = sources.classify(
        FIXTURES / "fathom-export.txt", fixture_text("fathom-export.txt")
    )
    assert (kind, verbatim) == ("third_party_summary", False)
    assert confidence < sources.CONFIRM_BELOW


def test_classify_transcript_then_summary_asks():
    kind, verbatim, _, confidence = sources.classify(
        FIXTURES / "otter-transcript-then-summary.txt",
        fixture_text("otter-transcript-then-summary.txt"),
    )
    assert (kind, verbatim) == ("third_party_summary", False)
    assert confidence < sources.CONFIRM_BELOW


def test_classify_prose_named_srt_is_not_verbatim():
    """A note saved with a subtitle extension is still a note.

    The reviewer's scenario: a formulation note whose prose uses arrows, with a
    stray numbered line above one. The old index-plus-arrow test called that
    SubRip and filed the clinician's own thinking as somebody's words at full
    confidence.
    """
    note = (
        "Formulation, 25 July\n"
        "\n"
        "1\n"
        "Trigger --> thought, then the spiral. He gets home, and it runs.\n"
        "\n"
        "The chain we mapped together: 6pm --> replaying --> tight chest --> no dinner.\n"
        "Worth testing whether naming it at the desk shortens the evening.\n"
    )
    kind, verbatim, _, confidence = sources.classify(Path("formulation.srt"), note)
    assert (kind, verbatim) == ("clinician_notes", False)
    assert confidence < sources.CONFIRM_BELOW

    # Harder: the same note quotes one real cue line from the recording, so the
    # file does hold a timed cue. The index-plus-arrow pair is still prose, and
    # only a full SubRip timestamp line under the index makes this a subtitle
    # file.
    with_quote = (
        "Formulation, 25 July\n"
        "\n"
        "1\n"
        "Trigger --> thought, then the spiral.\n"
        "\n"
        "He read this back to me off the recording:\n"
        "00:12:04,500 --> 00:12:09,200\n"
        "I just could not stop going over it.\n"
        "\n"
        "The chain we mapped: 6pm --> replaying --> tight chest.\n"
    )
    kind, verbatim, _, confidence = sources.classify(Path("formulation.srt"), with_quote)
    assert (kind, verbatim) == ("clinician_notes", False)
    assert confidence < sources.CONFIRM_BELOW


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def test_classify_cueless_vtt_header_is_not_verbatim():
    """A WEBVTT header with no cues under it is a claim, not a transcript.

    Nothing in the file was said by anyone, so it cannot be archived as words.
    It is kept and confirmed rather than trusted at full confidence.
    """
    kind, verbatim, _, confidence = sources.classify(
        Path("session.vtt"), "WEBVTT\n\nNOTE exported before the recording finished\n"
    )
    assert verbatim is False
    assert kind == "clinician_notes"
    assert confidence < sources.CONFIRM_BELOW


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
    assert turns[-1]["text"].endswith("two hours of it.")
    assert "otter.ai" not in turns[-1]["text"], "the export footer is not a spoken word"


def test_parse_never_folds_a_summary_into_a_speakers_turn():
    """The records failure this module exists to prevent, at parser level.

    The file is a verbatim transcript with Otter's generated Summary stapled to
    the end. Not one word of that summary may end up inside a turn, because a
    turn is what later becomes an attributed quote.
    """
    text = fixture_text("otter-transcript-then-summary.txt")
    _, turns = sources.parse_transcript(text)

    assert len(turns) == 6
    assert turns[-1]["text"] == (
        "I did two thought records this week, and the second one actually helped."
    )
    generated = ("Bob described a difficult week", "appears to be the priority", "otter.ai")
    for turn in turns:
        for phrase in generated:
            assert phrase not in turn["text"], f"generated prose leaked into a turn: {phrase}"
    assert "priority" not in sources.render_turns(turns)


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


def test_set_consent_refuses_a_profile_it_cannot_parse(scratch_vault):
    """A broken properties block must stop the write, not be written over.

    _split_frontmatter reads unparseable YAML as an empty dict so a listing never
    crashes. Merging one key into that and saving would delete the client's name,
    diagnosis, and risk flags in a single call.
    """
    profile = scratch_vault / "Clients" / CLIENT / "_Profile.md"
    broken = (
        "---\n"
        "type: client-profile\n"
        "client_id: C-0001\n"
        'name: "Bob Smith\n'  # unterminated quote, the classic hand-edit typo
        "risk_flags: [SI-passive-2026-07-24]\n"
        "---\n"
        "\nBob is a mid-career professional.\n"
    )
    profile.write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError):
        vault_mod.set_recording_consent(CLIENT, True)

    assert profile.read_text(encoding="utf-8") == broken, "the profile is untouched"
    assert vault_mod.has_recording_consent(CLIENT) is False

    # A block that reads as something other than properties is no safer to
    # overwrite than one that will not read at all.
    scalar = "---\njust a note to self\n---\n\nBob is a mid-career professional.\n"
    profile.write_text(scalar, encoding="utf-8")
    with pytest.raises(ValueError):
        vault_mod.set_recording_consent(CLIENT, True)
    assert profile.read_text(encoding="utf-8") == scalar


def test_set_consent_accepts_an_empty_properties_block(scratch_vault):
    """An empty block holds no properties, so adding one destroys nothing.

    Only a block that failed to read is refused. An empty one is written to like
    any other.
    """
    profile = scratch_vault / "Clients" / CLIENT / "_Profile.md"
    profile.write_text("---\n---\n\nBob is a mid-career professional.\n", encoding="utf-8")

    vault_mod.set_recording_consent(CLIENT, True)

    fm, body = vault_mod._split_frontmatter(profile.read_text(encoding="utf-8"))
    assert fm["recording_consent"] is True
    assert str(fm["recording_consent_date"]) == dt.date.today().isoformat()
    assert body == "Bob is a mid-career professional.\n"
    assert vault_mod.has_recording_consent(CLIENT) is True


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
    source_text = recstore.source_dir(CLIENT, source.id) / "source-text.md"
    assert source_text.read_text(encoding="utf-8") == text, "what was pasted is kept as it came"


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
    assert "otter.ai" not in raw, "the archive holds what was said, not the export footer"


# A perfectly ordinary session transcript that happens to contain the two things
# a stop marker can be mistaken for: a sentence ending in a clock time, and a
# client saying a web address out loud. Seven turns, and every one of them must
# survive.
SPOKEN_CLOCK_AND_URL = """Therapist: Where would you like to start today?

Client: The evenings, still. Last night was one of the bad ones.
I finally gave up around 2:30
and went downstairs to sit with the dog until it got light.

Therapist: What were you doing until then?

Client: Lying there mostly. I kept opening www.headspace.com at three in the morning, which does not help.

Therapist: What happens when you open it?

Client: Nothing. I read the first screen and put the phone back down again.

Therapist: That sounds exhausting.
"""


def test_a_sentence_ending_in_a_clock_time_does_not_end_the_transcript():
    """"I finally gave up around 2:30" is a sentence, not a speaker header.

    The header shape is a short name followed by a time. A continuation line that
    happens to end in a clock time is neither, and treating it as one truncated
    the transcript from seven turns to two.
    """
    _, turns = sources.parse_transcript(SPOKEN_CLOCK_AND_URL)

    assert len(turns) == 7
    assert [turn["speaker"] for turn in turns] == [
        "Therapist",
        "Client",
        "Therapist",
        "Client",
        "Therapist",
        "Client",
        "Therapist",
    ]
    assert turns[1]["text"] == (
        "The evenings, still. Last night was one of the bad ones. "
        "I finally gave up around 2:30 "
        "and went downstairs to sit with the dog until it got light."
    ), "the line is the client still talking, so it belongs to the client's turn"
    assert len(sources.render_turns(turns).strip().splitlines()) == 7

    # The line really does have the shape a header check keys on: a short run of
    # words then a bare clock time at the end. Only the name filter tells them
    # apart, so pin that the raw pattern would have been fooled.
    assert sources._OTTER_HEADER_RE.match("I finally gave up around 2:30") is not None
    assert sources._utterance_header("I finally gave up around 2:30") is None
    assert sources._utterance_header("Dana Ruiz 2:30") == ("Dana Ruiz", 150.0)


def test_a_spoken_url_stays_inside_the_turn():
    """A client naming a website is speech. A tool signing its export is not."""
    _, turns = sources.parse_transcript(SPOKEN_CLOCK_AND_URL)
    assert "www.headspace.com at three in the morning" in turns[3]["text"]

    # Short enough to be mistaken for a credits line, still a sentence.
    _, short = sources.parse_transcript(
        "Therapist: And overnight?\n"
        "\n"
        "Client: Same as ever.\n"
        "I kept opening www.headspace.com at 3am.\n"
        "\n"
        "Therapist: Did it help?\n"
        "\n"
        "Client: Not once.\n"
    )
    assert len(short) == 4
    assert "www.headspace.com at 3am." in short[1]["text"]

    # The real thing still ends accumulation.
    assert sources._is_credit_line("Transcribed by https://otter.ai") is True
    assert sources._is_credit_line("Transcribed by Rev.com") is True
    assert sources._is_credit_line("https://otter.ai") is True


def test_a_mid_transcript_heading_costs_only_its_own_turn():
    """A stop marker ends one turn, never the rest of the session.

    Some exports drop a generated section into the middle of the dialogue.
    Stopping the whole parse there threw away every exchange that followed, which
    is the larger loss by far.
    """
    text = (
        "Therapist: Where would you like to start today?\n"
        "\n"
        "Client: The evenings, still.\n"
        "\n"
        "Summary\n"
        "The client reported poor sleep and low mood this week.\n"
        "\n"
        "Therapist: What happens in the evenings?\n"
        "\n"
        "Client: I sit with it until it gets light.\n"
    )
    _, turns = sources.parse_transcript(text)

    assert len(turns) == 4, "the exchanges after the heading survive"
    assert [turn["speaker"] for turn in turns] == [
        "Therapist",
        "Client",
        "Therapist",
        "Client",
    ]
    assert turns[1]["text"] == "The evenings, still."
    assert turns[-1]["text"] == "I sit with it until it gets light."
    joined = " ".join(turn["text"] for turn in turns)
    assert "reported poor sleep" not in joined, "the generated line is still kept out"


def test_ingest_keeps_every_turn_of_a_plain_dialogue(consented_vault, tmp_path):
    src = tmp_path / "session.txt"
    src.write_text(SPOKEN_CLOCK_AND_URL, encoding="utf-8")

    source, _ = sources.ingest_file(src, CLIENT)

    raw = recstore.read_rung(CLIENT, source.id, 0)
    assert len(raw.strip().splitlines()) == 7, "every turn reaches the archive"
    assert "went downstairs to sit with the dog" in raw
    assert "put the phone back down again" in raw


def test_multi_paragraph_turn_is_not_truncated():
    """A long answer split over paragraphs is one turn, complete.

    Rev and Descript break a client's answer at paragraph boundaries. Ending the
    turn at the first blank line would drop every paragraph after the first, and
    those are the client's own words.
    """
    _, turns = sources.parse_transcript(fixture_text("rev-multi-paragraph.txt"))

    assert len(turns) == 5
    second = turns[3]
    assert second["speaker"] == "Bob Smith"
    assert second["text"].startswith("It slowed. Not gone, but slower.")
    assert "without me ever deciding to give it the whole evening." in second["text"], (
        "the second paragraph of the answer is still the client talking"
    )
    # The three things that do end a turn still end it.
    assert "Rev.com" not in " ".join(turn["text"] for turn in turns)


def test_rung0_ingest_keeps_the_original_text_the_parser_dropped(consented_vault, tmp_path):
    """The archive of last resort: whatever the parser made of it, the text stays.

    render_turns is a rendering, not an archive. Any line the parser did not
    recognise, or deliberately refused, would otherwise exist nowhere in the
    vault and be unrecoverable. This is the test that a parser miss can never
    lose the client's words.
    """
    src = tmp_path / "otter-verbatim.txt"
    original = fixture_text("otter-verbatim.txt")
    src.write_text(original, encoding="utf-8")

    source, _ = sources.ingest_file(src, CLIENT)
    assert source.entry_rung == 0

    raw = recstore.read_rung(CLIENT, source.id, 0)
    stored = (recstore.source_dir(CLIENT, source.id) / "source-text.md").read_text(
        encoding="utf-8"
    )
    assert stored == original, "the file is kept exactly as it arrived"
    assert "otter.ai" not in raw, "the parser still keeps credits out of the transcript"
    assert "otter.ai" in stored, "and what it dropped is still recoverable"


def test_rung0_ingest_keeps_every_paragraph(consented_vault, tmp_path):
    src = tmp_path / "rev-multi-paragraph.txt"
    original = fixture_text("rev-multi-paragraph.txt")
    src.write_text(original, encoding="utf-8")

    source, _ = sources.ingest_file(src, CLIENT)

    raw = recstore.read_rung(CLIENT, source.id, 0)
    assert "without me ever deciding to give it the whole evening." in raw
    assert (recstore.source_dir(CLIENT, source.id) / "source-text.md").read_text(
        encoding="utf-8"
    ) == original


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


def test_declared_media_kind_on_a_document_is_refused(consented_vault, tmp_path):
    """A document cannot be declared a recording.

    The media kinds enter at a rung that expects audio, so accepting the claim
    would file text where a transcript belongs and leave the source describing
    something it does not hold.
    """
    src = tmp_path / "notes.md"
    src.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    for kind in ("uploaded_audio", "uploaded_video", "live_recording"):
        with pytest.raises(sources.SourceError):
            sources.ingest_file(src, CLIENT, declared_kind=kind)
    assert not recstore.recordings_root().exists(), "a refused claim writes nothing"


def test_empty_transcript_file_is_refused(consented_vault, tmp_path):
    """A 0-byte .vtt has no material in it, and says so rather than filing prose."""
    empty = tmp_path / "session.vtt"
    empty.write_bytes(b"")
    with pytest.raises(sources.SourceError) as excinfo:
        sources.ingest_file(empty, CLIENT)
    assert "no readable text" in str(excinfo.value)

    blank = tmp_path / "blank.srt"
    blank.write_text("\n \n\n", encoding="utf-8")
    with pytest.raises(sources.SourceError):
        sources.ingest_file(blank, CLIENT)
    assert not recstore.recordings_root().exists()


def test_needs_confirmation_tracks_the_threshold(consented_vault, tmp_path):
    """The 0.8 bar, wired end to end: 0.75 asks, 0.85 does not."""
    assert sources.CONFIRM_BELOW == 0.8

    asks = tmp_path / "plain-transcript.txt"
    asks.write_text(fixture_text("plain-transcript.txt"), encoding="utf-8")
    source, needs_confirmation = sources.ingest_file(asks, CLIENT)
    assert sources.classify(asks, fixture_text("plain-transcript.txt"))[3] == 0.75
    assert needs_confirmation is True
    assert source.kind == "transcript_file", "stored with its best guess either way"

    settled = tmp_path / "granola-note.md"
    settled.write_text(fixture_text("granola-note.md"), encoding="utf-8")
    source, needs_confirmation = sources.ingest_file(settled, CLIENT)
    assert sources.classify(settled, fixture_text("granola-note.md"))[3] == 0.85
    assert needs_confirmation is False
    assert source.kind == "third_party_summary"


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


def test_concurrent_uploads_in_one_minute_cannot_collide(consented_vault, monkeypatch, tmp_path):
    """Two uploads finishing in the same minute must not take the same id.

    Reserving used to be check-then-create, and both callers fit inside the gap:
    both saw a free id, both took it, and the second wrote its audio and its
    meta over the first. write_audio removes the previous container as it goes,
    so the first session's only recording was gone with nothing left to say it
    had ever been there.

    The race is forced here rather than threaded: the reservation is wrapped so
    that the moment the first caller creates its folder, a second source appears
    underneath the same id. A check-then-create reservation hands the same name
    to both. mkdir(exist_ok=False) cannot.
    """
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)

    first = tmp_path / "first.m4a"
    first.write_bytes(b"the first session")
    second = tmp_path / "second.m4a"
    second.write_bytes(b"the second session")

    real_reserve = sources._reserve_source_id
    intruder = {"done": False}

    def _racing_reserve(client_id, captured_at):
        if not intruder["done"]:
            intruder["done"] = True
            # Somebody else got the base id a microsecond earlier.
            recstore.source_dir(client_id, "2026-07-25-1430").mkdir(parents=True)
        return real_reserve(client_id, captured_at)

    monkeypatch.setattr(sources, "_reserve_source_id", _racing_reserve)
    stolen, _ = sources.ingest_file(first, CLIENT)
    monkeypatch.setattr(sources, "_reserve_source_id", real_reserve)
    later, _ = sources.ingest_file(second, CLIENT)

    assert stolen.id != later.id
    assert [stolen.id, later.id] == ["2026-07-25-1430-2", "2026-07-25-1430-3"]
    # Both recordings survive, each its own bytes.
    assert (
        recstore.audio_path(CLIENT, stolen.id).read_bytes() == b"the first session"
    )
    assert (
        recstore.audio_path(CLIENT, later.id).read_bytes() == b"the second session"
    )
    assert recstore.read_meta(CLIENT, stolen.id) is not None
    assert recstore.read_meta(CLIENT, later.id) is not None


def test_a_failed_ingest_reserves_no_id(consented_vault, monkeypatch, tmp_path):
    """An unreadable file must not leave an empty folder holding an id."""
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)

    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(sources.SourceError):
        sources.ingest_file(empty, CLIENT)

    good = tmp_path / "notes.md"
    good.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(good, CLIENT)
    assert source.id == "2026-07-25-1430", "the failure took no id with it"


def test_needs_confirmation_is_persisted_and_cleared(consented_vault, tmp_path):
    """The question survives the response it was first asked in.

    A clinician who closes the tab before answering has to find the question
    still there. On disk it is the only thing separating a source waiting for an
    answer from a source whose processing silently failed.
    """
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    unsure, needs = sources.ingest_file(doc, CLIENT)
    assert needs is True
    assert unsure.needs_confirmation is True
    assert recstore.read_meta(CLIENT, unsure.id)["needs_confirmation"] is True
    assert recstore.list_sources(CLIENT)[0]["needs_confirmation"] is True

    answered = sources.reclassify(CLIENT, unsure.id, False, "clinician")
    assert answered.needs_confirmation is False
    assert recstore.read_meta(CLIENT, unsure.id)["needs_confirmation"] is False

    # A certain classification never asks in the first place.
    certain, needs = sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)
    assert needs is False
    assert recstore.read_meta(CLIENT, certain.id)["needs_confirmation"] is False

    # Meta written before this key existed reads as answered, not as pending.
    old = recstore.read_meta(CLIENT, certain.id)
    old.pop("needs_confirmation")
    recstore.write_meta(CLIENT, certain.id, old)
    assert sources.Source.from_meta(recstore.read_meta(CLIENT, certain.id)).needs_confirmation is (
        False
    )
    rows = {row["source_id"]: row for row in recstore.list_sources(CLIENT)}
    assert rows[certain.id]["needs_confirmation"] is False


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


def test_unstructured_fallback_filters_credits_and_headings():
    # A one-utterance export sits below the header-density bar and lands in the
    # unstructured fallback, which must still keep tool credits and generated
    # headings out of the archive (the raw text is retained separately).
    text = (
        "Speaker 1  0:03\n"
        "Hello there, thanks for making time today, it has been a while.\n"
        "Transcribed by https://otter.ai"
    )
    plain, turns = sources.parse_transcript(text)
    assert len(turns) == 1
    assert turns[0]["speaker"] is None
    assert "otter.ai" not in plain
    assert "Hello there" in turns[0]["text"]
    # Ordinary prose mentioning a time or a URL mid-sentence is untouched.
    prose = (
        "Client called after the session.\n"
        "They said they gave up around 2:30 and browsed www.headspace.com for a while."
    )
    plain2, turns2 = sources.parse_transcript(prose)
    assert "2:30" in plain2 and "headspace" in plain2


# ---------------------------------------------------------------------------
# The session date, which is not the capture date
# ---------------------------------------------------------------------------


def test_session_date_defaults_to_the_capture_date(consented_vault, monkeypatch, tmp_path):
    """Every ingest path writes one, and it is the day the material arrived.

    Right far more often than it is wrong, and wrong in a way the clinician can
    see and correct. captured_at is written alongside it and says something
    different: when Debrief received the bytes.
    """
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)

    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    audio = tmp_path / "session.m4a"
    audio.write_bytes(b"fake m4a bytes")

    for source, _ in (
        sources.ingest_file(doc, CLIENT),
        sources.ingest_file(audio, CLIENT),
        sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT),
        sources.ingest_text("pasted words", CLIENT, "pasted_transcript"),
    ):
        assert source.session_date == dt.date(2026, 7, 25)
        meta = recstore.read_meta(CLIENT, source.id)
        assert meta["session_date"] == "2026-07-25"
        assert meta["captured_at"] == "2026-07-25T14:30:00", "the two dates are separate"
        assert sources.effective_session_date(source) == dt.date(2026, 7, 25)


def test_set_session_date_corrects_in_both_directions(consented_vault, monkeypatch, tmp_path):
    """A session is usually uploaded later, and occasionally prepared earlier.

    Neither is Debrief's business to have an opinion about, so any date is
    accepted and nothing else in the record moves.
    """
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(doc, CLIENT)

    # Last week's session, uploaded today.
    meta = sources.set_session_date(CLIENT, source.id, "2026-07-21")
    assert meta["session_date"] == "2026-07-21"
    assert recstore.read_meta(CLIENT, source.id)["session_date"] == "2026-07-21"

    # A date in the future is a correction too, not an error.
    sources.set_session_date(CLIENT, source.id, "2099-12-31")
    restored = sources.Source.from_meta(recstore.read_meta(CLIENT, source.id))
    assert restored.session_date == dt.date(2099, 12, 31)
    assert sources.effective_session_date(restored) == dt.date(2099, 12, 31)

    # A date object is taken as it is.
    sources.set_session_date(CLIENT, source.id, dt.date(2026, 1, 2))
    after = recstore.read_meta(CLIENT, source.id)
    assert after["session_date"] == "2026-01-02"

    # And the rest of the provenance is exactly as it was.
    assert after["captured_at"] == "2026-07-25T14:30:00"
    assert after["kind"] == source.kind
    assert after["verbatim"] == source.verbatim
    assert after["consent_ref"] == source.consent_ref
    assert after["needs_confirmation"] == source.needs_confirmation


def test_set_session_date_refuses_anything_that_is_not_a_plain_iso_date(
    consented_vault, tmp_path
):
    """One written form, because 07-08 is two different days in two countries.

    A date that reads differently depending on who opens it is worse in a
    clinical record than no date at all, so only YYYY-MM-DD is accepted and the
    stored value is never changed by a refusal.
    """
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(doc, CLIENT)
    before = recstore.read_meta(CLIENT, source.id)["session_date"]

    for bad in (
        "",
        "   ",
        None,
        "25/07/2026",
        "07/25/2026",
        "July 25 2026",
        "yesterday",
        "2026-7-5",
        "20260725",
        "2026-W27-1",
        "2026-13-01",
        "2026-02-30",
        "2026-07-25T10:00:00",
        "2026-07-25 10:00",
        "٢٠٢٦-٠٧-٢٥",  # the same date in another script
        dt.datetime(2026, 7, 25, 10, 0),
    ):
        with pytest.raises(ValueError) as excinfo:
            sources.set_session_date(CLIENT, source.id, bad)
        assert "YYYY-MM-DD" in str(excinfo.value), f"{bad!r} got an unhelpful message"
        assert not isinstance(excinfo.value, sources.SourceError)

    assert recstore.read_meta(CLIENT, source.id)["session_date"] == before


def test_set_session_date_on_a_source_that_is_not_there(consented_vault):
    with pytest.raises(sources.SourceError):
        sources.set_session_date(CLIENT, "2026-07-25-1430", "2026-07-01")
    # An id that is not a single path segment is refused the same way, rather
    # than reaching the filesystem at all.
    with pytest.raises(sources.SourceError):
        sources.set_session_date(CLIENT, "../escape", "2026-07-01")


def test_session_date_round_trips_and_falls_back_to_the_capture_date():
    """Old meta has no session_date, and it always meant the capture date."""
    import json

    source = sources.Source(**_source(session_date=dt.date(2026, 7, 20)))
    meta = source.to_meta()
    assert json.loads(json.dumps(meta)) == meta
    assert meta["session_date"] == "2026-07-20"
    assert sources.Source.from_meta(meta) == source
    assert sources.effective_session_date(source) == dt.date(2026, 7, 20)

    old = source.to_meta()
    old.pop("session_date")
    restored = sources.Source.from_meta(old)
    assert restored.session_date is None
    assert sources.effective_session_date(restored) == dt.date(2026, 7, 25)
    assert sources.effective_session_date(old) == dt.date(2026, 7, 25)

    # A stored date that cannot be read is not a source that cannot be read.
    broken = source.to_meta()
    broken["session_date"] = "sometime in July"
    assert sources.Source.from_meta(broken).session_date is None
    assert sources.effective_session_date(broken) == dt.date(2026, 7, 25)

    # Nothing readable of either kind is the one case with no answer.
    assert sources.effective_session_date({}) is None
    assert sources.effective_session_date(None) is None


def test_list_sources_carries_the_session_date(consented_vault, monkeypatch, tmp_path):
    pinned = dt.datetime(2026, 7, 25, 14, 30, 0)
    monkeypatch.setattr(sources, "_now", lambda: pinned)
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(doc, CLIENT)

    row = recstore.list_sources(CLIENT)[0]
    assert row["session_date"] == "2026-07-25"

    sources.set_session_date(CLIENT, source.id, "2026-07-21")
    row = recstore.list_sources(CLIENT)[0]
    assert row["session_date"] == "2026-07-21"
    assert row["captured_at"] == "2026-07-25T14:30:00"
    assert sources.effective_session_date(row) == dt.date(2026, 7, 21)


# ---------------------------------------------------------------------------
# Reference material, which is not a session with anybody
# ---------------------------------------------------------------------------


def test_reference_kinds_keep_the_invariant_table():
    """Two kinds rather than one, so the verbatim-to-rung pairing still holds."""
    assert sources.KIND_RULES["reference_recording"] == (True, 0)
    assert sources.KIND_RULES["reference_document"] == (False, 2)
    assert sources.REFERENCE_KINDS == {"reference_recording", "reference_document"}
    assert sources.PRODUCERS == {
        "debrief",
        "zoom",
        "otter",
        "granola",
        "clinician",
        "unknown",
    }

    recording = sources.Source(**_source(kind="reference_recording", verbatim=True, entry_rung=0))
    assert (recording.verbatim, recording.entry_rung) == (True, 0)
    document = sources.Source(**_source(kind="reference_document", verbatim=False, entry_rung=2))
    assert (document.verbatim, document.entry_rung) == (False, 2)

    for wrong in (
        {"kind": "reference_recording", "verbatim": False, "entry_rung": 0},
        {"kind": "reference_recording", "verbatim": True, "entry_rung": 2},
        {"kind": "reference_document", "verbatim": True, "entry_rung": 2},
        {"kind": "reference_document", "verbatim": False, "entry_rung": 0},
    ):
        with pytest.raises(ValueError):
            sources.Source(**_source(**wrong))


def test_classify_never_guesses_reference():
    """Nothing in the bytes can tell an article from notes about a client.

    A wrong guess either hides a session's evidence or offers an article as
    evidence about a person, so the classifier is never allowed to make it.
    """
    for path in sorted(FIXTURES.iterdir()):
        kind, _, _, _ = sources.classify(path, path.read_text(encoding="utf-8"))
        assert kind not in sources.REFERENCE_KINDS, f"{path.name} classified as reference"

    for text in (
        "An article about panic. Panic peaks and then falls on its own.",
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nWelcome to this talk on trauma.\n",
        "Speaker 1  0:03\nThis is a recording of a training webinar.\n",
    ):
        assert sources.classify(None, text)[0] not in sources.REFERENCE_KINDS


def test_declared_reference_wins_and_asks_nothing(consented_vault, tmp_path):
    """The clinician's statement about what material is for always settles it."""
    prose = tmp_path / "panic-article.md"
    prose.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")

    source, needs_confirmation = sources.ingest_file(
        prose, CLIENT, declared_kind="reference_document"
    )
    assert (source.kind, source.verbatim, source.entry_rung) == ("reference_document", False, 2)
    assert needs_confirmation is False, "there is nothing left to ask"
    assert source.needs_confirmation is False
    assert source.produced_by == "unknown", "an article is not the clinician's own notes"
    directory = recstore.source_dir(CLIENT, source.id)
    assert (directory / "source-text.md").is_file()
    assert recstore.rungs_present(CLIENT, source.id) == set()

    # A file the classifier would have been certain about goes the same way.
    certain, needs_confirmation = sources.ingest_file(
        FIXTURES / "zoom.vtt", CLIENT, declared_kind="reference_document"
    )
    assert certain.kind == "reference_document"
    assert needs_confirmation is False


def test_media_declared_reference_lands_on_the_recording_kind(
    consented_vault, monkeypatch, tmp_path
):
    """The clinician says reference, the container says which reference kind."""
    audio = tmp_path / "talk.m4a"
    audio.write_bytes(b"fake m4a bytes")
    source, needs_confirmation = sources.ingest_file(
        audio, CLIENT, declared_kind="reference_document"
    )
    assert (source.kind, source.verbatim, source.entry_rung) == ("reference_recording", True, 0)
    assert needs_confirmation is False
    assert recstore.audio_path(CLIENT, source.id).name == "source.m4a"

    monkeypatch.setattr(sources, "_run_ffmpeg", fake_ffmpeg([]))
    video = tmp_path / "webinar.mp4"
    video.write_bytes(b"fake mp4 bytes")
    from_video, _ = sources.ingest_file(video, CLIENT, declared_kind="reference_recording")
    assert from_video.kind == "reference_recording"
    assert recstore.audio_path(CLIENT, from_video.id) is not None


def test_text_declared_reference_recording_lands_on_the_document_kind(
    consented_vault, tmp_path
):
    """The same correction from the other side: text has no audio to transcribe."""
    chapter = tmp_path / "chapter.md"
    chapter.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")

    source, needs_confirmation = sources.ingest_file(
        chapter, CLIENT, declared_kind="reference_recording"
    )
    assert (source.kind, source.verbatim, source.entry_rung) == ("reference_document", False, 2)
    assert needs_confirmation is False

    # The ordinary refusal is untouched: a document still cannot be a recording.
    with pytest.raises(sources.SourceError):
        sources.ingest_file(chapter, CLIENT, declared_kind="uploaded_audio")


def _seed_digest(source_id: str) -> None:
    """A rung 2 pair on disk, as a finished ladder run would leave it."""
    recstore.write_rung(
        CLIENT, source_id, 2, '{"source_id": "x"}\n', rendered_md="# Session digest x\n"
    )


def test_flip_to_reference_drops_the_digest_and_keeps_everything_else(
    consented_vault, tmp_path
):
    """The digest is the only thing a flip destroys, because it is the only
    thing whose shape depended on the answer."""
    source, _ = sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)
    recstore.write_rung(CLIENT, source.id, 1, "corrected transcript")
    _seed_digest(source.id)
    sources.set_session_date(CLIENT, source.id, "2026-07-01")

    flipped = sources.reclassify(CLIENT, source.id, reference=True)

    assert flipped.kind == "reference_recording"
    assert (flipped.verbatim, flipped.entry_rung) == (True, 0)
    assert flipped.needs_confirmation is False
    assert flipped.diarized is True, "who spoke is a fact about the bytes, not about the use"
    assert flipped.session_date == dt.date(2026, 7, 1), "a corrected date survives a flip"
    assert flipped.duration_min == source.duration_min

    directory = recstore.source_dir(CLIENT, source.id)
    assert recstore.rungs_present(CLIENT, source.id) == {0, 1}
    assert not (directory / "digest.json").exists()
    assert not (directory / "digest.md").exists(), "the rendering never outlives the digest"
    assert (directory / "source-text.md").is_file(), "the material itself is never touched"
    assert recstore.read_meta(CLIENT, source.id)["kind"] == "reference_recording"

    # A second click must not cost another digest and another rebuild.
    _seed_digest(source.id)
    again = sources.reclassify(CLIENT, source.id, reference=True)
    assert again == flipped
    assert (directory / "digest.json").is_file()


def test_flip_to_reference_works_on_a_recording(consented_vault, tmp_path):
    """Confirming a recording is meaningless, but calling one reference is not."""
    audio = tmp_path / "session.m4a"
    audio.write_bytes(b"fake m4a bytes")
    source, _ = sources.ingest_file(audio, CLIENT)

    flipped = sources.reclassify(CLIENT, source.id, reference=True)
    assert flipped.kind == "reference_recording"
    assert recstore.audio_path(CLIENT, source.id) is not None
    assert flipped.audio_path == source.audio_path


def test_flip_back_from_reference_reads_the_answer_off_the_disk(
    consented_vault, monkeypatch, tmp_path
):
    """Audio present means uploaded_audio, no audio means transcript_file.

    The kind a source arrived as is gone once the flip overwrote it, and the
    documented rule is what is actually on disk. A video comes back as
    uploaded_audio, which is true of every byte Debrief kept.
    """
    monkeypatch.setattr(sources, "_run_ffmpeg", fake_ffmpeg([]))
    video = tmp_path / "session.mp4"
    video.write_bytes(b"fake mp4 bytes")
    from_video, _ = sources.ingest_file(video, CLIENT)
    assert from_video.kind == "uploaded_video"

    sources.reclassify(CLIENT, from_video.id, reference=True)
    back = sources.reclassify(CLIENT, from_video.id, reference=False)
    assert back.kind == "uploaded_audio"
    assert (back.verbatim, back.entry_rung) == (True, 0)

    # No audio, so the transcript is what is there.
    transcript, _ = sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)
    sources.reclassify(CLIENT, transcript.id, reference=True)
    back = sources.reclassify(CLIENT, transcript.id, reference=False)
    assert back.kind == "transcript_file"
    assert back.diarized is True
    assert back.duration_min == transcript.duration_min

    # A write up comes back a write up, and who produced it decides which.
    granola = tmp_path / "granola-note.md"
    granola.write_text(fixture_text("granola-note.md"), encoding="utf-8")
    tool, _ = sources.ingest_file(granola, CLIENT)
    assert tool.kind == "third_party_summary"
    sources.reclassify(CLIENT, tool.id, reference=True)
    assert recstore.read_meta(CLIENT, tool.id)["kind"] == "reference_document"
    assert sources.reclassify(CLIENT, tool.id, reference=False).kind == "third_party_summary"

    own, _ = sources.ingest_text(fixture_text("clinician-notes.md"), CLIENT, "clinician_notes")
    sources.reclassify(CLIENT, own.id, reference=True)
    assert sources.reclassify(CLIENT, own.id, reference=False).kind == "clinician_notes"


def test_flip_back_never_resurrects_a_digest(consented_vault):
    """Both directions delete rung 2, so no digest ever outlives its shape."""
    source, _ = sources.ingest_file(FIXTURES / "zoom.vtt", CLIENT)
    _seed_digest(source.id)
    sources.reclassify(CLIENT, source.id, reference=True)
    assert 2 not in recstore.rungs_present(CLIENT, source.id)

    # A reference digest written while the source was reference material.
    recstore.write_rung(
        CLIENT, source.id, 2, '{"kind": "reference"}\n', rendered_md="# Reference digest\n"
    )
    sources.reclassify(CLIENT, source.id, reference=False)

    directory = recstore.source_dir(CLIENT, source.id)
    assert 2 not in recstore.rungs_present(CLIENT, source.id)
    assert not (directory / "digest.md").exists()
    assert recstore.rungs_present(CLIENT, source.id) == {0}


def test_flipping_a_source_keeps_the_meta_this_module_does_not_own(
    consented_vault, tmp_path
):
    """The retention stamp is a record of what happened, not a field to drop."""
    audio = tmp_path / "session.m4a"
    audio.write_bytes(b"fake m4a bytes")
    source, _ = sources.ingest_file(audio, CLIENT)
    meta = recstore.read_meta(CLIENT, source.id)
    meta["audio_deleted_at"] = "2026-07-26T09:00:00"
    recstore.write_meta(CLIENT, source.id, meta)

    sources.reclassify(CLIENT, source.id, reference=True)

    after = recstore.read_meta(CLIENT, source.id)
    assert after["audio_deleted_at"] == "2026-07-26T09:00:00"
    assert after["kind"] == "reference_recording"


def test_reference_material_has_nothing_to_confirm(consented_vault, tmp_path):
    """The word for word question is about session material, and this is not."""
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(doc, CLIENT)
    sources.reclassify(CLIENT, source.id, reference=True)

    with pytest.raises(sources.SourceError) as excinfo:
        sources.reclassify(CLIENT, source.id, True, "unknown")
    assert "reference material" in str(excinfo.value)
    assert recstore.read_meta(CLIENT, source.id)["kind"] == "reference_document"


def test_reclassify_still_needs_a_word_for_word_answer(consented_vault, tmp_path):
    """The confirm path takes a bool. Nothing else may be read as one."""
    doc = tmp_path / "notes.md"
    doc.write_text(fixture_text("clinician-notes.md"), encoding="utf-8")
    source, _ = sources.ingest_file(doc, CLIENT)

    with pytest.raises(sources.SourceError):
        sources.reclassify(CLIENT, source.id, produced_by="clinician")
    assert recstore.read_meta(CLIENT, source.id)["needs_confirmation"] is True
