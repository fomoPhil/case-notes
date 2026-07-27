"""Tests for debrief.ladder: the rungs, the map-reduce, and the rules in code.

Everything runs against a scratch vault in tmp_path, per tests/test_recstore.py.
The real DebriefVault at the repo root is never touched, and debrief.ladder.llm
is always stubbed, so no LM Studio is needed and no real model is ever called.

Three groups carry the clinical weight:

  the rungs        a run builds only what is missing and writes each rung before
                   the next stage starts, which is what makes an interrupted job
                   resumable rather than restartable
  the rules        spec section 6 rules 2 and 3 plus grounding, enforced after
                   the model returns rather than asked for in a prompt
  the timestamps   every minute in the digest is computed by code. The map schema
                   has no field for the model to put one in.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import debrief.config as config
import debrief.ladder as ladder
import debrief.records as records_mod
import debrief.recstore as recstore
import debrief.sources as sources
import debrief.stt as stt
import debrief.vault as vault_mod

CLIENT = "C-0001"
SID = "2026-07-25-1430"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)  # records.py binds it at import
    vault_mod.ensure_vault()
    return vault_dir


@pytest.fixture(autouse=True)
def _no_real_correction(monkeypatch):
    """The correction pass never reaches a model unless a test says so.

    stt holds its own reference to llm, so stubbing ladder's does not cover it.
    The default here is a pass-through that marks the text, which also makes
    "was correction run at all" checkable from the rung 1 file.
    """

    def _corrected(text, client_ctx=None, progress=None, **kwargs):
        if progress is not None:
            progress(1, 1)
        return f"[corrected] {text}"

    monkeypatch.setattr(stt, "correct_long_transcript", _corrected)
    return _corrected


def _source(**overrides) -> sources.Source:
    base = {
        "id": SID,
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
    return sources.Source(**base)


def _notes_source(**overrides) -> sources.Source:
    """A source that enters at rung 2: the clinician's own written notes."""
    return _source(
        kind="clinician_notes",
        verbatim=False,
        entry_rung=2,
        produced_by="clinician",
        diarized=False,
        duration_min=None,
        **overrides,
    )


# The client this vault is about. Attribution is decided by matching a turn's
# speaker label against this name, so it has to be real context, not a blank.
CLIENT_CTX = {"name": "Dana Ruiz"}

# The turns every test grounds against: (seconds, speaker, text). Short, real
# sentences, so a quote either is in here or plainly is not. The clinician and
# the client each speak twice, which is what tells the rung 0 reader that these
# are speaker labels rather than words somebody said.
TURNS = [
    (0, "Therapist", "So how has the week been."),
    (600, "Dana Ruiz", "I have not been sleeping since the move."),
    (1200, "Therapist", "We tried the breathing exercise together and it did settle a little."),
    (1800, "Dana Ruiz", "I still think I am the problem in that house."),
]

TRANSCRIPT = " ".join(text for _, _, text in TURNS)


def _map_reply(**overrides) -> dict:
    reply = {
        "topic": "Sleep and rumination",
        "summary": "Client described broken sleep since moving.",
        "quotes": ["I have not been sleeping since the move"],
        "interventions": [
            {"name": "Paced breathing", "client_response": "Settled a little."}
        ],
        "risk_language": [],
        "practices_or_homework": ["Breathing before bed"],
        "unresolved_threads": ["The argument itself"],
        "notable_shifts": ["Softened when describing the exercise"],
    }
    reply.update(overrides)
    return reply


def _reduce_reply(**overrides) -> dict:
    reply = {
        "source_id": "the-model-made-this-up",
        "duration_min": 999,
        "diarized": False,
        "segments": [{"start_min": 999, "end_min": 999, "topic": "wrong", "summary": "wrong"}],
        "themes": ["Sleep", "Self blame"],
        "interventions_observed": [
            {"name": "Paced breathing", "client_response": "Settled a little.", "start_min": 999}
        ],
        "client_statements": [
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 999,
                "attribution_confidence": "high",
            }
        ],
        "risk_language_present": False,
        "risk_excerpts": [],
        "practices_or_homework": ["Breathing before bed"],
        "unresolved_threads": ["The argument itself"],
        "notable_shifts": ["Softened when describing the exercise"],
    }
    reply.update(overrides)
    return reply


