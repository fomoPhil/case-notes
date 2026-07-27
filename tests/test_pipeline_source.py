"""Tests for pipeline threading of an optional session recording.

The hero flow is a clinician talking for ninety seconds and getting a filed
note. This feature adds a second, optional input to that flow, and the first
test in this file is the one that matters most: with no source named, the plan
is exactly the plan it has always been, key for key.

Everything after that is what happens when a source IS named. A digest that is
ready becomes labelled evidence and a frontmatter link. A digest that is still
being built, or was never built, changes nothing except a line of honest copy:
the debrief runs, the note is filed, and the plan says why there was no evidence.
Never blocking and never raising is the whole point, because the alternative is a
clinician losing a debrief they already gave.

No model and no STT engine is ever reached: llm.chat and the stt seams are
stubbed in every test.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import debrief.config as config
import debrief.extract as extract_mod
import debrief.jobs as jobs
import debrief.ladder as ladder
import debrief.llm as llm
import debrief.pipeline as pipeline
import debrief.records as records_mod
import debrief.recstore as recstore
import debrief.settings_store as settings_store
import debrief.sources as sources
import debrief.stt as stt
import debrief.vault as vault_mod

CLIENT = "C-0001"
SID = "2026-07-25-1430"

# Every key transcribe_and_extract has returned since before session recordings
# existed. A source-less plan must hold exactly these and nothing else.
TODAYS_PLAN_KEYS = {
    "client_id",
    "client",
    "transcript",
    "corrected_transcript",
    "note",
    "extraction",
    "actions",
    "deduped_actions",
    "unsupported_requests",
    "next_session_suggestions",
    "session_meta",
    "timings",
    "errors",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)  # records.py binds at import
    vault_mod.ensure_vault()
    return vault_dir


@pytest.fixture(autouse=True)
def _no_leaked_hold():
    """No test may leave the background queue held by a debrief that ended."""
    yield
    assert jobs._HOLD_DEPTH == 0, "an interactive hold outlived the test that took it"
    assert not jobs._HOLD.is_set()


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Nothing in this file opens Obsidian, Calendar, or Mail."""
    monkeypatch.setattr(vault_mod, "obsidian_available", lambda: False)
    monkeypatch.setattr(pipeline.vault, "obsidian_open_uri", lambda path: "obsidian://test")


@pytest.fixture()
def stub_model(monkeypatch):
    """One stubbed llm.chat, and every call it saw.

    extract() itself runs for real, so the prompt these tests assert about is
    the prompt the model would actually have been handed.
    """
    calls: list[dict] = []

    def fake_chat(messages, schema=None, max_tokens=None, temperature=None):
        calls.append({"messages": messages, "schema": schema})
        return {
            "note": {
                "data": "Dana reported broken sleep.",
                "assessment": "Progressing toward the sleep goal.",
                "plan": "Continue paced breathing.",
                "risk_present": False,
                "interventions": ["paced breathing"],
                "themes": ["sleep"],
                "client_quotes": [],
            },
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw debrief words")
    monkeypatch.setattr(
        stt,
        "correct_transcript",
        lambda text, ctx, framework, profession=None, dictionary=None: "corrected debrief words",
    )
    return calls


@pytest.fixture()
def extract_calls(monkeypatch):
    """Every extract() call, recorded, then passed straight through to the real one."""
    seen: list[dict] = []
    real = extract_mod.extract

    def spy(transcript, client_ctx, framework, now, **kwargs):
        seen.append({"transcript": transcript, "digest": kwargs.get("digest")})
        return real(transcript, client_ctx, framework, now, **kwargs)

    monkeypatch.setattr(extract_mod, "extract", spy)
    return seen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _digest(**overrides) -> dict:
    digest = {
        "source_id": SID,
        "duration_min": 52,
        "diarized": True,
        "segments": [
            {
                "start_min": 0,
                "end_min": 10,
                "topic": "Sleep since the move",
                "summary": "Client described broken sleep.",
            }
        ],
        "themes": ["Sleep"],
        "interventions_observed": [
            {"name": "Paced breathing", "client_response": "Settled.", "start_min": 6}
        ],
        "client_statements": [
            {
                "quote": "I have not been sleeping since the move",
                "start_min": 4,
                "attribution_confidence": "high",
            }
        ],
        "risk_language_present": False,
        "risk_excerpts": [],
        "practices_or_homework": ["Breathing before bed"],
        "unresolved_threads": [],
        "notable_shifts": [],
    }
    digest.update(overrides)
    return digest


def _seed_source(**overrides) -> sources.Source:
    source = _source(**overrides)
    recstore.write_meta(source.client_id, source.id, source.to_meta())
    return source


def _seed_digest(digest: dict, client_id: str = CLIENT, source_id: str = SID) -> None:
    recstore.write_rung(
        client_id,
        source_id,
        2,
        json.dumps(digest, indent=2, ensure_ascii=False) + "\n",
        rendered_md=ladder.render_digest_md(digest),
    )


def _seed_job(state: str, source_id: str = SID, kind: str = "ladder") -> str:
    """A job record on disk, without a queue and without a worker thread."""
    record = jobs._new_record(kind, {"client_id": CLIENT, "source_id": source_id}, 5)
    record["state"] = state
    jobs._write(record)
    return record["id"]


def _user_prompt(calls: list[dict]) -> str:
    """The user message of the extraction call (the last chat with a schema)."""
    for call in reversed(calls):
        if call["schema"] is not None:
            return call["messages"][1]["content"]
    raise AssertionError("no constrained extraction call was made")


# ---------------------------------------------------------------------------
# The regression that protects the hero flow
# ---------------------------------------------------------------------------


def test_plan_no_source_has_exactly_todays_keys(scratch_vault, stub_model):
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT)

    assert set(plan) == TODAYS_PLAN_KEYS
    # And nothing about the evidence link leaked into the note metadata either.
    assert not {"source_id", "source_kind", "source_verbatim"} & set(plan["session_meta"])
    assert "evidence_conflicts" not in plan["extraction"]
    assert "SESSION EVIDENCE" not in _user_prompt(stub_model)


