"""Tests for debrief.jobs: the queue, the pause, and the failures that show.

Everything runs against a scratch vault in tmp_path, per tests/test_recstore.py.
The real DebriefVault at the repo root is never touched, and no test may reach a
model: ladder.build is stubbed in every test that runs a job, and the llm and stt
seams underneath it are wired to fail loudly if anything ever gets that far.

Three groups carry the weight:

  determinism     jobs.py owns a worker thread, so a test that wants a fixed
                  order disables it and calls _run_job on the calling thread.
                  Tests that need the real thread synchronise on events and wait
                  for a stated condition. Nothing here sleeps and then asserts.
  the pause       a clinician recording a debrief takes the model off background
                  work at the next rung boundary and gives it back afterwards.
  the failures    this codebase once cleared an error one line after setting it.
                  A failed job has to keep its error, its stage, and its files.

No test leaves a worker thread running: _reset_state runs before and after every
one of them, so a thread can never outlive the vault it was pointed at.
"""

from __future__ import annotations

import datetime as dt
import json
import queue
import re
import threading
import time

import pytest

import debrief.config as config
import debrief.jobs as jobs
import debrief.ladder as ladder
import debrief.llm as llm
import debrief.records as records_mod
import debrief.recstore as recstore
import debrief.sources as sources
import debrief.stt as stt
import debrief.vault as vault_mod

CLIENT = "C-0001"
SID = "2026-07-25-1430"
OTHER_SID = "2026-07-26-0900"

# Every wait for a real worker thread. Generous on purpose: a slow machine must
# make a test slower, never flaky.
TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _live_workers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "debrief-jobs" and t.is_alive()]


@pytest.fixture(autouse=True, scope="module")
def _quarantine_vault(tmp_path_factory):
    """A scratch vault underneath every test in this file, for the whole module.

    This file is the one that runs real threads, and a thread outliving its test
    is the one way a scratch vault stops protecting anything: the per-test patch
    of config.VAULT_DIR is undone when the test ends, and a worker still alive a
    moment later would write its record to the repo's own DebriefVault.

    So the module patches VAULT_DIR too, and only puts it back once every test
    has finished. A leaked thread lands here rather than in the real vault, and
    the teardown assertion below is what actually names the leak.
    """
    quarantine = tmp_path_factory.mktemp("jobs-quarantine")
    patch = pytest.MonkeyPatch()
    patch.setattr(config, "VAULT_DIR", quarantine)
    yield quarantine
    patch.undo()


@pytest.fixture(autouse=True)
def _clean_jobs(monkeypatch):
    """No test starts with, or leaves behind, a worker thread or a queued job.

    monkeypatch is requested so this fixture tears down before monkeypatch puts
    config.VAULT_DIR back: a worker still running at that moment would be
    pointed at a vault that is no longer the one under test.

    The teardown waits longer than any stub in this file is allowed to block,
    and then asserts the thread is really gone. A leak that is only reported by
    the next test to behave oddly is a leak nobody finds.
    """
    jobs._reset_state(timeout=TIMEOUT + 5)
    yield
    jobs._reset_state(timeout=TIMEOUT + 5)
    assert not _live_workers(), "a worker thread outlived the test that started it"