class FakeLLM:
    """Stands in for debrief.ladder.llm, answering by schema.

    A map call is the one constrained to the map schema; anything else is the
    reduce. Every call is recorded so a test can assert what ran and in what
    order, and hooks let a test inspect the filesystem mid-run.
    """

    def __init__(self, map_reply=None, reduce_reply=None, on_map=None, on_reduce=None):
        self.calls: list[str] = []
        self.map_messages: list[str] = []
        self.reduce_messages: list[str] = []
        self._map_reply = map_reply or (lambda index: _map_reply())
        self._reduce_reply = reduce_reply or (lambda: _reduce_reply())
        self._on_map = on_map
        self._on_reduce = on_reduce

    def chat(self, messages, schema=None, **kwargs):
        user = messages[-1]["content"]
        if schema is ladder._MAP_SCHEMA:
            index = len(self.map_messages)
            self.calls.append("map")
            self.map_messages.append(user)
            if self._on_map is not None:
                self._on_map(index)
            return self._map_reply(index)
        self.calls.append("reduce")
        self.reduce_messages.append(user)
        if self._on_reduce is not None:
            self._on_reduce()
        return self._reduce_reply()

    @property
    def map_calls(self) -> int:
        return self.calls.count("map")

    @property
    def reduce_calls(self) -> int:
        return self.calls.count("reduce")


def _install(monkeypatch, fake: FakeLLM) -> FakeLLM:
    monkeypatch.setattr(ladder, "llm", fake)
    return fake


def _seed_rungs(rung0: str | None = None, rung1: str | None = None) -> None:
    if rung0 is not None:
        recstore.write_rung(CLIENT, SID, 0, rung0)
    if rung1 is not None:
        recstore.write_rung(CLIENT, SID, 1, rung1)


def _render(turns) -> str:
    """The rung 0 archive, in the shape sources.render_turns writes."""
    return "".join(
        f"[{seconds // 60:02d}:{seconds % 60:02d}] "
        + (f"{speaker}: " if speaker else "")
        + f"{text}\n"
        for seconds, speaker, text in turns
    )


def _timed_rung0() -> str:
    """A rung 0 archive with the stamps and speaker labels render_turns writes."""
    return _render(TURNS)


def _seed_meta(source: sources.Source) -> None:
    """Write the provenance a real source always has beside its rungs.

    load_digest reads this to decide whether a stored digest may still hand out
    quotes, so a test that round-trips a digest through disk needs it.
    """
    recstore.write_meta(source.client_id, source.id, source.to_meta())


def _long_text(sentences: int, words: int = 60) -> str:
    """Synthetic transcript long enough to pack into several map windows."""
    return " ".join(
        " ".join([f"s{i}"] + ["filler"] * (words - 1)) + "." for i in range(sentences)
    )


# ---------------------------------------------------------------------------
# The rungs
# ---------------------------------------------------------------------------


def test_build_runs_missing_rungs_only(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)

    def _boom(*args, **kwargs):
        raise AssertionError("rung 1 is already on disk, correction must not run")

    monkeypatch.setattr(stt, "correct_long_transcript", _boom)
    monkeypatch.setattr(
        stt,
        "transcribe_timed",
        lambda *a, **k: pytest.fail("rung 0 is already on disk, transcription must not run"),
    )
    fake = _install(monkeypatch, FakeLLM())

    digest = ladder.build(_source(), {})

    assert fake.calls == ["map", "reduce"]
    assert digest["source_id"] == SID
    # Rung 1 was not rewritten by this run.
    assert recstore.read_rung(CLIENT, SID, 1) == TRANSCRIPT
    # And the digest was built from the rung 1 file, not from rung 0. The
    # speaker labels and stamps only exist in the archive, so their absence
    # from the map prompt is the proof.
    assert TRANSCRIPT in fake.map_messages[0]
    assert "[00:00]" not in fake.map_messages[0]
    assert "Therapist:" not in fake.map_messages[0]


def test_build_writes_rungs_as_checkpoints(scratch_vault, monkeypatch, tmp_path):
    """Each rung is on disk before the next stage's first model call.

    This is the whole resume story: a crash between two stages leaves a rung
    file behind, and the next run picks up from it.
    """
    src = tmp_path / "incoming.m4a"
    src.write_bytes(b"fake audio bytes")
    recstore.write_audio(CLIENT, SID, src)

    directory = recstore.source_dir(CLIENT, SID)
    seen: dict[str, bool] = {}

    def _corrected(text, client_ctx=None, progress=None, **kwargs):
        seen["rung0_before_correction"] = (directory / "transcript-raw.md").is_file()
        return f"[corrected] {text}"

    monkeypatch.setattr(stt, "correct_long_transcript", _corrected)
    monkeypatch.setattr(
        stt, "transcribe_timed", lambda *a, **k: (TRANSCRIPT, [{"start": 0.0, "end": 60.0, "text": TRANSCRIPT}])
    )

    def _on_map(index):
        if index == 0:
            seen["rung1_before_map"] = (directory / "transcript.md").is_file()

    fake = _install(monkeypatch, FakeLLM(on_map=_on_map))

    ladder.build(_source(), {})

    assert seen["rung0_before_correction"] is True
    assert seen["rung1_before_map"] is True
    assert (directory / "digest.json").is_file()
    assert (directory / "digest.md").is_file()
    assert fake.reduce_calls == 1