def test_run_debrief_without_a_source_gains_no_keys(scratch_vault, stub_model):
    merged = pipeline.run_debrief("ignored.wav", CLIENT, execute=False, verify=False)
    for key in pipeline._SOURCE_PLAN_KEYS:
        assert key not in merged


# ---------------------------------------------------------------------------
# A digest that is ready
# ---------------------------------------------------------------------------


def test_plan_with_ready_digest(scratch_vault, stub_model, extract_calls):
    _seed_source()
    _seed_digest(_digest())

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["digest_used"] is True
    assert plan["source_status"] == "ready"
    assert plan["source"] == {
        "source_id": SID,
        "kind": "transcript_file",
        "verbatim": True,
    }
    assert plan["evidence_conflicts"] == []
    assert plan["no_debrief"] is False

    # The digest reached extract, and the evidence reached the prompt.
    assert extract_calls[0]["digest"] is not None
    prompt = _user_prompt(stub_model)
    assert "SESSION EVIDENCE" in prompt
    assert '"I have not been sleeping since the move" (minute 4)' in prompt

    # The note metadata links the evidence.
    assert plan["session_meta"]["source_id"] == SID
    assert plan["session_meta"]["source_kind"] == "transcript_file"
    assert plan["session_meta"]["source_verbatim"] is True


def test_a_reclassified_source_hands_over_no_quotes(scratch_vault, stub_model):
    """load_digest re-applies provenance, so quotes go when the kind changes.

    The clinician confirms an import was a meeting tool's write up rather than a
    transcript. The digest on disk still holds the quotes it was built with, and
    the extraction prompt must not.
    """
    _seed_source(kind="third_party_summary", verbatim=False, entry_rung=2, produced_by="granola")
    _seed_digest(_digest())

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["digest_used"] is True
    assert plan["source"]["verbatim"] is False
    assert "I have not been sleeping since the move" not in _user_prompt(stub_model)


# ---------------------------------------------------------------------------
# A digest that is not ready. Both cases proceed.
# ---------------------------------------------------------------------------


def test_plan_source_processing_proceeds_and_says_so(scratch_vault, stub_model, extract_calls):
    _seed_source()
    _seed_job(jobs.RUNNING)

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["digest_used"] is False
    assert plan["source_status"] == "processing"
    assert "still being processed" in plan["source_note"]
    assert "your debrief alone" in plan["source_note"]
    # The debrief ran anyway, with no evidence and no conflicts key.
    assert plan["note"]["data"]
    assert extract_calls[0]["digest"] is None
    assert "evidence_conflicts" not in plan
    assert "SESSION EVIDENCE" not in _user_prompt(stub_model)


@pytest.mark.parametrize("state", [jobs.QUEUED, jobs.RUNNING, jobs.WAITING])
def test_every_unfinished_job_state_reads_as_processing(scratch_vault, stub_model, state):
    _seed_source()
    _seed_job(state)
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["source_status"] == "processing"


@pytest.mark.parametrize("state", [jobs.DONE, jobs.FAILED, jobs.CANCELLED])
def test_a_finished_job_with_no_digest_reads_as_missing(scratch_vault, stub_model, state):
    """A failed ladder will never produce a digest, so saying "processing" lies."""
    _seed_source()
    _seed_job(state)
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["source_status"] == "missing"


def test_a_job_for_another_source_is_not_this_source(scratch_vault, stub_model):
    _seed_source()
    _seed_job(jobs.RUNNING, source_id="2026-07-26-0900")
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["source_status"] == "missing"