@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)  # records.py binds it at import
    vault_mod.ensure_vault()
    return vault_dir


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """The model seams under the ladder are wired to shout, never to run."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("a test reached the model. Stub ladder.build instead.")

    monkeypatch.setattr(llm, "chat", _forbidden)
    monkeypatch.setattr(stt, "transcribe_timed", _forbidden)
    monkeypatch.setattr(stt, "correct_long_transcript", _forbidden)


@pytest.fixture()
def no_worker(monkeypatch):
    """Determinism: submit() still queues, but only _run_job runs anything."""
    monkeypatch.setattr(jobs, "_worker_enabled", False)


class _Gate:
    """A release valve for every stub in this file that blocks a thread.

    A stub that waits on a test's own event keeps its thread alive for the full
    timeout when the test fails before setting it, and a thread that outlives
    its test outlives the scratch vault it was pointed at. So every blocking
    stub waits through here instead, and teardown opens the valve and joins
    whatever it started, however the test ended.
    """

    def __init__(self) -> None:
        self.valve = threading.Event()
        self.threads: list[threading.Thread] = []

    def spawn(self, target, *args) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def wait(self, own: threading.Event) -> bool:
        """True when the test released this stub, False when teardown did."""
        deadline = time.monotonic() + TIMEOUT
        while not self.valve.is_set() and time.monotonic() < deadline:
            if own.wait(0.01):
                return True
        return own.is_set()

    def release(self) -> None:
        self.valve.set()
        for thread in self.threads:
            thread.join(TIMEOUT + 5)


@pytest.fixture()
def gate(monkeypatch):
    """The release valve, closed at teardown before the vault is put back.

    monkeypatch is requested so this tears down before config.VAULT_DIR is
    restored: a stub released here writes its last record to the scratch vault,
    which is the whole point.
    """
    valve = _Gate()
    try:
        yield valve
    finally:
        valve.release()


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


def _seed_source(source_id: str = SID, **overrides) -> sources.Source:
    """Put one source's provenance on disk, which is all _run_ladder reads."""
    source = _source(id=source_id, **overrides)
    recstore.write_meta(source.client_id, source_id, source.to_meta())
    return source


def _payload(source_id: str = SID, client_id: str = CLIENT) -> dict:
    return {"client_id": client_id, "source_id": source_id}


def _seed_record(job_id: str, state: str, **extra) -> dict:
    """Write a job record straight to disk, the way a previous run left one.

    created_at is derived from the id so a seeded record orders the way a real
    one would.
    """
    stamp = f"{job_id[0:4]}-{job_id[4:6]}-{job_id[6:8]}T{job_id[9:11]}:{job_id[11:13]}:00+00:00"
    record = {
        "id": job_id,
        "kind": "ladder",
        "payload": _payload(),
        "priority": 5,
        "state": state,
        "stage": None,
        "done": 0,
        "total": 0,
        "error": None,
        "created_at": stamp,
        "updated_at": stamp,
        "interrupted": False,
    }
    record.update(extra)
    path = jobs._jobs_dir() / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _drain() -> list[str]:
    """Run everything on the queue, in queue order, on this thread."""
    ran: list[str] = []
    while True:
        try:
            _, _, job_id = jobs._QUEUE.get_nowait()
        except queue.Empty:
            return ran
        jobs._QUEUE.task_done()
        jobs._run_job(job_id)
        ran.append(job_id)


def _wait_until(predicate, timeout: float = TIMEOUT, what: str = "condition"):
    """Block until predicate() is true, or fail the test saying what was awaited.

    A wait for the thing the test expects, never a sleep followed by an
    assertion: when the expected thing happens this returns at once, and when it
    does not the test fails rather than passing on a lucky interleaving.
    """
    deadline = time.monotonic() + timeout
    tick = threading.Event()
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        tick.wait(0.01)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _state(job_id: str):
    record = jobs.status(job_id)
    return None if record is None else record.get("state")


def _wait_for_state(job_id: str, state: str) -> dict:
    _wait_until(lambda: _state(job_id) == state, what=f"job {job_id} to be {state}")
    return jobs.status(job_id)


# ---------------------------------------------------------------------------
# Submitting and persisting
# ---------------------------------------------------------------------------


def test_submit_persists_record_and_returns_sortable_id(scratch_vault, no_worker):
    before = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_id = jobs.submit("ladder", _payload(), priority=3)
    after = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")

    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", job_id), job_id
    assert before <= job_id[:15] <= after, "the id carries the time it was made"

    path = scratch_vault / ".debrief_staging" / "jobs" / f"{job_id}.json"
    assert path.is_file(), "the record is a json file under the staging jobs folder"

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["id"] == job_id
    assert record["kind"] == "ladder"
    assert record["payload"] == _payload()
    assert record["priority"] == 3
    assert record["state"] == "queued"
    assert record["stage"] is None
    assert record["done"] == 0
    assert record["total"] == 0
    assert record["error"] is None
    assert record["interrupted"] is False
    assert record["created_at"] and record["updated_at"]

    assert jobs.status(job_id) == record
    # A second submit in the same second still gets its own id and its own file.
    other = jobs.submit("ladder", _payload(OTHER_SID))
    assert other != job_id
    assert jobs.status(other)["payload"]["source_id"] == OTHER_SID