def test_build_transcribes_when_audio_only(scratch_vault, monkeypatch, tmp_path):
    src = tmp_path / "incoming.m4a"
    src.write_bytes(b"fake audio bytes")
    recstore.write_audio(CLIENT, SID, src)

    calls: list[str] = []

    def _timed(wav_path, *args, **kwargs):
        calls.append(wav_path)
        return (
            "two sentences",
            [
                {"start": 0.0, "end": 63.0, "text": "I have not been sleeping since the move."},
                {"start": 63.0, "end": 130.0, "text": "It has been like this for weeks."},
            ],
        )

    monkeypatch.setattr(stt, "transcribe_timed", _timed)
    _install(monkeypatch, FakeLLM())

    ladder.build(_source(), {})

    assert calls and calls[0].endswith("source.m4a")
    raw = recstore.read_rung(CLIENT, SID, 0)
    assert raw.startswith("[00:00] I have not been sleeping since the move.")
    assert "[01:03] It has been like this for weeks." in raw
    # The stamps are stripped out of what correction sees.
    assert "[00:00]" not in recstore.read_rung(CLIENT, SID, 1)


def test_build_without_audio_or_rung0_raises(scratch_vault, monkeypatch):
    recstore.write_meta(CLIENT, SID, {"kind": "uploaded_audio"})
    _install(monkeypatch, FakeLLM())
    with pytest.raises(sources.SourceError):
        ladder.build(_source(), {})


def test_entry_rung2_skips_rungs_0_and_1(scratch_vault, monkeypatch):
    recstore.write_source_text(CLIENT, SID, TRANSCRIPT)

    monkeypatch.setattr(
        stt, "correct_long_transcript", lambda *a, **k: pytest.fail("rung 2 source, no correction")
    )
    monkeypatch.setattr(
        stt, "transcribe_timed", lambda *a, **k: pytest.fail("rung 2 source, no transcription")
    )
    fake = _install(monkeypatch, FakeLLM())

    digest = ladder.build(_notes_source(), {})

    directory = recstore.source_dir(CLIENT, SID)
    assert not (directory / "transcript-raw.md").exists()
    assert not (directory / "transcript.md").exists()
    assert (directory / "digest.json").is_file()
    assert recstore.rungs_present(CLIENT, SID) == {2}
    assert fake.calls == ["map", "reduce"]
    assert digest["source_id"] == SID


