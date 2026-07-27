"""The seven rules from the session-recording spec, one test each.

Spec: docs/specs/2026-07-25-session-recording-and-ingestion.md, section 6. These
are the tests that matter most in this feature, because every one of them guards
something that cannot be undone once it has gone wrong. A misattributed quote, a
raw transcript, or a psychotherapy note pulled into the clinical record does not
get fixed by the next release; it sits in the file forever.

Each test is named for its rule so a reviewer can grep one and read it whole:

    rule 1  extract() never sees rung 0 or rung 1
    rule 2  a source that is not word for word never produces an attributed quote
    rule 3  a low confidence attribution never becomes a filed verbatim quote
    rule 4  the raw transcript never enters the session note
    rule 5  recordings and digests never touch Private/
    rule 6  ingestion is gated on dated recording consent
    rule 7  a recording without a debrief produces a note but no actions

Everything runs against a scratch vault in tmp_path. The real DebriefVault is
never touched and no model is ever called.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

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

# Unique enough that a coincidental match is not a thing that can happen.
RUNG0_SENTINEL = "ZQ-RAW-TRANSCRIPT-7788"
RUNG1_SENTINEL = "ZQ-CORRECTED-TRANSCRIPT-9911"


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
    yield
    assert jobs._HOLD_DEPTH == 0, "an interactive hold outlived the test that took it"
    assert not jobs._HOLD.is_set()


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(vault_mod, "obsidian_available", lambda: False)
    monkeypatch.setattr(pipeline.vault, "obsidian_open_uri", lambda path: "obsidian://test")


@pytest.fixture()
def chats(monkeypatch):
    """Every llm.chat call made anywhere in the run, recorded in order.

    The reply is settable per test through `chats.reply`, so a test can make the
    model do the worst thing it could plausibly do and then check that the code
    downstream refuses to carry it.
    """

    class _Chats(list):
        reply: dict | None = None

        def messages_text(self) -> str:
            """Every message of every call, as one string to search."""
            return json.dumps(self, ensure_ascii=False, default=str)

    recorded = _Chats()

    def fake_chat(messages, schema=None, max_tokens=None, temperature=None):
        recorded.append({"messages": messages, "schema_is_none": schema is None})
        if recorded.reply is not None:
            return dict(recorded.reply)
        return _NOTE_REPLY()

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw debrief words")
    monkeypatch.setattr(
        stt,
        "correct_transcript",
        lambda text, ctx, framework, profession=None, dictionary=None: "corrected debrief words",
    )
    return recorded


def _NOTE_REPLY(**overrides) -> dict:
    reply = {
        "note": {
            "data": "Dana reported broken sleep since the move.",
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
    reply.update(overrides)
    return reply


@pytest.fixture()
def extract_calls(monkeypatch):
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
    source = sources.Source(**base)
    recstore.write_meta(source.client_id, source.id, source.to_meta())
    return source


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
        "client_statements": [],
        "risk_language_present": False,
        "risk_excerpts": [],
        "practices_or_homework": ["Breathing before bed"],
        "unresolved_threads": [],
        "notable_shifts": [],
    }
    digest.update(overrides)
    return digest


def _write_digest(digest: dict, client_id: str = CLIENT, source_id: str = SID) -> None:
    recstore.write_rung(
        client_id,
        source_id,
        2,
        json.dumps(digest, indent=2, ensure_ascii=False) + "\n",
        rendered_md=ladder.render_digest_md(digest),
    )


# ---------------------------------------------------------------------------
# Rule 1: extract() never sees rung 0 or rung 1
# ---------------------------------------------------------------------------


def test_rule1_extract_never_sees_rung0_or_rung1(scratch_vault, chats, extract_calls):
    """The archive stays an archive. Only the distillate is read.

    Both rungs carry a sentence that exists nowhere else in the world, and the
    digest deliberately does not repeat either of them. If a single message of a
    single model call in the whole run carries one of those sentences, some path
    is reading the transcript, whatever the code looks like.
    """
    _source()
    recstore.write_rung(
        CLIENT,
        SID,
        0,
        f"[00:00] Therapist: How was the week.\n[10:00] Dana Ruiz: {RUNG0_SENTINEL} and then I slept badly.\n",
    )
    recstore.write_rung(CLIENT, SID, 1, f"How was the week. {RUNG1_SENTINEL} and then I slept badly.")
    _write_digest(_digest())

    merged = pipeline.run_debrief(
        "ignored.wav", CLIENT, execute=True, verify=False, source_id=SID
    )
    assert merged["digest_used"] is True

    everything = chats.messages_text()
    assert RUNG0_SENTINEL not in everything
    assert RUNG1_SENTINEL not in everything

    # And the transcript argument is the clinician's debrief, nothing else.
    assert len(extract_calls) == 1
    assert extract_calls[0]["transcript"] == "corrected debrief words"
    assert extract_calls[0]["digest"] is not None


# ---------------------------------------------------------------------------
# Rule 2: a non-verbatim source never produces an attributed quote
# ---------------------------------------------------------------------------


def test_rule2_nonverbatim_never_attributed(scratch_vault, chats, monkeypatch):
    """A meeting tool's write up informs the summary fields and nothing else.

    A granola note is almost entirely the tool's own interpretation. Ingesting
    that as verbatim is how a paraphrase ends up in a clinical record inside
    quotation marks, so the digest built from it may hold no client statement
    and no excerpt, whatever the model offers.
    """
    invented = "I have never felt this alone in my life"

    source = _source(
        kind="third_party_summary",
        verbatim=False,
        entry_rung=2,
        produced_by="granola",
        diarized=False,
    )
    recstore.write_source_text(
        CLIENT,
        SID,
        "Dana talked about sleep and about the move. She sounded flat. "
        "The tool summarised the session in its own words.",
    )

    def eager_model(messages, schema=None, max_tokens=None, temperature=None):
        """A model doing the worst plausible thing: quoting a paraphrase."""
        if schema is ladder._MAP_SCHEMA:
            return {
                "topic": "Sleep and isolation",
                "summary": "Client sounded flat.",
                "quotes": [invented],
                "interventions": [],
                "risk_language": [invented],
                "practices_or_homework": [],
                "unresolved_threads": [],
                "notable_shifts": [],
            }
        return {
            "source_id": "made-up",
            "duration_min": 999,
            "diarized": True,
            "segments": [],
            "themes": ["Isolation"],
            "interventions_observed": [],
            "client_statements": [
                {"quote": invented, "start_min": 3, "attribution_confidence": "high"}
            ],
            "risk_language_present": True,
            "risk_excerpts": [invented],
            "practices_or_homework": [],
            "unresolved_threads": [],
            "notable_shifts": [],
        }

    monkeypatch.setattr(llm, "chat", eager_model)
    ladder.build(source, {"name": "Dana Ruiz"})

    # The digest ON DISK carries no attributed quote and no excerpt.
    stored = json.loads(recstore.read_rung(CLIENT, SID, 2))
    assert stored["client_statements"] == []
    assert stored["risk_excerpts"] == []

    # And the extraction prompt built from it carries neither.
    monkeypatch.setattr(llm, "chat", lambda *a, **k: _NOTE_REPLY())
    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "corrected debrief words")

    prompts: list[str] = []

    def capturing(messages, schema=None, max_tokens=None, temperature=None):
        prompts.append(messages[1]["content"])
        return _NOTE_REPLY()

    monkeypatch.setattr(llm, "chat", capturing)
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["digest_used"] is True
    assert plan["source"]["verbatim"] is False
    assert invented not in prompts[0]
    assert '"' not in prompts[0].split("Client statements captured word for word:")[1].split(
        "SESSION DEBRIEF TRANSCRIPT:"
    )[0]


# ---------------------------------------------------------------------------
# Rule 3: a low confidence attribution never becomes a filed quote
# ---------------------------------------------------------------------------


def test_rule3_low_confidence_never_a_filed_quote(scratch_vault, chats, monkeypatch):
    """Unattributed is recoverable. Misattributed is not.

    The ladder marked this sentence "low", which means it could not prove the
    client said it. The prompt offers it as an unattributed observation, the
    model lifts it into client_quotes anyway (which is exactly what a 12b model
    does with a sentence that reads like a quote), and the deterministic pass
    after the model takes it back out and says so.
    """
    unconfirmed = "I still think I am the problem in that house"

    _source()
    _write_digest(
        _digest(
            client_statements=[
                {"quote": unconfirmed, "start_min": 18, "attribution_confidence": "low"},
                {
                    "quote": "I have not been sleeping since the move",
                    "start_min": 4,
                    "attribution_confidence": "high",
                },
            ]
        )
    )

    chats.reply = _NOTE_REPLY(
        note={
            "data": "Dana described the household as the source of her distress.",
            "assessment": "Barriers to the sleep goal persist.",
            "plan": "Continue paced breathing.",
            "risk_present": False,
            "interventions": ["paced breathing"],
            "themes": ["sleep"],
            "client_quotes": [
                f"  {unconfirmed.upper()}  ",
                "I have not been sleeping since the move",
            ],
        }
    )

    merged = pipeline.run_debrief(
        "ignored.wav", CLIENT, execute=True, verify=False, source_id=SID
    )

    # Gone from the note, and the confirmed one survived untouched.
    assert merged["note"]["client_quotes"] == ["I have not been sleeping since the move"]

    # The removal is visible, not silent.
    assert merged["evidence_conflicts"] == [
        "A quote was removed because the recording could not confirm who said "
        f"it: '{unconfirmed.upper()[:60]}...'"
    ]

    # And it never reached the filed record.
    filed = Path(merged["note_path"]).read_text(encoding="utf-8")
    assert unconfirmed.casefold() not in filed.casefold()


def test_rule3_leaves_a_debrief_only_quote_alone(scratch_vault, chats):
    """The strip is aimed at the digest's unconfirmed statements, not at quotes.

    A note whose quotes match nothing unattributed keeps every one of them, so
    the fail-safe cannot quietly empty a clinician's note.
    """
    _source()
    _write_digest(
        _digest(
            client_statements=[
                {
                    "quote": "I have not been sleeping since the move",
                    "start_min": 4,
                    "attribution_confidence": "high",
                }
            ]
        )
    )
    chats.reply = _NOTE_REPLY(
        note={
            "data": "d",
            "assessment": "a",
            "plan": "p",
            "risk_present": False,
            "interventions": [],
            "themes": [],
            "client_quotes": ["Something the clinician dictated in the debrief"],
        }
    )

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["note"]["client_quotes"] == ["Something the clinician dictated in the debrief"]
    assert plan["evidence_conflicts"] == []


# ---------------------------------------------------------------------------
# Rule 4: the raw transcript never enters the session note
# ---------------------------------------------------------------------------


def test_rule4_raw_transcript_never_in_note(scratch_vault, chats):
    """The note links to its evidence. It does not swallow it."""
    _source()
    recstore.write_rung(CLIENT, SID, 0, f"Dana Ruiz: {RUNG0_SENTINEL} I slept badly.\n")
    recstore.write_rung(CLIENT, SID, 1, f"{RUNG1_SENTINEL} I slept badly.")
    _write_digest(_digest())

    merged = pipeline.run_debrief(
        "ignored.wav", CLIENT, execute=True, verify=False, source_id=SID
    )
    filed = Path(merged["note_path"]).read_text(encoding="utf-8")

    assert RUNG0_SENTINEL not in filed
    assert RUNG1_SENTINEL not in filed
    # The link is there instead.
    assert f"source_id: {SID}" in filed
    assert "source_kind: transcript_file" in filed
    assert "source_verbatim: true" in filed
    # And the note still carries the clinician's own debrief, as it always has.
    assert "corrected debrief words" in filed


# ---------------------------------------------------------------------------
# Rule 5: recordings and digests never touch Private/
# ---------------------------------------------------------------------------


def _private_snapshot(root: Path) -> list[tuple]:
    """(path, mtime_ns, sha256) for every entry under Private/, sorted.

    Directories are included with no hash so an added or removed folder shows up
    as loudly as a changed file.
    """
    rows: list[tuple] = []
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file():
            data = path.read_bytes()
            rows.append((rel, path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest()))
        else:
            rows.append((rel, None, None))
    return rows


def test_rule5_private_never_touched(scratch_vault, chats, monkeypatch, tmp_path):
    """45 CFR 164.501 separation, applied to session material as well as notes.

    A whole feature's worth of work runs between the two snapshots: an ingest, a
    ladder build, a debrief that files a note, a source deletion, and a
    retention sweep. Private/ has to come out the far side identical, down to
    the modification time.
    """
    private = scratch_vault / "Private"
    (private / "process-notes.md").write_text(
        "# My own process notes\n\nZQ-PRIVATE-SENTINEL-4321\n", encoding="utf-8"
    )
    before = _private_snapshot(private)
    assert before, "the Private tree has to hold something for this to prove anything"

    vault_mod.set_recording_consent(CLIENT, True)

    # 1. Ingest a real source through the real API.
    source, _ = sources.ingest_text(
        "Therapist: How was the week.\nDana Ruiz: I have not been sleeping since the move.\n",
        CLIENT,
        "pasted_transcript",
    )

    # 2. Build its ladder for real, with the model stubbed.
    def digest_model(messages, schema=None, max_tokens=None, temperature=None):
        if schema is ladder._MAP_SCHEMA:
            return {
                "topic": "Sleep",
                "summary": "Broken sleep since the move.",
                "quotes": ["I have not been sleeping since the move"],
                "interventions": [],
                "risk_language": [],
                "practices_or_homework": [],
                "unresolved_threads": [],
                "notable_shifts": [],
            }
        return {
            "source_id": source.id,
            "duration_min": None,
            "diarized": False,
            "segments": [],
            "themes": ["Sleep"],
            "interventions_observed": [],
            "client_statements": [
                {
                    "quote": "I have not been sleeping since the move",
                    "start_min": 0,
                    "attribution_confidence": "high",
                }
            ],
            "risk_language_present": False,
            "risk_excerpts": [],
            "practices_or_homework": [],
            "unresolved_threads": [],
            "notable_shifts": [],
        }

    monkeypatch.setattr(llm, "chat", digest_model)
    monkeypatch.setattr(
        stt,
        "correct_long_transcript",
        lambda text, client_ctx=None, progress=None, **kw: text,
    )
    ladder.build(source, {"name": "Dana Ruiz"})

    # 3. A debrief that uses it and files a note.
    monkeypatch.setattr(llm, "chat", lambda *a, **k: _NOTE_REPLY())
    merged = pipeline.run_debrief(
        "ignored.wav", CLIENT, execute=True, verify=False, source_id=source.id
    )
    assert merged["digest_used"] is True

    # 4. A second source with audio, swept by the retention rule.
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF....WAVEfmt ")
    old_sid = "2026-01-01-0900"
    recstore.write_audio(CLIENT, old_sid, clip)
    recstore.write_meta(
        CLIENT,
        old_sid,
        _source(id=old_sid, captured_at=dt.datetime(2026, 1, 1, 9, 0)).to_meta(),
    )
    settings_store.save({"recording_retention_days": 7})
    assert recstore.sweep_retention(dt.datetime(2026, 7, 25, 15, 0)) == 1

    # 5. And a deletion, through the trash.
    assert recstore.delete_source(CLIENT, source.id) is True

    assert _private_snapshot(private) == before


def test_rule5_no_recording_module_names_private(scratch_vault):
    """The static half: none of the recording modules mentions the folder at all.

    A path that is never written cannot be written to by accident later, and a
    grep is the cheapest possible guard on that.
    """
    root = Path(__file__).resolve().parent.parent / "debrief"
    for name in ("sources.py", "recstore.py", "ladder.py", "jobs.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "Private" not in text, f"{name} names the psychotherapy notes folder"


# ---------------------------------------------------------------------------
# Rule 6: ingestion is gated on dated recording consent
# ---------------------------------------------------------------------------


def test_rule6_consent_gates_ingest(scratch_vault, chats, tmp_path):
    """The gate holds at the module layer, on both sides of a withdrawal.

    Material captured while consent was active keeps working: the digest still
    loads and a debrief can still use it, because the consent that mattered was
    the consent at capture time and the record of it is the audit trail. New
    material is refused from the moment the flag comes off.
    """
    vault_mod.set_recording_consent(CLIENT, True)
    source, _ = sources.ingest_text(
        "Therapist: How was the week.\nDana Ruiz: Better, mostly.\n",
        CLIENT,
        "pasted_transcript",
    )
    _write_digest(_digest(source_id=source.id), source_id=source.id)

    vault_mod.set_recording_consent(CLIENT, False)
    assert vault_mod.has_recording_consent(CLIENT) is False

    # Already captured material still loads, and the debrief path still uses it.
    assert ladder.load_digest(CLIENT, source.id) is not None
    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=source.id)
    assert plan["digest_used"] is True

    # A fresh ingest is refused.
    incoming = tmp_path / "session.txt"
    incoming.write_text("Therapist: hello.\nDana Ruiz: hello.\n", encoding="utf-8")
    with pytest.raises(sources.ConsentError):
        sources.ingest_file(incoming, CLIENT)
    with pytest.raises(sources.ConsentError):
        sources.ingest_text("pasted words", CLIENT, "pasted_transcript")


# ---------------------------------------------------------------------------
# Rule 7: a recording without a debrief produces a note but no actions
# ---------------------------------------------------------------------------


def test_rule7_recording_without_debrief_yields_no_actions(scratch_vault, monkeypatch):
    """Actions are intent, and only the clinician supplies intent.

    The recording is there, the digest is there, and the debrief is silence. A
    draft note is fine. Booking a follow up from a sentence somebody said in the
    room is not.
    """
    _source()
    _write_digest(_digest())

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "   ")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "   ")

    def eager(messages, schema=None, max_tokens=None, temperature=None):
        return _NOTE_REPLY(
            actions=[
                {
                    "type": "schedule_followup",
                    "datetime_utterance": "next Tuesday at 3",
                    "duration_min": 50,
                },
                {"type": "draft_client_email", "purpose": "confirm", "attachment": None},
            ]
        )

    monkeypatch.setattr(llm, "chat", eager)

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)

    assert plan["no_debrief"] is True
    assert plan["actions"] == []
    # The note itself survives: a draft from the recording is the point.
    assert plan["note"]["data"]
    assert plan["digest_used"] is True


def test_rule7_does_not_fire_when_there_is_a_debrief(scratch_vault, monkeypatch):
    """A real debrief keeps its actions, digest or no digest."""
    _source()
    _write_digest(_digest())

    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "raw")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "Book her in for next Tuesday.")
    monkeypatch.setattr(
        llm,
        "chat",
        lambda *a, **k: _NOTE_REPLY(
            actions=[
                {
                    "type": "schedule_followup",
                    "datetime_utterance": "next Tuesday at 3",
                    "duration_min": 50,
                }
            ]
        ),
    )

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT, source_id=SID)
    assert plan["no_debrief"] is False
    assert [a["type"] for a in plan["actions"]] == ["schedule_followup"]


def test_rule7_needs_a_digest_to_fire(scratch_vault, monkeypatch):
    """With no session material, an empty debrief is the old behaviour exactly.

    no_debrief exists to explain a note written from a recording alone. A silent
    dictation with no recording behind it is a different problem and this rule
    has nothing to say about it.
    """
    monkeypatch.setattr(stt, "transcribe", lambda wav, engine_id=None: "   ")
    monkeypatch.setattr(stt, "correct_transcript", lambda *a, **k: "   ")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: _NOTE_REPLY())

    plan = pipeline.transcribe_and_extract("ignored.wav", CLIENT)
    assert "no_debrief" not in plan
