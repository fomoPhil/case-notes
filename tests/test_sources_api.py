"""Tests for the session-source API: upload, paste, confirm, read, delete.

Everything runs against a scratch vault in tmp_path, per tests/test_records_api.py.
The real DebriefVault at the repo root is never touched and no test may reach a
model: the job worker is disabled in every test, so a submitted job is a record
on disk and nothing more.

Three things carry the clinical weight here:

  the consent gate      no route may bring material in without a dated consent
                        flag on the client profile, the paste route included
  the confirmation      material the classifier is unsure about is stored with
                        its guess and queues NO work, because classifying an AI
                        write up as verbatim without the human answer is the
                        records failure this feature exists to prevent
  stale evidence        a digest built under the old provenance is deleted when
                        the clinician's answer changes that provenance
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

import app as app_module

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
CLIENT = "C-0001"
NO_CONSENT_CLIENT = "C-0002"


def _healthy_checks():
    return [
        {"name": "Model server reachable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Gemma model loaded", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
    ]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A TestClient on a scratch vault, with consent granted and no worker thread."""
    jobs = app_module.jobs
    jobs._reset_state()

    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.records, "VAULT_DIR", vault_dir)
    app_module.vault.ensure_vault()
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    # No thread: submit() still writes the record and still queues, but nothing
    # runs, so no test can reach the ladder, stt, or the model.
    monkeypatch.setattr(jobs, "_worker_enabled", False)
    app_module.vault.set_recording_consent(CLIENT, True)

    yield TestClient(app_module.app), vault_dir

    jobs._reset_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upload(tc, name: str, data: bytes, client_id: str = CLIENT, **extra):
    form = {"client_id": client_id}
    form.update({k: v for k, v in extra.items() if v is not None})
    return tc.post(
        "/api/sources/upload",
        files={"file": (name, data, "application/octet-stream")},
        data=form,
    )


def _upload_fixture(tc, name: str, client_id: str = CLIENT, **extra):
    return _upload(tc, name, (FIXTURES / name).read_bytes(), client_id, **extra)


def _source_dir(vault_dir: Path, source_id: str, client_id: str = CLIENT) -> Path:
    return vault_dir / "Recordings" / client_id / source_id


def _today() -> str:
    """The day material uploaded in a test was captured on, in the one form."""
    return _dt.date.today().isoformat()


def _digest(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "duration_min": 12,
        "diarized": True,
        "segments": [{"start_min": 0, "end_min": 6, "topic": "Monday", "summary": "Replaying."}],
        "themes": ["rumination"],
        "interventions_observed": [],
        "client_statements": [],
        "risk_language_present": False,
        "risk_excerpts": [],
        "practices_or_homework": [],
        "unresolved_threads": [],
        "notable_shifts": [],
    }


def _seed_digest(source_id: str, client_id: str = CLIENT) -> dict:
    digest = _digest(source_id)
    app_module.recstore.write_rung(
        client_id,
        source_id,
        2,
        json.dumps(digest, indent=2) + "\n",
        rendered_md="# Session digest\n",
    )
    return digest


def _seed_note(vault_dir: Path, client_id: str = CLIENT, date: str = "2026-07-20") -> str:
    """One filed session note, and its vault relative path."""
    path = app_module.vault.write_session_note(
        client_id,
        {"data": "Filed from the debrief.", "assessment": "a", "plan": "p"},
        "the spoken debrief",
        {
            "session_date": date,
            "session_number": 1,
            "sections": [
                {"key": "data", "heading": "Data"},
                {"key": "assessment", "heading": "Assessment"},
                {"key": "plan", "heading": "Plan"},
            ],
        },
    )
    return str(path.relative_to(vault_dir))