def test_build_returns_existing_digest_without_calling_the_model(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    _seed_meta(_source())
    _install(monkeypatch, FakeLLM())
    first = ladder.build(_source(), CLIENT_CTX)

    def _boom(*args, **kwargs):
        raise AssertionError("rung 2 is already on disk, nothing should be rebuilt")

    monkeypatch.setattr(ladder.llm, "chat", _boom)
    assert ladder.build(_source(), CLIENT_CTX) == first


# ---------------------------------------------------------------------------
# Shape, and who owns which field
# ---------------------------------------------------------------------------


def test_digest_shape_matches_schema(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    _install(monkeypatch, FakeLLM())

    digest = ladder.build(_source(), {})

    for key in ladder.DIGEST_SCHEMA["required"]:
        assert key in digest, f"the digest is missing {key}"
    assert "_dropped_ungrounded" in digest
    assert ladder.DIGEST_SCHEMA["additionalProperties"] is False
    assert set(ladder.DIGEST_SCHEMA["required"]) == set(
        ladder.DIGEST_SCHEMA["properties"]
    ), "every field in the reduce schema must be required"

    # The model lied about all three code-owned fields. The Source wins.
    assert digest["source_id"] == SID
    assert digest["duration_min"] == 52
    assert digest["diarized"] is True
    # And it lied about the timeline, which is rebuilt from the windows.
    assert [segment["topic"] for segment in digest["segments"]] == ["Sleep and rumination"]


def test_map_schema_never_asks_for_a_timestamp():
    text = json.dumps(ladder._MAP_SCHEMA)
    for forbidden in ("start_min", "end_min", "start", "timestamp", "minute"):
        assert forbidden not in text, f"the map schema must not offer a {forbidden} field"


def test_map_windows_get_code_timestamps(scratch_vault, monkeypatch):
    long_text = _long_text(120)

    # Tier 1: rung 0 carried stamps, so the minutes come from them.
    stamped = [(0.0, "opening"), (600.0, "middle"), (1800.0, "closing")]
    fake = _install(monkeypatch, FakeLLM())
    digest = ladder.build_digest(long_text, _source(), {}, timed_lines=stamped)
    starts = [segment["start_min"] for segment in digest["segments"]]
    assert fake.map_calls >= 2, "the sample must pack into more than one window"
    assert starts[0] == 0
    assert starts[-1] == 30, "the last window lands in the 30 minute line"
    assert starts == sorted(starts)

    # Tier 2: no stamps, but the source knows how long it ran.
    fake = _install(monkeypatch, FakeLLM())
    digest = ladder.build_digest(long_text, _source(duration_min=60), {}, timed_lines=None)
    starts = [segment["start_min"] for segment in digest["segments"]]
    assert starts[0] == 0
    assert starts[-1] > 0
    assert max(starts) < 60
    assert digest["segments"][-1]["end_min"] == 60

    # Tier 3: neither. A minute is never guessed.
    fake = _install(monkeypatch, FakeLLM())
    digest = ladder.build_digest(long_text, _source(duration_min=None), {}, timed_lines=None)
    assert all(segment["start_min"] is None for segment in digest["segments"])
    assert all(segment["end_min"] is None for segment in digest["segments"])


# ---------------------------------------------------------------------------
# Degrading
# ---------------------------------------------------------------------------


def test_failed_map_window_degrades(scratch_vault, monkeypatch):
    long_text = _long_text(120)

    def _map(index):
        if index == 1:
            raise RuntimeError("LM Studio fell over on this window")
        return _map_reply()

    fake = _install(monkeypatch, FakeLLM(map_reply=_map))
    digest = ladder.build_digest(long_text, _source(), {}, timed_lines=None)

    assert fake.map_calls >= 2
    assert fake.reduce_calls == 1, "one bad window must not stop the reduce"
    topics = [segment["topic"] for segment in digest["segments"]]
    assert topics[1] == "(window failed)"
    assert digest["segments"][1]["summary"] == (
        "This part of the session could not be summarized."
    )
    assert topics[0] != "(window failed)"


def test_failed_reduce_raises_laddererror(scratch_vault, monkeypatch):
    def _reduce():
        raise RuntimeError("LM Studio is not running")

    _install(monkeypatch, FakeLLM(reduce_reply=_reduce))
    with pytest.raises(ladder.LadderError):
        ladder.build_digest(TRANSCRIPT, _source(), {}, timed_lines=None)


def test_failed_reduce_leaves_earlier_rungs_on_disk(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)

    def _reduce():
        raise RuntimeError("LM Studio is not running")

    _install(monkeypatch, FakeLLM(reduce_reply=_reduce))
    with pytest.raises(ladder.LadderError):
        ladder.build(_source(), {})
    assert recstore.rungs_present(CLIENT, SID) == {0, 1}


# ---------------------------------------------------------------------------
# The rules, enforced in code
# ---------------------------------------------------------------------------


def test_rule2_nonverbatim_strips_quotes_and_excerpts(scratch_vault, monkeypatch):
    """A source that is not word for word may never produce an attributed quote.

    The model is handed a Granola style write up and still returns quotes and
    risk excerpts. Both lists come back empty. The risk flag survives, because
    the flag is an invitation to look and the quote is a claim about who spoke.
    """
    recstore.write_source_text(CLIENT, SID, TRANSCRIPT)
    granola = _source(
        kind="third_party_summary",
        verbatim=False,
        entry_rung=2,
        produced_by="granola",
        diarized=False,
    )

    reduce = _reduce_reply(
        risk_language_present=True,
        risk_excerpts=["I still think I am the problem in that house"],
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(granola, {})

    assert digest["client_statements"] == []
    assert digest["risk_excerpts"] == []
    assert digest["risk_language_present"] is True


def test_rule3_undiarized_forces_low_confidence(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        client_statements=[
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 0,
                "attribution_confidence": "high",
            },
            {
                "quote": "I still think I am the problem in that house",
                "start_min": 30,
                "attribution_confidence": "high",
            },
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    # Both quotes sit in turns labelled with this client's own name, so the
    # attribution check would pass them. Rule 3 is the outer guard, and it wins.
    digest = ladder.build(_source(diarized=False), CLIENT_CTX)

    assert len(digest["client_statements"]) == 2
    assert {item["attribution_confidence"] for item in digest["client_statements"]} == {"low"}
    assert digest["diarized"] is False


def _statements(monkeypatch, quotes, rung0=None, rung1=None, client_ctx=None, **src):
    """Build a digest whose reduce returns `quotes`, every one claimed as high."""
    _seed_rungs(rung0=rung0 or _timed_rung0(), rung1=rung1 or TRANSCRIPT)
    reduce = _reduce_reply(
        client_statements=[
            {"quote": quote, "start_min": 0, "attribution_confidence": "high"}
            for quote in quotes
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))
    digest = ladder.build(_source(**src), CLIENT_CTX if client_ctx is None else client_ctx)
    return {item["quote"]: item["attribution_confidence"] for item in digest["client_statements"]}


def test_diarized_source_keeps_high_confidence(scratch_vault, monkeypatch):
    """The counterpart to rule 3: a client turn, named, may still be attributed."""
    marks = _statements(monkeypatch, ["I have not been sleeping since the move"])
    assert marks == {"I have not been sleeping since the move": "high"}


def test_attribution_low_when_the_clinician_said_it(scratch_vault, monkeypatch):
    """The failure this check exists for.

    The words are in the transcript, the source is diarized, and the model calls
    it a high confidence client statement. But the turn carrying those words is
    the clinician's, and rung 1 has no labels left for the model to have noticed.
    Only the rung 0 turns can settle it, so they do.
    """
    quote = "We tried the breathing exercise together and it did settle a little"
    assert _statements(monkeypatch, [quote]) == {quote: "low"}


def test_attribution_matches_the_client_first_name(scratch_vault, monkeypatch):
    turns = [
        (0, "Therapist", "So how has the week been."),
        (600, "Dana", "I have not been sleeping since the move."),
        (1200, "Therapist", "We tried the breathing exercise together."),
        (1800, "Dana", "I still think I am the problem in that house."),
    ]
    marks = _statements(
        monkeypatch,
        ["I have not been sleeping since the move"],
        rung0=_render(turns),
        rung1=" ".join(text for _, _, text in turns),
    )
    assert marks == {"I have not been sleeping since the move": "high"}


def test_attribution_low_when_a_quote_spans_two_turns(scratch_vault, monkeypatch):
    """Contiguous in the transcript, but nobody said it. Two people did."""
    spanning = "So how has the week been. I have not been sleeping since the move"
    assert _statements(monkeypatch, [spanning]) == {spanning: "low"}


def test_attribution_low_for_generic_speaker_labels(scratch_vault, monkeypatch):
    """Speaker 1 is not a person. Nothing ties it to this client, so nothing does."""
    turns = [
        (0, "Speaker 1", "So how has the week been."),
        (600, "Speaker 2", "I have not been sleeping since the move."),
        (1200, "Speaker 1", "We tried the breathing exercise together."),
        (1800, "Speaker 2", "I still think I am the problem in that house."),
    ]
    marks = _statements(
        monkeypatch,
        ["I have not been sleeping since the move"],
        rung0=_render(turns),
        rung1=" ".join(text for _, _, text in turns),
    )
    assert marks == {"I have not been sleeping since the move": "low"}


def test_attribution_low_without_a_client_name_in_context(scratch_vault, monkeypatch):
    """No name to compare a label against means no attribution can be earned."""
    marks = _statements(
        monkeypatch, ["I have not been sleeping since the move"], client_ctx={}
    )
    assert marks == {"I have not been sleeping since the move": "low"}


def test_grounding_drops_invented_quotes(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        client_statements=[
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 0,
                "attribution_confidence": "high",
            },
            {
                "quote": "I have felt suicidal every single day this month",
                "start_min": 12,
                "attribution_confidence": "high",
            },
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), {})

    quotes = [item["quote"] for item in digest["client_statements"]]
    assert quotes == ["I have not been sleeping since the move"]
    assert digest["_dropped_ungrounded"] == 1


def test_grounding_survives_curly_quotes_and_exotic_spaces(scratch_vault, monkeypatch):
    """Retyping a real quote with fancy characters keeps it. Inventing does not.

    Normalisation has to cut both ways: a genuine quote reformatted with a curly
    apostrophe or a non-breaking space must not be thrown away, and a fabricated
    one must not slip through by hiding a difference in an invisible character.
    """
    text = "I have not been sleeping since the move. It is not the house's fault."
    _seed_rungs(rung0=text, rung1=text)
    reduce = _reduce_reply(
        client_statements=[
            # The real sentence, retyped with a curly apostrophe and a
            # non-breaking space. Still the client's words.
            {
                "quote": "It is not the house’s fault",
                "start_min": 0,
                "attribution_confidence": "low",
            },
            # Not in the transcript, with a zero width space hiding the seam.
            {
                "quote": "I have not been sleeping since the​divorce",
                "start_min": 0,
                "attribution_confidence": "low",
            },
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), {})

    assert [item["quote"] for item in digest["client_statements"]] == [
        "It is not the house’s fault"
    ]
    assert digest["_dropped_ungrounded"] == 1


def test_grounding_drops_invented_risk_excerpts(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        risk_language_present=True,
        risk_excerpts=[
            "I still think I am the problem in that house",
            "I have a plan and I have the means",
        ],
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), {})

    assert digest["risk_excerpts"] == ["I still think I am the problem in that house"]
    assert digest["risk_language_present"] is True
    assert digest["_dropped_ungrounded"] == 1


def test_confidence_vocabulary_coerced(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        client_statements=[
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 0,
                "attribution_confidence": "very high",
            },
            {
                "quote": "We tried the breathing exercise together",
                "start_min": 20,
                "attribution_confidence": "medium",
            },
            {
                "quote": "I still think I am the problem in that house",
                "start_min": 30,
                "attribution_confidence": "",
            },
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), {})

    assert len(digest["client_statements"]) == 3
    assert {item["attribution_confidence"] for item in digest["client_statements"]} == {"low"}


def test_interventions_take_their_minute_from_the_window(scratch_vault, monkeypatch):
    """The reduce consolidates interventions. Only code knows when they happened."""
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        interventions_observed=[
            {"name": "Paced breathing", "client_response": "Settled.", "start_min": 41},
            {"name": "Something nobody reported", "client_response": "None.", "start_min": 7},
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), {})

    by_name = {item["name"]: item for item in digest["interventions_observed"]}
    assert by_name["Paced breathing"]["start_min"] == 0, "from the window, not the model"
    assert by_name["Something nobody reported"]["start_min"] is None