def test_status_of_an_unknown_job_is_none(scratch_vault, no_worker):
    assert jobs.status("20260725-101010-abcdef") is None
    assert jobs.status("../../etc/passwd") is None
    assert jobs.cancel("20260725-101010-abcdef") is False


def test_the_id_a_reader_gets_is_the_id_it_can_cancel(scratch_vault, no_worker):
    """The file is the truth. A record claiming another id does not redirect."""
    _seed_record("20260725-100000-000abc", "queued", id="somebody-elses-id")

    record = jobs.status("20260725-100000-000abc")
    assert record["id"] == "20260725-100000-000abc"
    assert [row["id"] for row in jobs.list_jobs()] == ["20260725-100000-000abc"]

    assert jobs.cancel("20260725-100000-000abc") is True
    assert jobs.status("20260725-100000-000abc")["state"] == "cancelled"
    assert jobs.status("somebody-elses-id") is None, "no second file was written"


def test_a_record_can_never_be_written_outside_the_jobs_folder(scratch_vault, no_worker):
    for bad in ("../../evil", "..", "a/b", "/etc/passwd", ".hidden", ""):
        with pytest.raises(jobs.JobError):
            jobs._write({"id": bad, "state": "queued"})

    assert not (scratch_vault / "evil.json").exists()
    assert not (scratch_vault.parent / "evil.json").exists()
    assert list(jobs._jobs_dir().glob("*")) == [] or not jobs._jobs_dir().exists()


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------


def test_run_job_executes_ladder_and_marks_done(scratch_vault, no_worker, monkeypatch):
    _seed_source()
    seen: dict = {}

    def _build(source, client_ctx, progress=None, between_rungs=None):
        seen["source"] = source
        seen["ctx"] = client_ctx
        progress("transcribing", 1, 1)
        between_rungs()
        progress("digesting", 4, 4)
        return {"source_id": source.id, "themes": ["sleep"]}

    monkeypatch.setattr(ladder, "build", _build)

    job_id = jobs.submit("ladder", _payload())
    assert _drain() == [job_id]

    record = jobs.status(job_id)
    assert record["state"] == "done"
    assert record["error"] is None
    assert record["stage"] == "digesting"
    assert (record["done"], record["total"]) == (4, 4)
    assert seen["source"].id == SID
    assert seen["source"].client_id == CLIENT
    assert seen["ctx"]["client_id"] == CLIENT, "the vault context reaches the ladder"


def test_priority_orders_queue(scratch_vault, no_worker, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: order.append(source.id) or {},
    )
    for source_id in ("s-first", "s-urgent", "s-second"):
        _seed_source(source_id)

    first = jobs.submit("ladder", _payload("s-first"), priority=5)
    urgent = jobs.submit("ladder", _payload("s-urgent"), priority=1)
    second = jobs.submit("ladder", _payload("s-second"), priority=5)

    assert _drain() == [urgent, first, second], "lower number first, then FIFO"
    assert order == ["s-urgent", "s-first", "s-second"]


def test_unknown_kind_fails_clearly(scratch_vault, no_worker):
    job_id = jobs.submit("sandwich", {"client_id": CLIENT})
    _drain()

    record = jobs.status(job_id)
    assert record["state"] == "failed"
    assert "sandwich" in record["error"]
    assert "ladder" in record["error"], "the message says what kinds there are"


def test_context_failure_does_not_fail_job(scratch_vault, no_worker, monkeypatch):
    _seed_source()
    seen: dict = {}

    def _no_context(client_id):
        raise FileNotFoundError(f"No profile for client {client_id!r}")

    monkeypatch.setattr(vault_mod, "client_context", _no_context)
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: seen.update(ctx=ctx) or {},
    )

    job_id = jobs.submit("ladder", _payload())
    _drain()

    assert seen["ctx"] == {}, "a context that cannot be read is empty, not fatal"
    assert jobs.status(job_id)["state"] == "done"


