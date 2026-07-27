"""Background work that steps aside the moment a human is waiting.

One local model server means every call has to take its turn, and a session
recording is 10 to 17 minutes of turns: transcribe, correct, digest. That would
be intolerable if it blocked anything, so it runs back here, and the queue is
what makes the constraint behave well rather than badly:

    session ends            the ladder starts building, quietly
    the clinician debriefs  their work goes in at a higher priority and takes
                            the model at the next rung boundary
    the note is on screen   the digest finished while they were talking

Three properties are the whole module, and each one is a promise to somebody.

**It pauses at a boundary, never mid call.** interactive_hold() sets a flag the
ladder's between_rungs callback reads. A held job parks between rungs, its state
reads "waiting", and it picks up exactly where it was when the hold lifts.
Killing a call in flight would waste the minutes it had already spent.

**It resumes rather than restarts.** Every rung the ladder finishes is on disk
before the next one starts, so a job interrupted by a quit or a crash costs only
the rung it was in. resume_pending() puts those jobs back on the queue at
startup and the ladder rebuilds only what is missing.

**It fails out loud.** This codebase has already shipped the other thing once:
an error set and then cleared one line later, so a broken run looked like a
finished one. A failed job here keeps its message, keeps the stage it died in,
and keeps every file it had already written. Nothing is deleted to tidy up.

Job records are json under `<vault>/.debrief_staging/jobs/`, one file per job,
written atomically with the vault's own helper so a reader can never catch a
half written record. config.VAULT_DIR is read at call time, never bound at
import, so a test that repoints the vault repoints this module with it.

The worker thread starts on the first submit, never at import, so importing this
module (which the app does, and pytest does) starts nothing.

The test seam is deliberate and small: `_run_job(job_id)` runs one job to
completion on the calling thread, and it is the same function the worker calls.
A test that wants a fixed order sets `_worker_enabled = False`, submits, and
calls `_run_job` itself.

Nothing here reads or writes the walled-off folder the vault keeps away from
every model and every feature. The ladder does the work; this module only
decides when it is allowed to.

No em dashes anywhere.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import json
import queue
import re
import secrets
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config, ladder, recstore, sources, vault
from .vault import _atomic_write


class JobError(RuntimeError):
    """A job could not be run. The message is safe to show a clinician."""


# ---------------------------------------------------------------------------
# The shape of a job
# ---------------------------------------------------------------------------

# Where the records live, under the same staging folder the vault already uses
# for work in progress. Out of Clients/ and out of Recordings/, because a job
# record is bookkeeping about the vault rather than part of it.
_STAGING_DIRNAME = ".debrief_staging"
_JOBS_DIRNAME = "jobs"

QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

STATES = frozenset({QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED})

# Finished, one way or another. A job in one of these is never run again and
# never requeued by a resume.
_TERMINAL = frozenset({DONE, FAILED, CANCELLED})

# Not finished. After a restart these are the jobs that were interrupted, since
# nothing was running at the moment the process came back up.
_UNFINISHED = frozenset({QUEUED, RUNNING, WAITING})

# Lower number first. Background work sits at the default; a clinician waiting
# on something goes in ahead of it.
INTERACTIVE_PRIORITY = 1
DEFAULT_PRIORITY = 5

# A job id is exactly one path segment: it starts with a letter or digit and
# continues with letters, digits, dot, dash, or underscore. Ids are generated
# here, but status() and cancel() are reachable from a route, so what arrives is
# checked before it is turned into a filename.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# How long a parked job waits before looking again. Short, because the only
# thing it costs is the cancel check, and a cancelled job that keeps sleeping is
# a job the clinician has to watch not stopping.
_HOLD_POLL_SECONDS = 0.05

# How long the worker blocks on an empty queue before checking whether it has
# been asked to stop.
_QUEUE_POLL_SECONDS = 0.25


def _now() -> str:
    """The current UTC time, to the second, in ISO form."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _new_id() -> str:
    """A job id: UTC timestamp plus a short random suffix.

    The timestamp makes ids sort by creation, which is what "newest first" in
    list_jobs is built on. The suffix is what makes two jobs submitted in the
    same second two jobs rather than one overwriting the other.
    """
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# Records on disk
# ---------------------------------------------------------------------------