# ---------------------------------------------------------------------------
# Rendering, loading, callbacks
# ---------------------------------------------------------------------------


def test_render_digest_md_readable_no_em_dash(scratch_vault, monkeypatch):
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    reduce = _reduce_reply(
        risk_language_present=True,
        risk_excerpts=["I still think I am the problem in that house"],
        themes=["Sleep", "Self blame"],
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))
    digest = ladder.build(_source(), CLIENT_CTX)

    rendered = ladder.render_digest_md(digest)

    assert "—" not in rendered and "–" not in rendered
    assert rendered.startswith(f"# Session digest {SID}")
    assert "52 minutes" in rendered
    for heading in (
        "## Timeline",
        "## Themes",
        "## Interventions observed",
        "## Client statements",
        "## Risk language",
        "## Practices or homework",
        "## Unresolved threads",
        "## Notable shifts",
    ):
        assert heading in rendered
    assert "| Start | End | Topic | What happened |" in rendered
    assert "Self blame" in rendered
    assert "attribution high" in rendered
    assert "not an assessment" in rendered
    assert "I still think I am the problem in that house" in rendered
    # digest.md on disk is the same rendering.
    assert (recstore.source_dir(CLIENT, SID) / "digest.md").read_text(
        encoding="utf-8"
    ) == rendered