# ---------------------------------------------------------------------------
# Failures, which have to stay visible
# ---------------------------------------------------------------------------


def test_failure_is_recorded_not_silent(scratch_vault, no_worker, monkeypatch):
    _seed_source()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        progress("correcting", 2, 9)
        raise ladder.LadderError("model unreachable")

    monkeypatch.setattr(ladder, "build", _build)

    job_id = jobs.submit("ladder", _payload())
    _drain()

    record = jobs.status(job_id)
    assert record["state"] == "failed"
    assert record["error"] == "model unreachable"
    assert record["stage"] == "correcting", "what failed, and how far it got, both survive"
    assert (record["done"], record["total"]) == (2, 9)
    assert (jobs._jobs_dir() / f"{job_id}.json").is_file(), "nothing on disk is deleted"


def test_any_exception_lands_on_the_record(scratch_vault, no_worker, monkeypatch):
    _seed_source()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(ladder, "build", _build)
    job_id = jobs.submit("ladder", _payload())
    _drain()

    record = jobs.status(job_id)
    assert record["state"] == "failed"
    assert "division by zero" in record["error"]


def test_cancel_never_erases_a_finished_failure(scratch_vault, no_worker, gate, monkeypatch):
    """A cancel deciding from a stale read must not erase a job's failure.

    This is the module's founding bug wearing a different hat. Cancel reads a
    state, the job fails, cancel writes its decision, and the error message is
    gone: a broken run that reads as a tidy cancellation.

    The interleaving is forced rather than hoped for. The canceller's read is
    held open until the failure has been written, which is the exact window the
    record lock has to close. Whichever operation wins is fine; what may never
    happen is a job that ran, raised, and then reads as merely cancelled.
    """
    _seed_source()
    ran = threading.Event()
    failure_written = threading.Event()
    canceller: dict = {}

    def _build(source, client_ctx, progress=None, between_rungs=None):
        ran.set()
        progress("correcting", 2, 9)
        raise ladder.LadderError("model unreachable")

    real_read = jobs._read
    real_write = jobs._write

    def _slow_read(job_id):
        record = real_read(job_id)
        if threading.current_thread() is canceller.get("thread"):
            # Hold the snapshot open. Without the lock, the job fails inside
            # this window and cancel goes on to write over the failure.
            failure_written.wait(1.0)
        return record

    def _watch_write(record):
        real_write(record)
        if record.get("state") == "failed":
            failure_written.set()

    monkeypatch.setattr(ladder, "build", _build)
    monkeypatch.setattr(jobs, "_read", _slow_read)
    monkeypatch.setattr(jobs, "_write", _watch_write)

    job_id = jobs.submit("ladder", _payload())
    thread = threading.Thread(target=jobs.cancel, args=(job_id,), daemon=True)
    canceller["thread"] = thread
    gate.threads.append(thread)
    thread.start()
    _wait_until(lambda: canceller.get("thread").is_alive(), what="the cancel to start")

    jobs._run_job(job_id)
    thread.join(TIMEOUT)
    assert not thread.is_alive()

    record = real_read(job_id)
    if ran.is_set():
        assert record["state"] == "failed", "a job that ran and raised says so"
        assert record["error"] == "model unreachable"
        assert (record["done"], record["total"]) == (2, 9)
    else:
        # Cancel got there first, so the job never ran. A real cancellation.
        assert record["state"] == "cancelled"
        assert record["error"] is None


def test_a_stopped_worker_is_never_resurrected(scratch_vault, gate, monkeypatch):
    """A stop belongs to the thread it stopped, even when the join times out.

    One local model means one worker. A stop flag shared between generations
    can be cleared by the next start while the thread it was meant to stop is
    still alive, and then two workers pull from one queue.
    """
    _seed_source()
    _seed_source(OTHER_SID)
    started = threading.Event()
    carry_on = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        started.set()
        assert gate.wait(carry_on)
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    jobs.submit("ladder", _payload())
    assert started.wait(TIMEOUT)
    first = jobs._WORKER
    assert first is not None
    assert jobs._stop_worker(timeout=0.2) is False, "the blocked job outlasts the join"

    jobs.submit("ladder", _payload(OTHER_SID))  # starts a second worker
    carry_on.set()

    _wait_until(lambda: not first.is_alive(), what="the stopped worker to exit")
    assert len(_live_workers()) == 1, "the stopped worker must not be brought back"