def test_plan_source_missing_proceeds(scratch_vault, stub_model, extract_calls):
    """A source id that names nothing at all still files a note from the debrief."""
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id="not-a-source")

    assert plan["digest_used"] is False
    assert plan["source_status"] == "missing"
    assert plan["source"] == {"source_id": "not-a-source"}
    assert "your debrief alone" in plan["source_note"]
    assert plan["note"]["data"]
    assert extract_calls[0]["digest"] is None
    assert plan["errors"] == []


def test_an_unreadable_digest_never_takes_the_debrief_down(
    scratch_vault, stub_model, monkeypatch
):
    _seed_source()

    def boom(client_id, source_id):
        raise RuntimeError("the digest file is a directory")

    monkeypatch.setattr(ladder, "load_digest", boom)
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["digest_used"] is False
    assert plan["note"]["data"]
    assert [e["stage"] for e in plan["errors"]] == ["source"]


# ---------------------------------------------------------------------------
# The filed note
# ---------------------------------------------------------------------------


def test_note_frontmatter_links_source(scratch_vault, stub_model, monkeypatch):
    _seed_source()
    _seed_digest(_digest())

    merged = pipeline.run_debrief(
        "ignored.wav", CLIENT, execute=True, verify=False, source_id=SID
    )

    note_path = merged["note_path"]
    assert note_path, merged["errors"]
    text = open(note_path, encoding="utf-8").read()
    assert f"source_id: {SID}" in text
    assert "source_kind: transcript_file" in text
    assert "source_verbatim: true" in text
    # The merged result carries the source keys through too.
    assert merged["digest_used"] is True
    assert merged["source_status"] == "ready"


def test_a_note_filed_without_a_source_carries_no_source_frontmatter(
    scratch_vault, stub_model
):
    merged = pipeline.run_debrief("ignored.wav", CLIENT, execute=True, verify=False)
    text = open(merged["note_path"], encoding="utf-8").read()
    assert "source_id:" not in text
    assert "source_kind:" not in text
    assert "source_verbatim:" not in text


# ---------------------------------------------------------------------------
# The hold: background work steps aside while a human is waiting
# ---------------------------------------------------------------------------


def test_interactive_hold_wraps_model_phase(scratch_vault, monkeypatch):
    held: dict = {}

    def correcting(text, ctx, framework, profession=None, dictionary=None):
        held["correct"] = jobs._HOLD.is_set()
        return "corrected debrief words"

    def extracting(messages, schema=None, max_tokens=None, temperature=None):
        held["extract"] = jobs._HOLD.is_set()
        return {
            "note": {"data": "d", "assessment": "a", "plan": "p", "client_quotes": []},
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", correcting)
    monkeypatch.setattr(llm, "chat", extracting)

    assert not jobs._HOLD.is_set()
    pipeline.transcribe_and_extract("ignored.wav", CLIENT)

    assert held == {"correct": True, "extract": True}
    assert not jobs._HOLD.is_set()
    assert jobs._HOLD_DEPTH == 0


def test_interactive_hold_releases_when_the_model_phase_dies(scratch_vault, monkeypatch):
    """A hold that outlived its debrief would stop the queue for good.

    KeyboardInterrupt because the pipeline deliberately catches Exception at
    every stage: this is the class of failure that gets past those guards, and
    the context manager's finally is what has to hold.
    """

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("stopped mid correction")

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", interrupted)

    with pytest.raises(KeyboardInterrupt):
        pipeline.transcribe_and_extract("ignored.wav", CLIENT)

    assert not jobs._HOLD.is_set()
    assert jobs._HOLD_DEPTH == 0


# ---------------------------------------------------------------------------
# Conflicts between the two inputs
# ---------------------------------------------------------------------------


def test_model_conflicts_reach_the_plan(scratch_vault, monkeypatch):
    _seed_source()
    _seed_digest(_digest())

    def conflicted(messages, schema=None, max_tokens=None, temperature=None):
        return {
            "note": {"data": "d", "assessment": "a", "plan": "p", "client_quotes": []},
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
            "evidence_conflicts": [
                "The debrief places the argument last week; the recording puts it at minute 6.",
                "   ",
            ],
        }

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "corrected")
    monkeypatch.setattr(llm, "chat", conflicted)

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["evidence_conflicts"] == [
        "The debrief places the argument last week; the recording puts it at minute 6."
    ]


def test_settings_are_read_once_and_the_source_never_changes_the_format(
    scratch_vault, stub_model
):
    """Session evidence is an extra input, not a different note."""
    settings_store.save({"note_format": "SOAP", "profession": "therapy"})
    _seed_source()
    _seed_digest(_digest())

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["session_meta"]["format"] == "SOAP"
    assert [s["key"] for s in plan["session_meta"]["sections"]] == [
        "subjective",
        "objective",
        "assessment",
        "plan",
    ]