def test_render_digest_md_says_none_noted_and_survives_a_thin_digest():
    empty = ladder.render_digest_md({})
    assert "None noted." in empty
    assert "—" not in empty
    assert "not known" in empty

    rendered = ladder.render_digest_md(
        {
            "source_id": SID,
            "duration_min": None,
            "diarized": False,
            "segments": [
                {"start_min": None, "end_min": None, "topic": "a | b", "summary": "one\ntwo"}
            ],
            "risk_language_present": False,
            "_dropped_ungrounded": 2,
        }
    )
    assert "| ? | ? | a \\| b | one two |" in rendered
    assert "2 candidate quote(s) were dropped" in rendered
    assert "No word for word client statements" in rendered


def test_load_digest_roundtrip_and_missing_none(scratch_vault, monkeypatch):
    assert ladder.load_digest(CLIENT, SID) is None
    assert ladder.load_digest("../escape", SID) is None

    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    _seed_meta(_source())
    _install(monkeypatch, FakeLLM())
    digest = ladder.build(_source(), CLIENT_CTX)

    assert ladder.load_digest(CLIENT, SID) == digest

    # A corrupt digest.json reads as absent rather than raising.
    (recstore.source_dir(CLIENT, SID) / "digest.json").write_text("{not json", encoding="utf-8")
    assert ladder.load_digest(CLIENT, SID) is None


def test_between_rungs_called_at_boundaries(scratch_vault, monkeypatch, tmp_path):
    _seed_rungs(rung0=_timed_rung0())
    _install(monkeypatch, FakeLLM())

    calls: list[int] = []
    ladder.build(_source(), {}, between_rungs=lambda: calls.append(len(calls)))
    assert len(calls) == 2, "one boundary before rung 1, one before rung 2"

    # A source that enters at rung 2 has exactly one boundary.
    recstore.write_source_text(CLIENT, "notes-1", TRANSCRIPT)
    notes_calls: list[int] = []
    ladder.build(
        _notes_source(id="notes-1"), {}, between_rungs=lambda: notes_calls.append(1)
    )
    assert len(notes_calls) == 1