def test_worker_survives_a_failing_job(scratch_vault, monkeypatch):
    _seed_source("s-bad")
    _seed_source("s-good")

    def _build(source, client_ctx, progress=None, between_rungs=None):
        if source.id == "s-bad":
            raise ladder.LadderError("model unreachable")
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    bad = jobs.submit("ladder", _payload("s-bad"))
    good = jobs.submit("ladder", _payload("s-good"))

    assert _wait_for_state(bad, "failed")["error"] == "model unreachable"
    _wait_for_state(good, "done")
    assert jobs._WORKER is not None and jobs._WORKER.is_alive()


# ---------------------------------------------------------------------------
# The pause, which is the whole point of the queue
# ---------------------------------------------------------------------------


def test_interactive_hold_pauses_between_rungs(scratch_vault, monkeypatch):
    _seed_source()
    at_boundary = threading.Event()
    second_rung = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        progress("transcribing", 1, 2)
        at_boundary.set()
        between_rungs()
        second_rung.set()
        progress("digesting", 2, 2)
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    with jobs.interactive_hold():
        job_id = jobs.submit("ladder", _payload())
        assert at_boundary.wait(TIMEOUT), "the first rung ran"
        record = _wait_for_state(job_id, "waiting")
        assert record["stage"] == "transcribing", "the pause keeps the last stage"
        assert not second_rung.is_set(), "the held job is parked at the boundary"

    assert second_rung.wait(TIMEOUT), "releasing the hold lets the rung start"
    record = _wait_for_state(job_id, "done")
    assert record["stage"] == "digesting"


def test_a_held_job_still_lets_the_worker_be_stopped(scratch_vault, monkeypatch):
    """Teardown must never end up waiting on a hold that nobody is going to lift."""
    _seed_source()
    at_boundary = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        at_boundary.set()
        between_rungs()
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    jobs._acquire_hold()  # a hold left behind, the way a failing test leaves one
    jobs.submit("ladder", _payload())
    assert at_boundary.wait(TIMEOUT)
    worker = jobs._WORKER

    jobs._reset_state()

    assert worker is not None and not worker.is_alive(), "the parked job let go"
    assert not jobs._HOLD.is_set()


def test_nested_holds_release_only_when_all_exit(scratch_vault):
    with jobs.interactive_hold():
        assert jobs._HOLD.is_set()
        with jobs.interactive_hold():
            assert jobs._HOLD.is_set()
        assert jobs._HOLD.is_set(), "the inner exit must not release the outer hold"
    assert not jobs._HOLD.is_set()

    # An exception on the way out still releases: __exit__ runs in a finally.
    with pytest.raises(RuntimeError):
        with jobs.interactive_hold():
            with jobs.interactive_hold():
                raise RuntimeError("the debrief blew up")
    assert not jobs._HOLD.is_set()
    assert jobs._HOLD_DEPTH == 0


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------


def test_cancel_queued_job_never_runs(scratch_vault, no_worker, monkeypatch):
    _seed_source()
    calls: list[str] = []
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: calls.append(source.id) or {},
    )

    job_id = jobs.submit("ladder", _payload())
    assert jobs.cancel(job_id) is True
    assert jobs.status(job_id)["state"] == "cancelled"

    _drain()

    assert calls == [], "a cancelled job is not run, even though it was queued"
    assert jobs.status(job_id)["state"] == "cancelled"
    assert jobs.cancel(job_id) is False, "cancelling twice is not a second cancel"