def _stub_extraction(monkeypatch) -> None:
    """No model, and no STT: the draft route's one call answers from here."""

    def fake_chat(messages, schema=None, max_tokens=None, temperature=None):
        return {
            "note": {
                "data": "The recording covers sleep since the move.",
                "assessment": "Consistent with the sleep goal.",
                "plan": "Continue paced breathing.",
                "risk_present": False,
                "interventions": [],
                "themes": ["sleep"],
                "client_quotes": [],
            },
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }

    monkeypatch.setattr(app_module.llm, "chat", fake_chat)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_transcript_roundtrip(client):
    tc, vault_dir = client
    resp = _upload_fixture(tc, "zoom.vtt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "transcript_file"
    assert body["verbatim"] is True
    assert body["produced_by"] == "zoom"
    assert body["needs_confirmation"] is False
    assert body["job_id"]
    assert body["job_error"] is None

    # rung 0 is on disk and the queued job names this source.
    assert (_source_dir(vault_dir, body["source_id"]) / "transcript-raw.md").is_file()
    record = app_module.jobs.status(body["job_id"])
    assert record["kind"] == "ladder"
    assert record["payload"] == {"client_id": CLIENT, "source_id": body["source_id"]}
    assert record["state"] == "queued"


def test_upload_ambiguous_doc_queues_nothing(client, tmp_path):
    tc, vault_dir = client
    resp = _upload_fixture(tc, "clinician-notes.md")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_confirmation"] is True
    assert body["verbatim"] is False
    assert body["job_id"] is None
    # Nothing was queued, and nothing was written that a resume could pick up.
    assert app_module.jobs.list_jobs() == []
    assert not (vault_dir / ".debrief_staging" / "jobs").exists()


def test_upload_with_a_declared_kind_needs_no_confirmation(client):
    tc, _ = client
    # The clinician said what it is on the way in, so there is nothing to ask.
    resp = _upload_fixture(tc, "clinician-notes.md", declared_kind="clinician_notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "clinician_notes"
    assert body["verbatim"] is False
    assert body["entry_rung"] == 2
    assert body["needs_confirmation"] is False
    assert body["job_id"]


def test_upload_rejects_a_declared_kind_a_document_cannot_be(client):
    tc, _ = client
    resp = _upload_fixture(tc, "clinician-notes.md", declared_kind="uploaded_audio")
    assert resp.status_code == 422


def test_upload_without_consent_403_before_staging_anything(client, monkeypatch):
    tc, vault_dir = client
    staged: list = []

    def _never(*args, **kwargs):
        staged.append(args)
        raise AssertionError("a consent-less upload must not be copied to disk")

    # Asked early enough that gigabytes of somebody's session are not written to
    # the disk first and told no afterwards.
    monkeypatch.setattr(app_module, "_copy_capped", _never)
    resp = _upload_fixture(tc, "zoom.vtt", client_id=NO_CONSENT_CLIENT)
    assert resp.status_code == 403
    assert staged == []
    assert resp.json()["detail"] == app_module.sources.CONSENT_MESSAGE


def test_upload_without_consent_403_with_plain_copy(client):
    tc, vault_dir = client
    resp = _upload_fixture(tc, "zoom.vtt", client_id=NO_CONSENT_CLIENT)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail == app_module.sources.CONSENT_MESSAGE
    # Plain and non-legal: the app takes no position on recording law.
    low = detail.lower()
    assert not any(word in low for word in ("law", "legal", "jurisdiction", "state", "consult"))
    assert "—" not in detail
    # Nothing landed for a client who never consented.
    assert not (vault_dir / "Recordings" / NO_CONSENT_CLIENT).exists()


def test_upload_rejects_disallowed_extension_400(client):
    tc, _ = client
    resp = _upload(tc, "session.exe", b"MZ binary")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    # The message has to name what IS accepted, not just what is not.
    for ext in (".m4a", ".mp4", ".vtt", ".docx"):
        assert ext in detail


def test_upload_over_cap_413(client, monkeypatch, tmp_path):
    tc, _ = client
    # The cap the app actually ships with, and copy that names it.
    assert app_module._SOURCE_MAX_UPLOAD_BYTES == 2 * 1024 * 1024 * 1024
    assert "2 GB" in app_module._SOURCE_TOO_LARGE

    # Declared content-length: refused without staging the file.
    monkeypatch.setattr(app_module, "_SOURCE_MAX_UPLOAD_BYTES", 64)
    resp = _upload(tc, "session.wav", b"x" * 4096)
    assert resp.status_code == 413
    assert resp.json()["detail"] == app_module._SOURCE_TOO_LARGE

    # Streamed bytes: a body that lies about its length is still capped.
    dest = tmp_path / "streamed.bin"
    upload = UploadFile(file=io.BytesIO(b"x" * 4096), filename="session.wav")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(app_module._stream_upload(upload, dest))
    assert caught.value.status_code == 413


def test_upload_cleans_its_temp_file_on_failure(client, monkeypatch):
    tc, _ = client

    def _boom(*_args, **_kwargs):
        raise app_module.sources.SourceError("that file could not be read.")

    monkeypatch.setattr(app_module.sources, "ingest_file", _boom)
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("debrief_source_upload_*"))
    resp = _upload_fixture(tc, "zoom.vtt")
    assert resp.status_code == 422
    assert set(tmp_root.glob("debrief_source_upload_*")) == before


# ---------------------------------------------------------------------------
# Paste
# ---------------------------------------------------------------------------


def test_paste_transcript_queues_work(client):
    tc, vault_dir = client
    resp = tc.post(
        "/api/sources/paste",
        json={"client_id": CLIENT, "text": "Dana: How was the week?", "kind": "pasted_transcript"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "pasted_transcript"
    assert body["verbatim"] is True
    assert body["needs_confirmation"] is False
    assert body["job_id"]
    assert (_source_dir(vault_dir, body["source_id"]) / "source-text.md").is_file()


def test_paste_without_consent_403(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/paste",
        json={"client_id": NO_CONSENT_CLIENT, "text": "Some notes.", "kind": "clinician_notes"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == app_module.sources.CONSENT_MESSAGE


def test_paste_over_cap_413(client, monkeypatch):
    tc, _ = client
    assert app_module._PASTE_MAX_BYTES == 2 * 1024 * 1024
    assert "2 MB" in app_module._PASTE_TOO_LARGE

    monkeypatch.setattr(app_module, "_PASTE_MAX_BYTES", 512)
    resp = tc.post(
        "/api/sources/paste",
        json={"client_id": CLIENT, "text": "x" * 4096, "kind": "pasted_transcript"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == app_module._PASTE_TOO_LARGE
    # Nothing was stored from a body that was never fully read.
    assert app_module.recstore.list_sources(CLIENT) == []


def test_confirm_body_is_bounded(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module, "_JSON_MAX_BYTES", 32)
    resp = tc.post(
        "/api/sources/2026-01-01-0900/confirm",
        json={"client_id": CLIENT, "verbatim": True, "produced_by": "otter"},
    )
    assert resp.status_code == 413


def test_paste_rejects_a_kind_that_is_not_pasteable(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/paste",
        json={"client_id": CLIENT, "text": "words", "kind": "uploaded_audio"},
    )
    assert resp.status_code == 422


def test_paste_that_needs_confirmation_queues_nothing(client, monkeypatch):
    tc, _ = client
    real = app_module.sources.ingest_text

    def _unsure(text, client_id, kind):
        source, _ = real(text, client_id, kind)
        return source, True

    # The rule is the route's to hold, not the ingest function's. If a paste
    # ever becomes something the app has to ask about, it must queue nothing.
    monkeypatch.setattr(app_module.sources, "ingest_text", _unsure)
    resp = tc.post(
        "/api/sources/paste",
        json={"client_id": CLIENT, "text": "Dana: How was the week?", "kind": "pasted_transcript"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_confirmation"] is True
    assert resp.json()["job_id"] is None
    assert app_module.jobs.list_jobs() == []


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def test_confirm_verbatim_rebuilds_rung0_and_queues(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "otter-transcript-then-summary.txt").json()["source_id"]
    folder = _source_dir(vault_dir, source_id)
    assert not (folder / "transcript-raw.md").exists()

    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": True, "produced_by": "otter"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "transcript_file"
    assert body["verbatim"] is True
    assert body["job_id"]

    assert (folder / "transcript-raw.md").is_file()
    assert (folder / "source-text.md").is_file()
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "transcript_file"
    assert meta["verbatim"] is True
    assert meta["entry_rung"] == 0
    assert meta["produced_by"] == "otter"


def test_confirm_summary_deletes_stale_digest_and_queues(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "otter-transcript-then-summary.txt").json()["source_id"]
    folder = _source_dir(vault_dir, source_id)

    # A previous classification guessed verbatim and got as far as a digest.
    app_module.recstore.write_rung(CLIENT, source_id, 0, "Dana: hello\n")
    app_module.recstore.write_rung(CLIENT, source_id, 1, "Dana: hello\n")
    _seed_digest(source_id)
    assert (folder / "digest.json").is_file()
    assert (folder / "digest.md").is_file()

    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "otter"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "third_party_summary"
    assert body["verbatim"] is False
    assert body["job_id"]

    # The digest was built under the old provenance, so it is gone. Both halves.
    assert not (folder / "digest.json").exists()
    assert not (folder / "digest.md").exists()
    # The transcript files a verbatim guess wrote are gone with it.
    assert not (folder / "transcript-raw.md").exists()
    assert not (folder / "transcript.md").exists()
    # The original text is never touched: it is the only copy of what arrived.
    assert (folder / "source-text.md").is_file()

    record = app_module.jobs.status(body["job_id"])
    assert record["payload"] == {"client_id": CLIENT, "source_id": source_id}


def test_confirm_clinician_notes_stay_the_clinicians(client, vault_dir=None):
    tc, vault_dir = client
    # Prose with no structure classifies as clinician_notes at 0.5 confidence,
    # which is under the confirmation bar, so EVERY set of typed notes arrives
    # at this route. Answering "not word for word, I wrote it" must not relabel
    # the clinician's own observations as somebody else's summary: that kind is
    # written into the filed note's frontmatter as source_kind.
    source_id = _upload_fixture(tc, "clinician-notes.md").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "clinician"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "clinician_notes"
    meta = json.loads(
        (_source_dir(vault_dir, source_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["kind"] == "clinician_notes"
    assert meta["produced_by"] == "clinician"
    assert meta["entry_rung"] == 2

    # A meeting tool's write up is still a third party summary.
    other = _upload_fixture(tc, "otter-transcript-then-summary.txt").json()["source_id"]
    summary = tc.post(
        f"/api/sources/{other}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "otter"},
    )
    assert summary.json()["kind"] == "third_party_summary"


def test_confirm_twice_cancels_the_first_ladder(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "otter-transcript-then-summary.txt").json()["source_id"]
    first = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": True, "produced_by": "otter"},
    ).json()
    second = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "otter"},
    ).json()

    # Two ladders on one source would race each other on the rung files, and
    # the loser would write a digest built under provenance that has changed.
    assert second["cancelled_jobs"] == [first["job_id"]]
    assert app_module.jobs.status(first["job_id"])["state"] == "cancelled"
    live = [
        row
        for row in app_module.jobs.list_jobs(CLIENT)
        if row["state"] not in ("cancelled", "failed", "done")
    ]
    assert [row["id"] for row in live] == [second["job_id"]]


def test_confirm_after_consent_is_withdrawn_keeps_the_capture_time_trail(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "otter-transcript-then-summary.txt").json()["source_id"]
    before = json.loads(
        (_source_dir(vault_dir, source_id) / "meta.json").read_text(encoding="utf-8")
    )["consent_ref"]

    app_module.vault.set_recording_consent(CLIENT, False)
    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "otter"},
    )
    # Correcting what already-stored material IS stays possible after consent is
    # withdrawn: the remedy for withdrawal is deleting the material, and a
    # record whose provenance cannot be corrected is a worse record.
    assert resp.status_code == 200
    after = json.loads(
        (_source_dir(vault_dir, source_id) / "meta.json").read_text(encoding="utf-8")
    )["consent_ref"]
    # The reference describes the consent that was in force when the material
    # was captured. That fact does not change later, so neither does this.
    assert after == before


def test_confirm_audio_source_409(client):
    tc, _ = client
    source_id = _upload(tc, "session.m4a", b"fake audio bytes").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "clinician"},
    )
    assert resp.status_code == 409
    assert "recording" in resp.json()["detail"].lower()


def test_confirm_unknown_source_404(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/2026-01-01-0900/confirm",
        json={"client_id": CLIENT, "verbatim": True, "produced_by": "otter"},
    )
    assert resp.status_code == 404


def test_confirm_rejects_a_bad_client_id_400(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/2026-01-01-0900/confirm",
        json={"client_id": "../escape", "verbatim": True, "produced_by": "otter"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_get_sources_list_enriched(client):
    tc, _ = client
    body = _upload_fixture(tc, "zoom.vtt").json()
    source_id, job_id = body["source_id"], body["job_id"]

    resp = tc.get("/api/sources", params={"client_id": CLIENT})
    assert resp.status_code == 200
    rows = resp.json()["sources"]
    assert len(rows) == 1
    row = rows[0]
    assert row["source_id"] == source_id
    assert row["kind"] == "transcript_file"
    assert row["rungs"] == [0]
    assert row["has_audio"] is False
    assert row["job"]["id"] == job_id
    assert row["job"]["state"] == "queued"
    assert row["job"]["error"] is None

    # The same enrichment on the single-source read.
    one = tc.get(f"/api/sources/{source_id}", params={"client_id": CLIENT})
    assert one.status_code == 200
    assert one.json()["job"]["id"] == job_id
    assert one.json()["rungs"] == [0]


def test_get_sources_carries_the_unanswered_question(client):
    tc, _ = client
    # Closing the tab must not lose the question. It is on disk, so the panel
    # can re-ask after a reload, and an unanswered source is distinguishable
    # from one whose processing quietly failed.
    unsure = _upload_fixture(tc, "clinician-notes.md").json()["source_id"]
    certain = _upload_fixture(tc, "zoom.vtt").json()["source_id"]

    rows = {r["source_id"]: r for r in tc.get("/api/sources", params={"client_id": CLIENT}).json()["sources"]}
    assert rows[unsure]["needs_confirmation"] is True
    assert rows[unsure]["job"] is None, "unanswered means unqueued"
    assert rows[certain]["needs_confirmation"] is False
    one = tc.get(f"/api/sources/{unsure}", params={"client_id": CLIENT}).json()
    assert one["needs_confirmation"] is True

    tc.post(
        f"/api/sources/{unsure}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "clinician"},
    )
    rows = {r["source_id"]: r for r in tc.get("/api/sources", params={"client_id": CLIENT}).json()["sources"]}
    assert rows[unsure]["needs_confirmation"] is False
    assert rows[unsure]["job"]["state"] == "queued"
    assert tc.get(f"/api/sources/{unsure}", params={"client_id": CLIENT}).json()[
        "needs_confirmation"
    ] is False


def test_get_sources_requires_a_client_id(client):
    tc, _ = client
    assert tc.get("/api/sources").status_code == 400
    assert tc.get("/api/sources", params={"client_id": "../escape"}).status_code == 400


def test_get_source_404_when_unknown(client):
    tc, _ = client
    resp = tc.get("/api/sources/2026-01-01-0900", params={"client_id": CLIENT})
    assert resp.status_code == 404


def test_get_digest_404_before_built_200_after(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]

    missing = tc.get(f"/api/sources/{source_id}/digest", params={"client_id": CLIENT})
    assert missing.status_code == 404
    detail = missing.json()["detail"]
    assert "not ready" in detail.lower()
    assert "—" not in detail

    _seed_digest(source_id)
    found = tc.get(f"/api/sources/{source_id}/digest", params={"client_id": CLIENT})
    assert found.status_code == 200
    assert found.json()["themes"] == ["rumination"]
    assert found.json()["source_id"] == source_id


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_source_cancels_active_job_and_trashes(client):
    tc, vault_dir = client
    body = _upload_fixture(tc, "zoom.vtt").json()
    source_id, job_id = body["source_id"], body["job_id"]

    resp = tc.request(
        "DELETE", f"/api/sources/{source_id}", params={"client_id": CLIENT}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert resp.json()["cancelled_jobs"] == [job_id]

    # A running ladder must not resurrect files mid-delete.
    assert app_module.jobs.status(job_id)["state"] == "cancelled"
    assert not _source_dir(vault_dir, source_id).exists()
    # Through the trash, so the 30-day grace period still applies.
    assert app_module.records.list_trash()


def test_delete_audio_only(client):
    tc, vault_dir = client
    source_id = _upload(tc, "session.m4a", b"fake audio bytes").json()["source_id"]
    folder = _source_dir(vault_dir, source_id)
    assert (folder / "source.m4a").is_file()

    resp = tc.request(
        "DELETE",
        f"/api/sources/{source_id}",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not (folder / "source.m4a").exists()
    # The evidence trail outlives the audio.
    assert (folder / "meta.json").is_file()

    # Nothing left to delete the second time.
    again = tc.request(
        "DELETE",
        f"/api/sources/{source_id}",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert again.status_code == 404


def test_delete_audio_only_leaves_the_ladder_running(client):
    tc, _ = client
    body = _upload(tc, "session.m4a", b"fake audio bytes").json()
    job_id = body["job_id"]

    resp = tc.request(
        "DELETE",
        f"/api/sources/{body['source_id']}",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert resp.status_code == 200
    # Cancelling here would leave the source with no audio AND no transcript,
    # which is the evidence trail being deleted by the route that promises to
    # keep it. The ladder is left alone; if it now cannot find the audio it
    # fails out loud, which is recoverable.
    assert resp.json()["cancelled_jobs"] == []
    assert app_module.jobs.status(job_id)["state"] == "queued"


def test_delete_404_has_no_side_effects(client):
    tc, _ = client
    # A .vtt source never has audio, so audio_only on it is a 404. It must not
    # take the pending ladder down on the way out.
    body = _upload_fixture(tc, "zoom.vtt").json()
    resp = tc.request(
        "DELETE",
        f"/api/sources/{body['source_id']}",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert resp.status_code == 404
    assert app_module.jobs.status(body["job_id"])["state"] == "queued"


def test_delete_unknown_source_404(client):
    tc, _ = client
    resp = tc.request(
        "DELETE", "/api/sources/2026-01-01-0900", params={"client_id": CLIENT}
    )
    assert resp.status_code == 404


def test_delete_audio_only_on_a_missing_source_says_it_is_missing(client):
    tc, _ = client
    resp = tc.request(
        "DELETE",
        "/api/sources/2026-01-01-0900",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert resp.status_code == 404
    # "There is no audio stored for that source" would tell a clinician their
    # material is filed and merely missing its audio, when it is not there.
    assert resp.json()["detail"] == "No such source for this client."

    # And the other 404 still says what it means when the source does exist.
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    no_audio = tc.request(
        "DELETE",
        f"/api/sources/{source_id}",
        params={"client_id": CLIENT, "audio_only": "true"},
    )
    assert no_audio.status_code == 404
    assert "no audio" in no_audio.json()["detail"].lower()


def test_delete_rejects_a_traversal_source_id_400(client, vault_dir=None):
    tc, vault_dir = client
    # Ids that survive URL normalisation and still reach the route. A plain
    # "../escape" is collapsed by the client before it is ever sent.
    # Refused is refused: the router turning a separator into a path that
    # matches nothing (405) is as good an answer as the id validator saying no.
    for bad in (".hidden", "%2E%2E", "..%2Fescape", "sub%2Fdir"):
        resp = tc.request("DELETE", f"/api/sources/{bad}", params={"client_id": CLIENT})
        assert resp.status_code in (400, 404, 405), f"{bad} -> {resp.status_code}"
    # And the same on the client id, which is the other half of the path.
    resp = tc.request(
        "DELETE", "/api/sources/2026-01-01-0900", params={"client_id": "../escape"}
    )
    assert resp.status_code == 400
    assert vault_dir.parent.joinpath("escape").exists() is False


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


def test_consent_route_sets_and_withdraws_dated_flag(client):
    tc, _ = client
    granted = tc.post(
        f"/api/clients/{NO_CONSENT_CLIENT}/recording-consent", json={"granted": True}
    )
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["granted"] is True
    assert body["date"]
    # The copy says whose responsibility consent is, and offers no legal advice.
    note = body["note"].lower()
    assert "your responsibility" in note
    assert not any(word in note for word in ("law", "legal", "jurisdiction", "consult"))
    assert "—" not in body["note"]
    assert app_module.vault.has_recording_consent(NO_CONSENT_CLIENT) is True

    withdrawn = tc.post(
        f"/api/clients/{NO_CONSENT_CLIENT}/recording-consent", json={"granted": False}
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["granted"] is False
    # The date it was granted on survives: it is the audit trail.
    assert withdrawn.json()["date"] == body["date"]
    assert app_module.vault.has_recording_consent(NO_CONSENT_CLIENT) is False


def test_consent_route_rejects_a_missing_flag_and_a_missing_client(client):
    tc, _ = client
    assert tc.post(f"/api/clients/{CLIENT}/recording-consent", json={}).status_code == 400
    assert (
        tc.post(f"/api/clients/{CLIENT}/recording-consent", json={"granted": "yes"}).status_code
        == 400
    )
    assert (
        tc.post("/api/clients/C-9999/recording-consent", json={"granted": True}).status_code == 404
    )


def test_consent_route_409_on_unreadable_frontmatter(client):
    tc, vault_dir = client
    profile = vault_dir / "Clients" / CLIENT / "_Profile.md"
    profile.write_text("---\n[not, properties]\n---\n\nBody.\n", encoding="utf-8")
    resp = tc.post(f"/api/clients/{CLIENT}/recording-consent", json={"granted": True})
    assert resp.status_code == 409
    assert "_Profile.md" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_jobs_endpoint_lists_and_surfaces_worker_error(client, monkeypatch):
    tc, _ = client
    job_id = _upload_fixture(tc, "zoom.vtt").json()["job_id"]
    monkeypatch.setattr(app_module.jobs, "_LAST_WORKER_ERROR", "could not write a job record")

    resp = tc.get("/api/jobs", params={"client_id": CLIENT})
    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body["jobs"]] == [job_id]
    assert body["worker_error"] == "could not write a job record"


# ---------------------------------------------------------------------------
# The debrief route's source linkage
# ---------------------------------------------------------------------------


def test_debrief_accepts_source_id(client, monkeypatch, tmp_path):
    tc, _ = client
    seen: dict = {}

    def _fake_convert(_data, tmp):
        wav = Path(tmp) / "in.wav"
        wav.write_bytes(b"RIFF")
        return str(wav)

    def _fake_pipeline(wav_path, client_id, source_id=None):
        seen.update({"wav": wav_path, "client_id": client_id, "source_id": source_id})
        return {"client_id": client_id, "actions": []}

    monkeypatch.setattr(app_module, "_convert_to_wav", _fake_convert)
    monkeypatch.setattr(app_module.pipeline, "transcribe_and_extract", _fake_pipeline)

    resp = tc.post(
        "/api/debrief",
        files={"audio": ("debrief.webm", b"audio bytes", "audio/webm")},
        data={"client_id": CLIENT, "source_id": "2026-07-25-1430"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["client_id"] == CLIENT
    assert seen["source_id"] == "2026-07-25-1430"

    # No source_id is still the hero flow, unchanged.
    seen.clear()
    resp = tc.post(
        "/api/debrief",
        files={"audio": ("debrief.webm", b"audio bytes", "audio/webm")},
        data={"client_id": CLIENT},
    )
    assert resp.status_code == 200
    assert seen["source_id"] is None


def test_debrief_rejects_a_bad_source_id_400(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(app_module.pipeline, "transcribe_and_extract", lambda *a, **k: {})
    resp = tc.post(
        "/api/debrief",
        files={"audio": ("debrief.webm", b"audio bytes", "audio/webm")},
        data={"client_id": CLIENT, "source_id": "../escape"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_lifespan_runs_retention_sweep_and_resume(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.records, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)

    calls: list[str] = []
    monkeypatch.setattr(
        app_module.recstore, "sweep_retention", lambda now: calls.append("sweep") or 0
    )
    monkeypatch.setattr(app_module.jobs, "resume_pending", lambda: calls.append("resume") or 0)

    with TestClient(app_module.app):
        pass
    assert calls == ["sweep", "resume"]


def test_lifespan_survives_a_failing_sweep(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.records, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)

    resumed: list[str] = []

    def _explode(_now):
        raise OSError("the vault went away")

    monkeypatch.setattr(app_module.recstore, "sweep_retention", _explode)
    monkeypatch.setattr(app_module.jobs, "resume_pending", lambda: resumed.append("resume") or 0)

    # A broken sweep must not stop the app booting, nor stop the resume.
    with TestClient(app_module.app):
        pass
    assert resumed == ["resume"]


def test_confirm_a_reference_source_422_and_leaves_its_ladder_alone(client):
    """A stale confirm click must not kill the run building a reference digest.

    reclassify refuses this too, but by the time it is reached the route has
    already cancelled the source's jobs, and nothing queues them again. So the
    refusal happens before the cancellation, not after it.
    """
    tc, _ = client
    body = _upload_fixture(
        tc, "clinician-notes.md", declared_kind="reference_document"
    ).json()
    source_id, job_id = body["source_id"], body["job_id"]
    assert body["kind"] == "reference_document"
    assert body["needs_confirmation"] is False
    assert job_id, "the ladder is queued, and this is the job that must survive"

    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "clinician"},
    )

    assert resp.status_code == 422
    assert "reference material" in resp.json()["detail"]
    assert app_module.jobs.status(job_id)["state"] != "cancelled"
    live = [
        row
        for row in app_module.jobs.list_jobs(CLIENT)
        if row["state"] not in ("cancelled", "failed", "done")
    ]
    assert [row["id"] for row in live] == [job_id]
    # And the source is exactly as it was.
    assert app_module.recstore.read_meta(CLIENT, source_id)["kind"] == "reference_document"


# ---------------------------------------------------------------------------
# Draft a note from a source
# ---------------------------------------------------------------------------


def test_draft_returns_a_no_debrief_plan(client, monkeypatch):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    _seed_digest(source_id)
    _stub_extraction(monkeypatch)

    resp = tc.post(f"/api/sources/{source_id}/draft", json={"client_id": CLIENT})
    assert resp.status_code == 200, resp.text
    plan = resp.json()

    # The review screen already knows this shape: a note, no actions, no debrief.
    assert plan["no_debrief"] is True
    assert plan["actions"] == []
    assert plan["deduped_actions"] == []
    assert plan["transcript"] == ""
    assert plan["digest_used"] is True
    assert plan["note"]["data"]
    assert plan["session_meta"]["source_id"] == source_id
    assert plan["session_meta"]["session_date"] == _today()


def test_draft_while_the_ladder_is_still_running_409(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]

    resp = tc.post(f"/api/sources/{source_id}/draft", json={"client_id": CLIENT})
    assert resp.status_code == 409
    assert "still processing" in resp.json()["detail"]


def test_draft_of_an_unconfirmed_source_409(client):
    """Unconfirmed provenance must not become a note, digest or no digest."""
    tc, _ = client
    body = _upload_fixture(tc, "clinician-notes.md").json()
    assert body["needs_confirmation"] is True
    _seed_digest(body["source_id"])

    resp = tc.post(f"/api/sources/{body['source_id']}/draft", json={"client_id": CLIENT})
    assert resp.status_code == 409
    assert "not sure what that material is" in resp.json()["detail"]


def test_draft_unknown_source_404(client):
    tc, _ = client
    resp = tc.post("/api/sources/2026-01-01-0900/draft", json={"client_id": CLIENT})
    assert resp.status_code == 404


def test_draft_rejects_a_bad_client_id_400(client):
    tc, _ = client
    resp = tc.post("/api/sources/2026-01-01-0900/draft", json={"client_id": "../escape"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Connect a source to a note that is already filed
# ---------------------------------------------------------------------------


def test_assign_appends_one_amendment_and_leaves_the_note_above_it_alone(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    rel = _seed_note(vault_dir)
    before = (vault_dir / rel).read_text(encoding="utf-8")

    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "linked": True,
        "note_path": rel,
        "already_linked": False,
        "linked_notes": [rel],
        "amendment": body["amendment"],
    }

    after = (vault_dir / rel).read_text(encoding="utf-8")
    # Everything the note said before is still there, unchanged, in order. The
    # amend machinery strips the file's trailing newline before appending, and
    # that is the only difference above the amendment.
    kept = before.rstrip("\n")
    assert after.startswith(kept)
    assert "## Amendment" in after[len(kept):]
    assert after.count("## Amendment") == 1

    line = (
        f"Session evidence connected: a transcript from {_today()}, stored in "
        f"the vault at Recordings/{CLIENT}/{source_id}/. Word for word "
        "transcript and summary live beside it."
    )
    assert line in after
    assert body["amendment"] == line
    assert "—" not in after


def test_assign_a_second_time_appends_nothing(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    rel = _seed_note(vault_dir)

    tc.post(f"/api/sources/{source_id}/assign", json={"client_id": CLIENT, "note_path": rel})
    once = (vault_dir / rel).read_text(encoding="utf-8")
    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )

    assert resp.status_code == 200
    assert resp.json()["already_linked"] is True
    assert (vault_dir / rel).read_text(encoding="utf-8") == once
    assert app_module.recstore.read_meta(CLIENT, source_id)["linked_notes"] == [rel]


def test_assign_a_reference_source_says_reference_material(client):
    tc, vault_dir = client
    source_id = _upload_fixture(
        tc, "clinician-notes.md", declared_kind="reference_document"
    ).json()["source_id"]
    rel = _seed_note(vault_dir)

    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )
    assert resp.status_code == 200
    text = (vault_dir / rel).read_text(encoding="utf-8")
    # It is not session evidence and the record does not say it is.
    assert "Reference material connected: a document from" in text
    assert "Session evidence connected" not in text
    assert "The text and summary live beside it." in text


@pytest.mark.parametrize(
    "note_path",
    ["../../etc/passwd", "/etc/passwd", "Clients/../../escape.md", "~/notes.md"],
)
def test_assign_refuses_a_path_that_leaves_the_vault(client, note_path):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": note_path},
    )
    assert resp.status_code == 400


def test_assign_refuses_an_empty_note_path(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": "   "},
    )
    assert resp.status_code == 400


def test_assign_another_clients_note_404(client):
    """A valid vault path that is not this client's record is not found here."""
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    foreign = _seed_note(vault_dir, client_id=NO_CONSENT_CLIENT)

    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": foreign},
    )
    assert resp.status_code == 404
    assert "## Amendment" not in (vault_dir / foreign).read_text(encoding="utf-8")


def test_assign_a_note_that_is_not_there_404(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": f"Clients/{CLIENT}/Sessions/2020-01-01-session.md"},
    )
    assert resp.status_code == 404


def test_assign_unknown_source_404(client):
    tc, vault_dir = client
    rel = _seed_note(vault_dir)
    resp = tc.post(
        "/api/sources/2026-01-01-0900/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )
    assert resp.status_code == 404


def test_assign_after_consent_is_withdrawn_still_documents_the_material(client):
    """The material is already in the record's custody. This only says where."""
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    rel = _seed_note(vault_dir)
    app_module.vault.set_recording_consent(CLIENT, False)

    resp = tc.post(
        f"/api/sources/{source_id}/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )
    assert resp.status_code == 200
    assert "## Amendment" in (vault_dir / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The session date
# ---------------------------------------------------------------------------


def test_session_date_round_trips_into_the_row(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]

    resp = tc.patch(
        f"/api/sources/{source_id}/session-date",
        json={"client_id": CLIENT, "session_date": "2026-07-20"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_date"] == "2026-07-20"
    # captured_at answers a different question and is nobody's to edit.
    assert resp.json()["captured_at"].startswith(_today())

    rows = tc.get("/api/sources", params={"client_id": CLIENT}).json()["sources"]
    assert rows[0]["session_date"] == "2026-07-20"


@pytest.mark.parametrize("value", ["20 July 2026", "2026-13-40", "", None, "20260720"])
def test_session_date_refuses_what_it_cannot_read(client, value):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    resp = tc.patch(
        f"/api/sources/{source_id}/session-date",
        json={"client_id": CLIENT, "session_date": value},
    )
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.json()["detail"]


def test_session_date_unknown_source_404(client):
    tc, _ = client
    resp = tc.patch(
        "/api/sources/2026-01-01-0900/session-date",
        json={"client_id": CLIENT, "session_date": "2026-07-20"},
    )
    assert resp.status_code == 404


def test_the_listing_carries_an_effective_date_and_its_links(client):
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    rel = _seed_note(vault_dir)

    row = tc.get("/api/sources", params={"client_id": CLIENT}).json()["sources"][0]
    # Never stated, so it falls back to the day the material arrived.
    assert row["session_date"] == _today()
    assert row["linked_notes"] == []

    tc.post(f"/api/sources/{source_id}/assign", json={"client_id": CLIENT, "note_path": rel})
    row = tc.get("/api/sources", params={"client_id": CLIENT}).json()["sources"][0]
    assert row["linked_notes"] == [rel]
    one = tc.get(f"/api/sources/{source_id}", params={"client_id": CLIENT}).json()
    assert one["linked_notes"] == [rel]
    assert one["session_date"] == _today()


def test_a_drafted_plan_files_through_the_existing_review_flow(client, monkeypatch):
    """Draft, then approve: the plan goes straight into /api/execute unchanged.

    The point of the draft path is that it produces the plan shape the review
    screen already files. Nothing is stubbed between the two calls except the
    model and the surfaces that would open on a real Mac.
    """
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    _seed_digest(source_id)
    _stub_extraction(monkeypatch)
    monkeypatch.setattr(app_module.vault, "obsidian_available", lambda: False)

    plan = tc.post(f"/api/sources/{source_id}/draft", json={"client_id": CLIENT}).json()
    plan["verify"] = False
    resp = tc.post("/api/execute", json=plan)

    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["note_path"], result["errors"]
    text = Path(result["note_path"]).read_text(encoding="utf-8")
    assert f"source_id: {source_id}" in text
    assert f"session_date: '{_today()}'" in text
    # No debrief happened, and the note says so instead of holding an empty
    # transcript block.
    assert "## Session Transcript" not in text
    assert "No debrief was recorded." in text
    assert result["actions"] == []


def test_assign_of_an_unconfirmed_source_is_refused(client):
    """An amendment names what the material is, forever. A guess may not.

    Withdrawn consent does not block a link, because linking only documents
    material the record already holds. An unanswered classification is a
    different thing: the sentence appended to the note asserts a provenance, and
    nobody has vouched for this one yet.
    """
    tc, vault_dir = client
    body = _upload_fixture(tc, "clinician-notes.md").json()
    assert body["needs_confirmation"] is True
    rel = _seed_note(vault_dir)

    resp = tc.post(
        f"/api/sources/{body['source_id']}/assign",
        json={"client_id": CLIENT, "note_path": rel},
    )
    assert resp.status_code == 422
    assert "not sure what that material is" in resp.json()["detail"]
    assert "## Amendment" not in (vault_dir / rel).read_text(encoding="utf-8")


def test_a_link_survives_the_source_changing_kind(client):
    """Flipping what material IS does not un-say where it ended up."""
    tc, vault_dir = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    rel = _seed_note(vault_dir)
    linked = tc.post(
        f"/api/sources/{source_id}/assign", json={"client_id": CLIENT, "note_path": rel}
    )
    assert linked.status_code == 200, linked.text

    # The clinician decides it was a write up after all. reclassify rewrites the
    # provenance and carries everything it does not own across with it.
    resp = tc.post(
        f"/api/sources/{source_id}/confirm",
        json={"client_id": CLIENT, "verbatim": False, "produced_by": "otter"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "third_party_summary"

    assert app_module.recstore.read_meta(CLIENT, source_id)["linked_notes"] == [rel]
    row = tc.get(f"/api/sources/{source_id}", params={"client_id": CLIENT}).json()
    assert row["linked_notes"] == [rel]


# ---------------------------------------------------------------------------
# Reference material: what the source is FOR, flipped either way
# ---------------------------------------------------------------------------


def test_flip_to_reference_drops_the_stale_digest_and_rebuilds(client):
    tc, _ = client
    body = _upload_fixture(tc, "zoom.vtt").json()
    source_id, first_job = body["source_id"], body["job_id"]
    assert body["kind"] == "transcript_file"
    _seed_digest(source_id)
    assert 2 in app_module.recstore.rungs_present(CLIENT, source_id)

    resp = tc.post(
        f"/api/sources/{source_id}/reference",
        json={"client_id": CLIENT, "reference": True},
    )

    assert resp.status_code == 200, resp.text
    row = resp.json()
    # Word for word is a fact about the bytes and survives the flip.
    assert row["kind"] == "reference_recording"
    assert row["verbatim"] is True
    assert row["entry_rung"] == 0
    assert row["needs_confirmation"] is False

    # The job building the session digest is stopped, and a fresh one queued.
    # Both jobs are made in the same second, so this is also where the row's
    # job pointer has to be right: it names the work that is actually running,
    # and pointing it at the job just cancelled would have the UI report a dead
    # job forever, with no poll able to correct it.
    assert row["cancelled_jobs"] == [first_job]
    assert app_module.jobs.status(first_job)["state"] == "cancelled"
    assert row["job_id"] and row["job_id"] != first_job
    assert row["job_error"] is None
    assert row["job"] is not None
    assert row["job"]["id"] == row["job_id"], "the row points at the new job, not the dead one"
    assert row["job"]["state"] != "cancelled"
    live = [
        job
        for job in app_module.jobs.list_jobs(CLIENT)
        if job["state"] not in ("cancelled", "failed", "done")
    ]
    assert [job["id"] for job in live] == [row["job_id"]]
    assert live[0]["payload"] == {"client_id": CLIENT, "source_id": source_id}

    # A session digest under reference material is stale evidence, so it goes.
    assert 2 not in app_module.recstore.rungs_present(CLIENT, source_id)
    assert 0 in app_module.recstore.rungs_present(CLIENT, source_id)


def test_flip_back_restores_a_session_kind(client):
    """The kind it arrived as is gone, so the answer is read off the disk."""
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    tc.post(f"/api/sources/{source_id}/reference", json={"client_id": CLIENT, "reference": True})

    resp = tc.post(
        f"/api/sources/{source_id}/reference",
        json={"client_id": CLIENT, "reference": False},
    )

    assert resp.status_code == 200, resp.text
    row = resp.json()
    # Word for word with no audio on disk is a transcript, per the back flip rule.
    assert row["kind"] == "transcript_file"
    assert row["verbatim"] is True
    assert row["job_id"]


def test_a_same_direction_flip_costs_nothing(client):
    """A second click must not cost a summary and the work to rebuild it."""
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    first = tc.post(
        f"/api/sources/{source_id}/reference", json={"client_id": CLIENT, "reference": True}
    ).json()
    _seed_digest(source_id)

    resp = tc.post(
        f"/api/sources/{source_id}/reference",
        json={"client_id": CLIENT, "reference": True},
    )

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["kind"] == "reference_recording"
    assert row["job_id"] is None
    assert row["cancelled_jobs"] == []
    # Nothing was stopped, and the digest that was there is still there.
    assert app_module.jobs.status(first["job_id"])["state"] != "cancelled"
    assert 2 in app_module.recstore.rungs_present(CLIENT, source_id)


def test_a_recording_flipped_to_reference_is_a_reference_recording(client):
    tc, _ = client
    body = _upload(tc, "session.m4a", b"fake audio bytes").json()
    assert body["kind"] == "uploaded_audio"

    resp = tc.post(
        f"/api/sources/{body['source_id']}/reference",
        json={"client_id": CLIENT, "reference": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "reference_recording"


def test_a_write_up_flipped_to_reference_is_a_reference_document(client):
    tc, _ = client
    source_id = _upload_fixture(
        tc, "clinician-notes.md", declared_kind="clinician_notes"
    ).json()["source_id"]

    resp = tc.post(
        f"/api/sources/{source_id}/reference",
        json={"client_id": CLIENT, "reference": True},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["kind"] == "reference_document"
    assert row["verbatim"] is False
    assert row["entry_rung"] == 2


def test_reference_flip_needs_a_yes_or_no(client):
    tc, _ = client
    source_id = _upload_fixture(tc, "zoom.vtt").json()["source_id"]
    resp = tc.post(
        f"/api/sources/{source_id}/reference",
        json={"client_id": CLIENT, "reference": "yes please"},
    )
    assert resp.status_code == 400
    assert "reference material" in resp.json()["detail"]


def test_reference_flip_unknown_source_404(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/2026-01-01-0900/reference",
        json={"client_id": CLIENT, "reference": True},
    )
    assert resp.status_code == 404


def test_reference_flip_rejects_a_bad_client_id_400(client):
    tc, _ = client
    resp = tc.post(
        "/api/sources/2026-01-01-0900/reference",
        json={"client_id": "../escape", "reference": True},
    )
    assert resp.status_code == 400