def test_progress_stages_reported(scratch_vault, monkeypatch, tmp_path):
    src = tmp_path / "incoming.m4a"
    src.write_bytes(b"fake audio bytes")
    recstore.write_audio(CLIENT, SID, src)
    monkeypatch.setattr(
        stt,
        "transcribe_timed",
        lambda *a, **k: (TRANSCRIPT, [{"start": 0.0, "end": 60.0, "text": TRANSCRIPT}]),
    )
    _install(monkeypatch, FakeLLM())

    reported: list[tuple[str, int, int]] = []
    ladder.build(_source(), {}, progress=lambda *args: reported.append(args))

    stages = [stage for stage, _, _ in reported]
    assert stages.index("transcribing") < stages.index("correcting") < stages.index("digesting")
    assert ("transcribing", 1, 1) in reported
    assert ("correcting", 1, 1) in reported
    digesting = [(done, total) for stage, done, total in reported if stage == "digesting"]
    assert digesting[0] == (0, digesting[-1][1])
    assert digesting[-1][0] == digesting[-1][1], "the last digest tick is complete"
    assert all(0 <= done <= total for done, total in digesting)


def test_reduce_input_stays_bounded(scratch_vault, monkeypatch):
    """A very long transcript widens its windows rather than adding more.

    The reduce call reads every window's report at once, so an unbounded window
    count is an unbounded prompt, and 64k is all there is.
    """
    fake = _install(monkeypatch, FakeLLM())
    ladder.build_digest(_long_text(4000), _source(duration_min=600), {}, timed_lines=None)
    assert fake.map_calls <= ladder._MAX_MAP_WINDOWS
    assert fake.reduce_calls == 1


def test_build_digest_rejects_empty_text(scratch_vault, monkeypatch):
    _install(monkeypatch, FakeLLM())
    with pytest.raises(ladder.LadderError):
        ladder.build_digest("   ", _source(), {})


# ---------------------------------------------------------------------------
# Provenance is re-checked every time a digest is handed out
# ---------------------------------------------------------------------------


def _reclassified_meta() -> None:
    """Rewrite meta as the spec's confirm flow would: it is an AI summary."""
    recstore.write_meta(
        CLIENT,
        SID,
        _source(
            kind="third_party_summary",
            verbatim=False,
            entry_rung=2,
            produced_by="granola",
        ).to_meta(),
    )