def test_cancel_running_job_stops_at_rung_boundary(scratch_vault, gate, monkeypatch):
    _seed_source()
    first_rung = threading.Event()
    carry_on = threading.Event()
    second_rung = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        progress("correcting", 1, 2)
        first_rung.set()
        assert gate.wait(carry_on)
        between_rungs()
        second_rung.set()
        progress("digesting", 2, 2)
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    job_id = jobs.submit("ladder", _payload())
    assert first_rung.wait(TIMEOUT)
    assert _state(job_id) == "running"
    assert jobs.cancel(job_id) is True
    carry_on.set()

    record = _wait_for_state(job_id, "cancelled")
    assert not second_rung.is_set(), "cancellation lands at the boundary, not mid rung"
    assert record["stage"] == "correcting", "the rung that finished is still reported"
    assert record["error"] is None


# ---------------------------------------------------------------------------
# Resuming after a restart
# ---------------------------------------------------------------------------


def test_cancel_while_parked_in_a_hold(scratch_vault, monkeypatch):
    """A job waiting on a hold still hears a cancellation."""
    _seed_source()
    at_boundary = threading.Event()
    second_rung = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        progress("correcting", 1, 2)
        at_boundary.set()
        between_rungs()
        second_rung.set()
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    with jobs.interactive_hold():
        job_id = jobs.submit("ladder", _payload())
        assert at_boundary.wait(TIMEOUT)
        _wait_for_state(job_id, "waiting")
        assert jobs.cancel(job_id) is True
        record = _wait_for_state(job_id, "cancelled")

    assert not second_rung.is_set(), "a parked job that is cancelled does not carry on"
    assert record["stage"] == "correcting"
    assert record["error"] is None


def test_a_job_never_runs_twice_at_once(scratch_vault, no_worker, gate, monkeypatch):
    """A duplicate queue entry is a leftover, not a second instruction."""
    _seed_source()
    inside = threading.Event()
    carry_on = threading.Event()
    runs: list[str] = []

    def _build(source, client_ctx, progress=None, between_rungs=None):
        runs.append(source.id)
        inside.set()
        assert gate.wait(carry_on)
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    job_id = jobs.submit("ladder", _payload())
    runner = gate.spawn(jobs._run_job, job_id)
    assert inside.wait(TIMEOUT)

    jobs._run_job(job_id)  # the same job, while the first run is still inside it
    assert runs == [SID], "the second call did not start a second ladder"

    carry_on.set()
    runner.join(TIMEOUT)
    assert not runner.is_alive()
    assert jobs.status(job_id)["state"] == "done"


def test_resume_requeues_interrupted_only(scratch_vault, no_worker):
    seeded = {
        "queued": "20260725-100001-aaaaaa",
        "running": "20260725-100002-bbbbbb",
        "waiting": "20260725-100003-cccccc",
        "done": "20260725-100004-dddddd",
        "failed": "20260725-100005-eeeeee",
        "cancelled": "20260725-100006-ffffff",
    }
    for state, job_id in seeded.items():
        _seed_record(job_id, state)
    corrupt = jobs._jobs_dir() / "20260725-100099-badbad.json"
    corrupt.write_text("{ this is not json", encoding="utf-8")

    assert jobs.resume_pending() == 3

    by_state = {row["state"] for row in jobs.list_jobs()}
    assert by_state == {"queued", "done", "failed", "cancelled"}

    requeued = [row for row in jobs.list_jobs() if row["interrupted"]]
    assert len(requeued) == 3
    assert all(row["state"] == "queued" for row in requeued)

    untouched = [row for row in jobs.list_jobs() if not row["interrupted"]]
    assert {row["state"] for row in untouched} == {"done", "failed", "cancelled"}

    assert corrupt.is_file(), "an unreadable record is skipped, never deleted"
    assert jobs._QUEUE.qsize() == 3, "the three are back on the live queue"


def test_resume_with_nothing_to_resume(scratch_vault, no_worker):
    assert jobs.resume_pending() == 0
    _seed_record("20260725-100000-000abc", "done")
    assert jobs.resume_pending() == 0


def test_resume_twice_still_runs_the_job_once(scratch_vault, no_worker, monkeypatch):
    """Resuming is safe to repeat: a second queue entry does not run it again."""
    _seed_source()
    runs: list[str] = []
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: runs.append(source.id) or {},
    )
    _seed_record("20260725-100000-000abc", "running")

    assert jobs.resume_pending() == 1
    assert jobs.resume_pending() == 1, "still queued, so still resumable"

    _drain()

    assert runs == [SID], "a duplicate queue entry does not run the job twice"
    assert jobs.status("20260725-100000-000abc")["state"] == "done"