def _jobs_dir() -> Path:
    """The jobs folder for the currently configured vault. Creates nothing."""
    return config.VAULT_DIR / _STAGING_DIRNAME / _JOBS_DIRNAME


def _job_path(job_id) -> Path | None:
    """The record file for a job id, or None when the id is not a safe name."""
    text = "" if job_id is None else str(job_id)
    if not _ID_RE.fullmatch(text) or ".." in text:
        return None
    return _jobs_dir() / f"{text}.json"


def _read_path(path: Path) -> dict | None:
    """One record, or None when it is missing or not readable json.

    A corrupt record reads as absent rather than raising, and is never deleted:
    it is the only surviving evidence of whatever wrote it.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read(job_id) -> dict | None:
    """One job's record, by id."""
    path = _job_path(job_id)
    return None if path is None else _read_path(path)


# Held across any read of a record that is followed by a write of it. The file
# write itself is already atomic; what needs guarding is the gap between
# deciding what to write and writing it, because two threads that both read
# "queued" will both act on it. That gap is how a finished job's error message
# would get overwritten with somebody else's stale idea of its state, which is
# exactly the silent failure this module was written to end.
_RECORD_LOCK = threading.RLock()


def _write(record: dict) -> None:
    """Persist a record atomically, stamping updated_at. Raises on a bad id.

    Every state change in this module goes through here, which is what makes a
    crash mid write survivable: the vault's helper writes a temp file in the
    same folder and renames it over the target, so a reader sees the old record
    or the new one and never half of either.
    """
    path = _job_path(record.get("id"))
    if path is None:
        raise JobError(f"invalid job id: {record.get('id')!r}")
    with _RECORD_LOCK:
        record["updated_at"] = _now()
        _atomic_write(path, json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n")


def _new_record(kind: str, payload: dict, priority: int) -> dict:
    stamp = _now()
    return {
        "id": _new_id(),
        "kind": kind,
        "payload": dict(payload or {}),
        "priority": int(priority),
        "state": QUEUED,
        "stage": None,
        "done": 0,
        "total": 0,
        "error": None,
        "created_at": stamp,
        "updated_at": stamp,
        "interrupted": False,
    }


# ---------------------------------------------------------------------------
# The queue and its one worker
# ---------------------------------------------------------------------------

# (priority, sequence, job_id). The sequence is what turns a priority queue into
# a fair one: two jobs at the same priority come out in the order they went in,
# rather than in whatever order their ids happen to sort.
_QUEUE: queue.PriorityQueue = queue.PriorityQueue()
_SEQUENCE = itertools.count()

_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.RLock()

# The stop flag belongs to one worker, not to the module. A shared flag can be
# cleared by the next start while the thread it was meant to stop is still
# alive, which resurrects it: two workers, one model, and the serialisation this
# whole module exists to provide is gone. Each thread gets its own event, and an
# event that has been set is never handed to anybody again.
_STOP: threading.Event = threading.Event()

# What the worker caught that could not be written to a record. A disk that is
# full or a vault that has gone away can defeat the record itself, and a silent
# swallow there is the same failure this module was written to end.
_LAST_WORKER_ERROR: str | None = None


def _lost(job_id, detail) -> None:
    """Report a failure that could not be written to its own record.

    The record is where a failure is supposed to live, so when the record is the
    thing that failed there is nowhere left inside the app to put it. It goes to
    stderr, which is where the console the app runs in will show it, and it is
    kept here for last_worker_error() so a route or a test can read it back. A
    job left saying "running" forever with no visible cause is the exact failure
    this module exists to end, and this is the last line of that defence.
    """
    global _LAST_WORKER_ERROR
    message = f"debrief jobs: could not record job failure for {job_id}: {detail}"
    _LAST_WORKER_ERROR = message
    print(message, file=sys.stderr)


def last_worker_error() -> str | None:
    """The last failure that could not be written to a job record, or None.

    Public so the jobs route can surface it: a failure the records cannot hold
    still has to reach the clinician somehow.
    """
    return _LAST_WORKER_ERROR

# The test seam. False means submit() still queues and still persists, but no
# thread is started, so nothing runs until a test calls _run_job itself.
_worker_enabled = True


def _worker_loop(stop: threading.Event) -> None:
    """Pull jobs and run them, one at a time, until this worker's flag is set.

    The loop is deliberately unkillable by its own work: _run_job already turns
    any exception into a failed record, and the guard here catches whatever a
    record write itself could raise. One broken job must never take the queue
    down with it, because the next job in line belongs to somebody else.
    """
    while not stop.is_set():
        try:
            _, _, job_id = _QUEUE.get(timeout=_QUEUE_POLL_SECONDS)
        except queue.Empty:
            continue
        try:
            _run_job(job_id)
        except Exception as exc:  # noqa: BLE001 - the worker outlives every job
            # Reaching here means the record itself could not be written, so
            # the job is stuck saying whatever it last managed to say. There is
            # nowhere inside the app left to put this, so it goes to the console.
            _lost(job_id, exc)
        finally:
            _QUEUE.task_done()


def _start_worker() -> None:
    """Start the worker if it is not already running. Called on every enqueue."""
    global _WORKER, _STOP
    if not _worker_enabled:
        return
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        stop = threading.Event()
        _STOP = stop
        _WORKER = threading.Thread(
            target=_worker_loop, args=(stop,), name="debrief-jobs", daemon=True
        )
        _WORKER.start()


def _stop_worker(timeout: float = 5.0) -> bool:
    """Stop the worker and wait for it. True when it actually stopped.

    Releases every hold first. A job parked at a rung boundary is waiting on the
    hold rather than on this flag, so a stop with a hold still up would wait out
    the whole timeout and leave the thread exactly where it was.

    The flag belongs to the thread being stopped and is never reused, so a
    worker that outlives its timeout still exits after its current job instead
    of being brought back to life by the next start.
    """
    global _WORKER, _HOLD_DEPTH
    with _HOLD_LOCK:
        _HOLD_DEPTH = 0
        _HOLD.clear()
        _RESUMED.set()
    with _WORKER_LOCK:
        worker, _WORKER = _WORKER, None
        _STOP.set()
    if worker is None:
        return True
    worker.join(timeout)
    return not worker.is_alive()


def _enqueue(job_id: str, priority) -> None:
    """Put a job on the queue and make sure something is there to take it off."""
    try:
        rank = int(priority)
    except (TypeError, ValueError):
        rank = DEFAULT_PRIORITY
    _QUEUE.put((rank, next(_SEQUENCE), str(job_id)))
    _start_worker()


# ---------------------------------------------------------------------------
# The hold, which is how a human takes the model back
# ---------------------------------------------------------------------------

# _HOLD is set while any hold is active. _RESUMED is its inverse, and exists
# because an Event can only be waited on for being set: a parked job waits on
# _RESUMED so it wakes the instant the last hold exits, rather than spinning.
_HOLD = threading.Event()
_RESUMED = threading.Event()
_RESUMED.set()

# Overlapping debriefs are two separate holds, so a count decides when the model
# goes back to the background rather than whichever one happens to exit first.
_HOLD_DEPTH = 0
_HOLD_LOCK = threading.Lock()


def _acquire_hold() -> None:
    global _HOLD_DEPTH
    with _HOLD_LOCK:
        _HOLD_DEPTH += 1
        _HOLD.set()
        _RESUMED.clear()


def _release_hold() -> None:
    global _HOLD_DEPTH
    with _HOLD_LOCK:
        _HOLD_DEPTH = max(_HOLD_DEPTH - 1, 0)
        if _HOLD_DEPTH == 0:
            _HOLD.clear()
            _RESUMED.set()


@contextmanager
def interactive_hold() -> Iterator[None]:
    """Hold background work at the next rung boundary for as long as this block.

    Wrap the interactive path a clinician is waiting on:

        with jobs.interactive_hold():
            note = pipeline.run(...)

    Background jobs finish the call they are in, park at the next boundary, and
    report "waiting" until the block exits. Holds nest, and the release runs in
    a finally, so an exception inside the block gives the model back rather than
    stranding the queue.
    """
    _acquire_hold()
    try:
        yield
    finally:
        _release_hold()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

# Job ids the clinician has cancelled. In memory on purpose: it only has to
# outlive the run it interrupts, and a cancelled job's record on disk already
# says so.
_CANCELLED: set[str] = set()
_CANCEL_LOCK = threading.Lock()


class _Cancelled(Exception):
    """Raised at a rung boundary when this job has been cancelled."""


def _mark_cancelled(job_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCELLED.add(str(job_id))


def _is_cancelled(job_id: str) -> bool:
    with _CANCEL_LOCK:
        return str(job_id) in _CANCELLED


def _forget_cancel(job_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCELLED.discard(str(job_id))


# ---------------------------------------------------------------------------
# One run per job
# ---------------------------------------------------------------------------

# The jobs a thread is inside right now. A duplicate queue entry (a resume that
# ran twice, a resume racing a submit) must not become a second run of the same
# job: two ladders on one source would transcribe it twice and race each other
# on the rung files.
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


def _claim(job_id: str) -> bool:
    """Take this job, unless another thread is already inside it."""
    with _INFLIGHT_LOCK:
        if job_id in _INFLIGHT:
            return False
        _INFLIGHT.add(job_id)
        return True


def _release(job_id: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(job_id)


def _reset_state(timeout: float = 5.0) -> None:
    """Stop the worker, empty the queue, and clear the holds and cancellations.

    For tests, between one scratch vault and the next, so a thread can never
    outlive the vault it was pointed at. Nothing on disk is touched: this
    forgets what is in memory, which is the part that would otherwise leak from
    one test into the next.
    """
    global _LAST_WORKER_ERROR
    _stop_worker(timeout)  # releases the holds first, so a parked job lets go
    while True:
        try:
            _QUEUE.get_nowait()
        except queue.Empty:
            break
        _QUEUE.task_done()
    with _CANCEL_LOCK:
        _CANCELLED.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()
    _LAST_WORKER_ERROR = None


# ---------------------------------------------------------------------------
# What a runner is handed
# ---------------------------------------------------------------------------


class _Controls:
    """The two callbacks a long job hands to the ladder, bound to one record.

    progress is how far along it is. between_rungs is where the queue is allowed
    to interrupt: it is the only place a hold parks a job and the only place a
    cancellation takes effect, so nothing is ever abandoned mid call.
    """

    def __init__(self, record: dict) -> None:
        self.record = record

    @property
    def job_id(self) -> str:
        return str(self.record.get("id") or "")

    def progress(self, stage: str, done: int, total: int) -> None:
        """Record which stage is running and how far through it is."""
        self.record["stage"] = str(stage or "").strip() or None
        self.record["done"] = int(done)
        self.record["total"] = int(total)
        _write(self.record)

    def between_rungs(self) -> None:
        """A rung boundary: stop if cancelled, park while a human is waiting.

        Called by the ladder immediately before it starts a rung. The wait loop
        is bounded rather than a single blocking wait so a cancellation arriving
        during a long hold is seen at the next tick instead of at the end of it.
        """
        self._stop_if_cancelled()
        if not _HOLD.is_set():
            return
        self._set_state(WAITING)
        try:
            while _HOLD.is_set():
                self._stop_if_cancelled()
                _RESUMED.wait(_HOLD_POLL_SECONDS)
        finally:
            # Even on the way out through a cancellation: the record must not be
            # left claiming to be waiting for a hold that no longer holds it.
            self._set_state(RUNNING)

    def _set_state(self, state: str) -> None:
        if self.record.get("state") == state:
            return
        self.record["state"] = state
        _write(self.record)

    def _stop_if_cancelled(self) -> None:
        if _is_cancelled(self.job_id):
            raise _Cancelled()


# ---------------------------------------------------------------------------
# The runners
# ---------------------------------------------------------------------------


def _run_ladder(record: dict, controls: _Controls) -> dict:
    """Build every rung a source is missing. The one long job this app has.

    Reads the source's provenance from disk rather than from the payload, so a
    reclassification between submitting and running is honoured. The client
    context is best effort: a profile that cannot be read makes for a thinner
    digest, not a failed job, and losing 15 minutes of transcription over a
    missing summary paragraph would be the wrong trade every time.
    """
    payload = record.get("payload") or {}
    client_id = str(payload.get("client_id") or "").strip()
    source_id = str(payload.get("source_id") or "").strip()
    if not client_id or not source_id:
        raise JobError("this job names no client and no source, so there is nothing to build.")

    try:
        meta = recstore.read_meta(client_id, source_id)
    except ValueError as exc:
        raise JobError(f"this job names a source that cannot exist: {exc}") from exc
    if not meta:
        raise JobError(
            f"there is no stored source {source_id} for client {client_id}, so there "
            "is nothing to build."
        )

    # The folder is what identity means here: the rungs are written by client id
    # and source id, and a meta that disagreed with where it was found would
    # send them somewhere else.
    meta = dict(meta)
    meta["id"] = source_id
    meta["client_id"] = client_id
    try:
        source = sources.Source.from_meta(meta)
    except (ValueError, TypeError) as exc:
        raise JobError(f"the provenance for this source could not be read: {exc}") from exc

    try:
        client_ctx = vault.client_context(client_id)
    except Exception:  # noqa: BLE001 - context is an input, never a reason to fail
        client_ctx = {}
    if not isinstance(client_ctx, dict):
        client_ctx = {}

    return ladder.build(
        source,
        client_ctx,
        progress=controls.progress,
        between_rungs=controls.between_rungs,
    )


# Every kind of work this queue knows how to do. An unknown kind is a failed job
# with a message that names it, never a silent no-op.
_RUNNERS: dict[str, Callable[[dict, _Controls], object]] = {"ladder": _run_ladder}


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------


def _run_job(job_id: str) -> dict | None:
    """Run one job to completion on the calling thread. Returns its record.

    The worker calls this, and so does a test that wants a fixed order: same
    function, same path, no separate code that only tests exercise.

    Every ending lands on the record before this returns. A cancellation is not
    a failure and carries no error text; everything else, LadderError or
    otherwise, is a failure carrying the exception's own message. The stage,
    done, and total from the last progress report are left exactly as they were,
    so a partial run says how far it got, and nothing on disk is removed.

    Do not call this from a thread that is inside its own interactive_hold():
    the job would park at the first rung boundary waiting for a hold that only
    the calling thread can release.
    """
    job_id = str(job_id)
    if not _claim(job_id):
        # Somebody is already inside this job. A duplicate queue entry is a
        # leftover, not a second instruction.
        return _read(job_id)
    try:
        record = _read(job_id)
        if record is None:
            # A queued job whose record has gone or gone bad. There is nothing
            # left to write the failure onto, so the console is told instead:
            # the evidence should not be only a file nobody thought to look at.
            _lost(job_id, "its record is missing or could not be read")
            return None
        # The id this was found under is the id it is written back under,
        # whatever the record says about itself. Anything else updates a
        # different file.
        record["id"] = job_id
        if record.get("state") in _TERMINAL:
            # Already finished, or cancelled while it sat on the queue. The
            # queue entry is a leftover, not an instruction.
            _forget_cancel(job_id)
            return record

        controls = _Controls(record)
        try:
            record["state"] = RUNNING
            record["error"] = None
            _write(record)
            runner = _RUNNERS.get(str(record.get("kind") or ""))
            if runner is None:
                raise JobError(
                    f"unknown job kind: {record.get('kind')!r}. "
                    f"Known kinds are {', '.join(sorted(_RUNNERS))}."
                )
            controls._stop_if_cancelled()
            runner(record, controls)
        except _Cancelled:
            record["state"] = CANCELLED
            _write(record)
        except Exception as exc:  # noqa: BLE001 - every failure lands on the record
            record["state"] = FAILED
            record["error"] = str(exc).strip() or exc.__class__.__name__
            _write(record)
        else:
            record["state"] = DONE
            record["error"] = None
            _write(record)
        finally:
            _forget_cancel(job_id)
        return record
    finally:
        _release(job_id)


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def submit(kind: str, payload: dict, priority: int = DEFAULT_PRIORITY) -> str:
    """Queue a job and return its id. Starts the worker if it is not running.

    The record is on disk before the job is on the queue, so a crash in between
    leaves a job that a resume picks up rather than a job nobody can see.
    """
    kind = str(kind or "").strip()
    if not kind:
        raise JobError("a job needs a kind.")
    if payload is not None and not isinstance(payload, dict):
        raise JobError("a job payload is a dict.")
    try:
        priority = int(priority)
    except (TypeError, ValueError) as exc:
        raise JobError(f"a job priority is a number: {priority!r}") from exc

    record = _new_record(kind, payload or {}, priority)
    _write(record)
    _enqueue(record["id"], priority)
    return record["id"]


def status(job_id: str) -> dict | None:
    """One job's whole record, or None when there is no such job.

    The id comes back as the one it was looked up by, so what a caller reads is
    always what it can cancel.
    """
    record = _read(job_id)
    if record is not None:
        record["id"] = str(job_id)
    return record


def list_jobs(client_id: str | None = None) -> list[dict]:
    """Every job, newest first, optionally narrowed to one client.

    Newest first is by created_at, with the id breaking a tie, because an id
    carries the time only to the second and two jobs submitted in the same
    second would otherwise be ordered by their random suffix. An unreadable
    record is skipped: a listing must never be taken down by one bad file.
    """
    directory = _jobs_dir()
    if not directory.is_dir():
        return []

    wanted = None if client_id is None else str(client_id)
    rows: list[dict] = []
    for path in directory.glob("*.json"):
        record = _read_path(path)
        if record is None:
            continue
        record["id"] = path.stem
        if wanted is not None:
            payload = record.get("payload") or {}
            if str(payload.get("client_id") or "") != wanted:
                continue
        rows.append(record)

    rows.sort(
        key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")),
        reverse=True,
    )
    return rows


def cancel(job_id: str) -> bool:
    """Stop a job. False when there is no such job, or it already finished.

    A job still on the queue is marked cancelled and never runs. A job already
    running is flagged, and stops at its next rung boundary: the call in flight
    is allowed to finish, and every rung it has already written stays on disk,
    so restarting it later costs only what it had not yet done.

    The read and the write happen under one lock, and the state is read again
    inside it. Deciding from a snapshot taken outside would let this write
    "cancelled" over a job that failed a moment ago, taking its error message
    and its progress with it.
    """
    with _RECORD_LOCK:
        record = _read(job_id)
        if record is None:
            return False
        state = record.get("state")
        if state in _TERMINAL:
            return False

        # Flagged whatever the state, because a job that reads as queued here
        # may be starting on the worker thread, and the flag is what that run
        # checks. Only a job that is still queued is written to: a running one
        # owns its own record until it stops at a boundary.
        _mark_cancelled(job_id)
        if state == QUEUED:
            record["id"] = str(job_id)
            record["state"] = CANCELLED
            _write(record)
        return True


def resume_pending() -> int:
    """Requeue every job a restart interrupted. Returns how many.

    Anything that was queued, running, or waiting when the process stopped goes
    back on the queue. Finished, failed, and cancelled jobs are left alone.

    Only a job that was actually mid flight, running or waiting, is marked
    interrupted. A job that was still queued had not started, so nothing was cut
    off, and saying otherwise would have the UI report a stall to the clinician
    that never happened.

    Resuming is cheap: the ladder asks the disk which rungs exist and builds
    only the ones that do not, so a job that died in the digest does not
    transcribe anything again.

    A job a thread is inside right now is not interrupted, so it is left alone.
    Calling this while the worker is live is therefore safe, which matters
    because the app calls it at startup and nothing stops it being called again.
    """
    directory = _jobs_dir()
    if not directory.is_dir():
        return 0

    count = 0
    for path in sorted(directory.glob("*.json")):
        with _RECORD_LOCK:
            record = _read_path(path)
            if record is None:
                continue  # Corrupt, and left exactly where it is.
            if record.get("state") not in _UNFINISHED:
                continue
            with _INFLIGHT_LOCK:
                if path.stem in _INFLIGHT:
                    continue  # Running now, so not something a restart cut off.
            # The filename is the id: it is what a reader will look this record
            # up by, whatever the record says about itself.
            record["id"] = path.stem
            if record.get("state") in (RUNNING, WAITING):
                record["interrupted"] = True
            record["state"] = QUEUED
            try:
                _write(record)
            except JobError:
                continue
        _enqueue(record["id"], record.get("priority", DEFAULT_PRIORITY))
        count += 1
    return count