def test_reclassifying_a_source_stops_its_stored_digest_serving_quotes(
    scratch_vault, monkeypatch
):
    """The confirm flow has to reach digests that were already written.

    A source imported as a transcript, digested with quotes, and then confirmed
    by the clinician as a meeting tool's summary must stop handing those quotes
    out. Enforcing the rule only at build time would leave a file full of
    attributed statements that nobody actually said, which is the records
    failure this feature exists to prevent.
    """
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    _seed_meta(_source())
    reduce = _reduce_reply(
        risk_language_present=True,
        risk_excerpts=["I still think I am the problem in that house"],
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    built = ladder.build(_source(), CLIENT_CTX)
    assert built["client_statements"] and built["risk_excerpts"]

    _reclassified_meta()

    def _boom(*args, **kwargs):
        raise AssertionError("nothing should be rebuilt, only re-enforced")

    monkeypatch.setattr(ladder.llm, "chat", _boom)

    reclassified = _source(
        kind="third_party_summary", verbatim=False, entry_rung=2, produced_by="granola"
    )
    for digest in (ladder.load_digest(CLIENT, SID), ladder.build(reclassified, CLIENT_CTX)):
        assert digest["client_statements"] == []
        assert digest["risk_excerpts"] == []
        # The flag and everything else the digest knows survive.
        assert digest["risk_language_present"] is True
        assert digest["themes"] == ["Sleep", "Self blame"]


def test_unreadable_provenance_fails_toward_stripping_quotes(scratch_vault, monkeypatch):
    """No provenance is not a shrug. It is a reason not to hand out quotes."""
    _seed_rungs(rung0=_timed_rung0(), rung1=TRANSCRIPT)
    _seed_meta(_source())
    _install(monkeypatch, FakeLLM())
    assert ladder.build(_source(), CLIENT_CTX)["client_statements"]

    (recstore.source_dir(CLIENT, SID) / "meta.json").write_text("{broken", encoding="utf-8")
    assert ladder.load_digest(CLIENT, SID)["client_statements"] == []

    (recstore.source_dir(CLIENT, SID) / "meta.json").unlink()
    assert ladder.load_digest(CLIENT, SID)["client_statements"] == []


def test_enforce_provenance_coerces_a_stored_confidence(scratch_vault):
    """A digest edited on disk still cannot smuggle a value past the vocabulary."""
    stored = {
        "client_statements": [
            {"quote": "a", "start_min": 0, "attribution_confidence": "certain"},
            {"quote": "b", "start_min": 1, "attribution_confidence": "high"},
        ],
        "risk_excerpts": ["x"],
    }
    kept = ladder.enforce_provenance(stored, _source(diarized=True))
    assert [item["attribution_confidence"] for item in kept["client_statements"]] == [
        "low",
        "high",
    ]
    forced = ladder.enforce_provenance(stored, _source(diarized=False))
    assert {item["attribution_confidence"] for item in forced["client_statements"]} == {"low"}
    # The input is never mutated in place.
    assert stored["client_statements"][1]["attribution_confidence"] == "high"


# ---------------------------------------------------------------------------
# Normalisation and the rung 0 reader
# ---------------------------------------------------------------------------


def test_grounding_matches_a_decomposed_accent(scratch_vault, monkeypatch):
    """The same word typed the two legal ways has to match itself."""
    precomposed = "I still cannot sit through a caf\u00e9 without checking the door."
    decomposed = "I still cannot sit through a cafe\u0301 without checking the door"
    assert precomposed != decomposed + "."

    _seed_rungs(rung0=precomposed, rung1=precomposed)
    reduce = _reduce_reply(
        client_statements=[
            {"quote": decomposed, "start_min": 0, "attribution_confidence": "low"}
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), CLIENT_CTX)
    assert [item["quote"] for item in digest["client_statements"]] == [decomposed]
    assert digest["_dropped_ungrounded"] == 0


def test_a_lone_leading_word_before_a_colon_survives_into_rung_1(scratch_vault, monkeypatch):
    """"Remember: breathe" is a sentence, not a speaker.

    The rung 0 reader strips speaker labels so correction sees only words. A
    label recurs across a session; a clinician's line that happens to open with
    a word and a colon does not, and eating its first word would put different
    words in the clinical record from the ones that were spoken.
    """
    turns = [
        (0, "Therapist", "So how has the week been."),
        (300, "Dana Ruiz", "I have not been sleeping since the move."),
        (600, "Therapist", "We tried the breathing exercise together."),
        (900, "Dana Ruiz", "It did settle a little that time."),
        (1200, None, "Remember: breathe before bed."),
        (1500, "Therapist", "Say more about the argument."),
        (1800, "Dana Ruiz", "I still think I am the problem in that house."),
    ]
    _seed_rungs(rung0=_render(turns))
    _install(monkeypatch, FakeLLM())

    ladder.build(_source(), CLIENT_CTX)

    rung1 = recstore.read_rung(CLIENT, SID, 1)
    assert "Remember: breathe before bed." in rung1
    # The real speaker labels are still stripped, and the stamps with them.
    assert "Therapist:" not in rung1
    assert "Dana Ruiz:" not in rung1
    assert "[00:00]" not in rung1


# ---------------------------------------------------------------------------
# The two ways a quote can fail to make the digest
# ---------------------------------------------------------------------------


def test_quotes_over_the_cap_are_counted_apart_from_invented_ones(
    scratch_vault, monkeypatch
):
    """Abbreviating is not the same failure as fabricating, so it gets its own count."""
    sentences = [f"Line number {n} about the week and the work." for n in range(30)]
    transcript = " ".join(sentences)
    _seed_rungs(rung0=transcript, rung1=transcript)

    quotes = [sentence.rstrip(".") for sentence in sentences[:25]]
    quotes.append("Line number ninety nine about a week that never happened")
    reduce = _reduce_reply(
        client_statements=[
            {"quote": quote, "start_min": 0, "attribution_confidence": "low"}
            for quote in quotes
        ]
    )
    _install(monkeypatch, FakeLLM(reduce_reply=lambda: reduce))

    digest = ladder.build(_source(), CLIENT_CTX)

    assert len(digest["client_statements"]) == 20
    assert digest["_dropped_over_cap"] == 5
    assert digest["_dropped_ungrounded"] == 1

    rendered = ladder.render_digest_md(digest)
    assert "1 candidate quote(s) were dropped" in rendered
    assert "5 further quote(s) were held back" in rendered
    assert "\u2014" not in rendered


def test_ladder_never_names_the_walled_off_folder():
    """A later static test asserts this too. Keep the module clean of it."""
    text = (config.REPO_ROOT / "debrief" / "ladder.py").read_text(encoding="utf-8")
    assert "Private" not in text
    assert "—" not in text