def test_resume_leaves_a_running_job_alone(scratch_vault, no_worker, gate, monkeypatch):
    """A job a thread is inside right now was not interrupted by anything."""
    _seed_source()
    inside = threading.Event()
    carry_on = threading.Event()

    def _build(source, client_ctx, progress=None, between_rungs=None):
        inside.set()
        assert gate.wait(carry_on)
        return {"source_id": source.id}

    monkeypatch.setattr(ladder, "build", _build)

    job_id = jobs.submit("ladder", _payload())
    runner = gate.spawn(jobs._run_job, job_id)
    assert inside.wait(TIMEOUT)

    assert jobs.resume_pending() == 0, "a live job is not an interrupted one"
    assert jobs.status(job_id)["interrupted"] is False

    carry_on.set()
    runner.join(TIMEOUT)
    assert jobs.status(job_id)["state"] == "done"


def test_resume_starts_the_worker_it_queues_into(scratch_vault, monkeypatch):
    """A restart resumes on its own: nothing else has to be submitted first."""
    _seed_source()
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: {"source_id": source.id},
    )
    _seed_record("20260725-100000-000abc", "running", stage="correcting", done=3, total=9)

    assert jobs.resume_pending() == 1

    record = _wait_for_state("20260725-100000-000abc", "done")
    assert record["interrupted"] is True, "the interruption is still on the record"


def test_nothing_starts_a_worker_until_there_is_work(scratch_vault, monkeypatch):
    """Importing this module, or reading from it, starts no thread."""
    assert not _live_workers()
    jobs.list_jobs()
    jobs.status("20260725-100000-000abc")
    jobs.cancel("20260725-100000-000abc")
    assert not _live_workers(), "reading from the queue starts nothing"
    assert jobs._WORKER is None

    _seed_source()
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: {"source_id": source.id},
    )
    job_id = jobs.submit("ladder", _payload())
    _wait_for_state(job_id, "done")
    assert jobs._WORKER is not None and jobs._WORKER.is_alive()


def test_resumed_job_runs_and_completes(scratch_vault, no_worker, monkeypatch):
    _seed_source()
    monkeypatch.setattr(
        ladder,
        "build",
        lambda source, ctx, progress=None, between_rungs=None: {"source_id": source.id},
    )
    _seed_record("20260725-100000-000abc", "running", stage="correcting", done=3, total=9)

    assert jobs.resume_pending() == 1
    assert _drain() == ["20260725-100000-000abc"]
    assert jobs.status("20260725-100000-000abc")["state"] == "done"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_jobs_filters_and_orders(scratch_vault, no_worker):
    _seed_record("20260725-090000-aaaaaa", "done")
    _seed_record("20260725-100000-bbbbbb", "queued", payload=_payload(OTHER_SID))
    _seed_record("20260726-080000-cccccc", "failed", payload=_payload(client_id="C-0002"))

    everything = jobs.list_jobs()
    assert [row["id"] for row in everything] == [
        "20260726-080000-cccccc",
        "20260725-100000-bbbbbb",
        "20260725-090000-aaaaaa",
    ], "newest first"

    mine = jobs.list_jobs(CLIENT)
    assert [row["id"] for row in mine] == [
        "20260725-100000-bbbbbb",
        "20260725-090000-aaaaaa",
    ]
    assert [row["id"] for row in jobs.list_jobs("C-0002")] == ["20260726-080000-cccccc"]
    assert jobs.list_jobs("C-0404") == []


def test_list_jobs_survives_a_corrupt_record(scratch_vault, no_worker):
    _seed_record("20260725-090000-aaaaaa", "done")
    (jobs._jobs_dir() / "20260725-100000-badbad.json").write_text("nope", encoding="utf-8")

    assert [row["id"] for row in jobs.list_jobs()] == ["20260725-090000-aaaaaa"]


def test_list_jobs_before_anything_was_submitted(scratch_vault):
    assert jobs.list_jobs() == []
