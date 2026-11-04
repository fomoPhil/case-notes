"""Debrief web server (local only).

FastAPI wrapper around debrief.pipeline. Three JSON endpoints plus the static
single-page UI. Everything runs on 127.0.0.1; audio, model, vault, and screen
captures never leave the Mac.

Flow the UI drives:
  GET  /api/clients   -> the mock clients from the vault
  POST /api/debrief   -> transcribe + extract, returns the plan WITHOUT executing
  POST /api/execute   -> runs the approved plan (actions + note + verify)

Run:  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8377
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from debrief import (
    actions,
    agent,
    audit,
    classify,
    compiler,
    config,
    doctor,
    formats,
    jobs,
    ladder,
    llm,
    models,
    pipeline,
    records,
    recstore,
    render,
    settings_store,
    sources,
    stt,
    vault,
    verify,
    vocab,
)
from debrief.vault import VaultPathError

APP_HOST = "127.0.0.1"
APP_PORT = 8377

# Marker file (in the vault) recording that the first-run wizard has completed.
# Phase 4 owns creating it; Phase 1 only reports its absence as first_run.
_SETUP_MARKER = ".debrief_setup_done"

# Cheap cache so /api/debrief and /api/execute can gate on readiness without
# probing the model server (~1.5s) on every request.
_DOCTOR_CACHE_TTL = 10.0
_doctor_cache: dict = {"at": 0.0, "checks": None}

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Server-side store for original uploads, keyed by an opaque token that rides
# the plan through the approve round-trip. The archived vault m4a is converted
# from this original (best available source), not the 16k STT wav.
_AUDIO_STORE = Path(tempfile.gettempdir()) / "debrief_audio_store"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _store_original_audio(data: bytes) -> str:
    _AUDIO_STORE.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    (_AUDIO_STORE / f"{token}.webm").write_bytes(data)
    return token


def _pop_original_audio(token: str | None) -> Path | None:
    """Resolve a stored upload for a token. Returns the path or None."""
    if not token or not _TOKEN_RE.match(str(token)):
        return None
    path = _AUDIO_STORE / f"{token}.webm"
    return path if path.exists() else None


def _startup_warning(what: str, exc: Exception) -> None:
    """Say out loud that a startup chore did not run. Never raises.

    A chore that fails at boot must not stop the app, but it must not vanish
    either: a retention sweep that silently did not run means audio the
    clinician asked to be deleted is still there, and a resume that silently did
    not run means interrupted jobs never come back. Both go to stderr, which is
    the console the app is started from, the same place the job queue reports a
    failure it could not write to a record.
    """
    print(f"debrief startup: {what} did not run: {exc}", file=sys.stderr)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Make sure the vault + mock clients exist before the first request.
    try:
        vault.ensure_vault()
    except Exception:
        pass
    # Sweep expired trash (older than 30 days) on startup. Best effort.
    try:
        records.sweep_trash(_dt.datetime.now())
    except Exception:
        pass
    # Delete session audio past the retention window the clinician chose. Rungs
    # 1 and 2 are never touched, so losing the audio never loses the note's
    # evidence trail. Best effort, in its own guard: a sweep that cannot run is
    # never a reason the app will not start.
    try:
        recstore.sweep_retention(_dt.datetime.now())
    except Exception as exc:  # noqa: BLE001
        _startup_warning("the recording retention sweep", exc)
    # Put back on the queue whatever a quit or a crash interrupted. The ladder
    # rebuilds from the first missing rung, so a resume costs only the rung the
    # job was inside. Also best effort, and also on its own.
    try:
        jobs.resume_pending()
    except Exception as exc:  # noqa: BLE001
        _startup_warning("the interrupted job resume", exc)
    yield


app = FastAPI(title="Debrief", docs_url=None, redoc_url=None, lifespan=_lifespan)

# The only names this server answers to. A web page can point its own domain at
# 127.0.0.1 (DNS rebinding) and read the API as if it were same-origin, but its
# Host header still carries its own name, so it is refused here.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_name(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0]


@app.middleware("http")
async def _local_only(request: Request, call_next):
    """Refuse requests that did not come from Debrief's own page.

    Any web page can POST a text/plain body to 127.0.0.1 without a preflight,
    which would let it trash notes, open Mail drafts, or book Calendar events.
    Browsers mark those requests with Origin and Sec-Fetch-Site, so a state
    changing request from another site is refused.
    """
    host = request.headers.get("host", "")
    if _host_name(host) not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Debrief only answers on this Mac."}, status_code=403)
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc.lower() != host.strip().lower():
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _cached_checks(force: bool = False) -> list[dict]:
    """Return doctor.run_checks(), cached for ~10s to keep hot paths fast."""
    now = time.monotonic()
    if (
        not force
        and _doctor_cache["checks"] is not None
        and (now - _doctor_cache["at"]) < _DOCTOR_CACHE_TTL
    ):
        return _doctor_cache["checks"]
    checks = doctor.run_checks()
    _doctor_cache["checks"] = checks
    _doctor_cache["at"] = now
    return checks


def _first_hard_failure(checks: list[dict]) -> dict | None:
    """First failing hard check, or None if all hard checks pass."""
    for c in checks:
        if c.get("hard") and not c["ok"]:
            return c
    return None


def _require_model_ready() -> None:
    """Guard for model-dependent endpoints.

    Raises 503 with the failed hard check's fix text when the model server or
    model is unavailable, so callers get actionable JSON instead of a raw 500.
    """
    checks = _cached_checks()
    for c in checks:
        # Match on the stable key, never the display name: check names are
        # clinician-facing copy and are expected to change.
        model_check = c.get("key") in ("model_server", "model_loaded") or c.get(
            "name"
        ) in ("Model server reachable", "Gemma model loaded")
        if model_check and not c["ok"]:
            raise HTTPException(
                status_code=503,
                detail={"error": c["detail"], "fix": c["fix"]},
            )


# ---------------------------------------------------------------------------
# Clinician-readable fix copy
# ---------------------------------------------------------------------------
#
# Every failure a clinician can actually hit returns {"error", "fix"}: `error` is
# the short technical line (useful in a bug report), `fix` is what to do about it.
# The UI already understands that shape, so a raw Python exception string never
# renders in a modal. Validation problems stay plain 400 strings: they are already
# in plain language and the message IS the fix.

_MODEL_FIX_GENERIC = (
    "Debrief could not reach the local model. Open LM Studio, load the model, "
    "then try again."
)

_MODEL_FIX_COMPILE = (
    "Debrief could not reach the local model, so it could not read your template. "
    "Open LM Studio, load the model, then press Compile again."
)

_MODEL_FIX_PREVIEW = (
    "Debrief could not reach the local model, so it could not build a preview. "
    "Open LM Studio, load the model, then try the preview again."
)

_MODEL_FIX_DEBRIEF = (
    "Debrief could not finish reading that session. Check that LM Studio is "
    "running with the model loaded, then record or upload the debrief again."
)

_MODEL_FIX_TRANSCRIBE = (
    "Debrief could not transcribe that audio. Check that the speech model "
    "finished downloading in Settings, then try again."
)

_VAULT_UNREADABLE_FIX = (
    "Debrief could not read your vault folder. Check that it still exists and "
    "that you have permission to open it, then reload."
)

_VAULT_UNWRITABLE_FIX = (
    "Debrief could not write to your vault folder. Check that the disk is not "
    "full and that you have permission to write there, then try again."
)


def _model_unavailable(exc: BaseException) -> bool:
    """True when a failure is the local model being unreachable, not bad output.

    llm.ModelUnavailable is raised on any transport failure talking to LM Studio
    (refused connection, DNS, timeout). Wrapped errors carry it as __cause__, so
    the whole chain is walked.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 10:
        if isinstance(current, llm.ModelUnavailable):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def _require_feature(name: str) -> None:
    """Guard for feature-gated endpoints. Raises 403 when the named feature is
    turned off in settings, so a disabled assistant returns actionable JSON."""
    if not settings_store.load().get("features", {}).get(name, True):
        raise HTTPException(
            status_code=403,
            detail={"error": "The assistant is turned off in Settings."},
        )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/status")
def api_status() -> JSONResponse:
    """Report detected servers, active model, vault, checks, and readiness."""
    checks = _cached_checks(force=True)
    servers = models.detect_servers()
    active = models.pick_gemma()

    vault_dir = config.VAULT_DIR
    vault_info = {
        "path": str(vault_dir),
        "exists": vault_dir.exists(),
        "writable": vault_dir.exists() and os.access(vault_dir, os.W_OK),
    }

    ready = _first_hard_failure(checks) is None
    first_run = not (vault_dir / _SETUP_MARKER).exists()

    return JSONResponse({
        "servers": servers,
        "active_model": active,
        "vault": vault_info,
        "checks": checks,
        "ready": ready,
        "first_run": first_run,
    })


def _iso_or_none(value) -> str | None:
    """Coerce a frontmatter date/datetime/string to an ISO string, or None.

    YAML parses unquoted dates into date/datetime objects, so the home screen
    would otherwise receive types it cannot compare against today.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _client_slim(c: dict) -> dict:
    """The slim client shape the UI lists, from profile frontmatter."""
    cid = c.get("client_id")
    return {
        "client_id": cid,
        "name": c.get("name"),
        "framework": c.get("framework"),
        "presenting_concerns": c.get("presenting_concerns", []),
        "risk_flags": c.get("risk_flags", []),
        # Scheduling fields drive the home screen's "today" block.
        "next_session": _iso_or_none(c.get("next_session")),
        "last_session": _iso_or_none(c.get("last_session")),
        "session_count": records.count_sessions(cid) if cid else 0,
        # True only for the three demo clients the scaffold seeds, so the UI can
        # label them and nobody mistakes a sample record for a real one.
        "sample": bool(c.get("sample", False)),
    }


def _clients_slim_sync() -> list[dict]:
    """Blocking half of /api/clients: profile frontmatter plus session counts."""
    return [_client_slim(c) for c in vault.list_clients()]


@app.get("/api/clients")
async def api_clients() -> JSONResponse:
    """Return the list of clients from the vault (profile frontmatter)."""
    try:
        slim = await run_in_threadpool(_clients_slim_sync)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not read clients: {exc}",
                "fix": _VAULT_UNREADABLE_FIX,
            },
        )
    return JSONResponse(jsonable_encoder(slim))


@app.post("/api/clients")
async def api_client_create(request: Request) -> JSONResponse:
    """Create a client record from hand-entered details.

    Body: {name, email?, framework?, presenting_concerns?}. name is required;
    presenting_concerns accepts a list of short phrases or a comma-separated
    string. Returns the created client in the same slim shape /api/clients uses.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object.")

    def _create() -> dict:
        profile = vault.create_client(
            name=body.get("name"),
            email=body.get("email"),
            framework=body.get("framework") or "",
            presenting_concerns=body.get("presenting_concerns"),
        )
        return _client_slim(profile)

    try:
        created = await run_in_threadpool(_create)
    except vault.ClientInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not create the client folder: {exc}",
                "fix": _VAULT_UNWRITABLE_FIX,
            },
        )
    audit.log_records_change(
        "Added client", f"Clients/{created['client_id']}/_Profile.md", created["name"] or ""
    )
    return JSONResponse(jsonable_encoder(created), status_code=201)


@app.get("/api/activity/recent")
async def api_activity_recent(limit: int = 6) -> JSONResponse:
    """Recently filed session notes across the caseload, plus today's count."""
    try:
        payload = await run_in_threadpool(records.recent_activity, limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not read recent activity: {exc}",
                "fix": _VAULT_UNREADABLE_FIX,
            },
        )
    return JSONResponse(jsonable_encoder(payload))


def _convert_to_wav(src_bytes: bytes, workdir: Path) -> str:
    """Write the uploaded audio and transcode it to 16k mono wav via ffmpeg.

    MediaRecorder hands us webm/opus; parakeet wants a 16 kHz mono wav.
    """
    src = workdir / "input.webm"
    src.write_bytes(src_bytes)
    wav = workdir / "audio.wav"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not wav.exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-500:]}")
    return str(wav)


def _debrief_sync(data: bytes, client_id: str, source_id: str | None = None) -> dict:
    """Blocking half of /api/debrief; runs in a worker thread."""
    with tempfile.TemporaryDirectory(prefix="debrief_") as tmp:
        try:
            wav_path = _convert_to_wav(data, Path(tmp))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            return pipeline.transcribe_and_extract(wav_path, client_id, source_id)
        except HTTPException:
            raise
        except Exception as exc:
            if _model_unavailable(exc):
                raise HTTPException(
                    status_code=503,
                    detail={"error": str(exc), "fix": _MODEL_FIX_GENERIC},
                )
            raise HTTPException(
                status_code=500,
                detail={"error": f"Debrief failed: {exc}", "fix": _MODEL_FIX_DEBRIEF},
            )


@app.post("/api/debrief")
async def api_debrief(
    audio: UploadFile,
    client_id: str = Form(...),
    source_id: str = Form(None),
) -> JSONResponse:
    """Transcribe + extract the uploaded debrief. Returns the plan, no execution.

    source_id, when given, names session material already brought in for this
    client. Its digest is handed to the extractor as labelled evidence. A source
    that is still processing never blocks the debrief: the pipeline says so in
    the plan and writes the note without it.
    """
    _require_model_ready()
    client_id = _existing_client_id(client_id)
    sid = (source_id or "").strip() or None
    if sid is not None:
        cid = client_id
        # Validates both ids against the storage rules and creates nothing, so a
        # crafted id is refused here rather than turned into a path downstream.
        try:
            recstore.source_dir(cid, sid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    plan = await run_in_threadpool(_debrief_sync, data, client_id, sid)
    # Keep the original upload so execute can archive it into the vault.
    try:
        plan["audio_token"] = _store_original_audio(data)
    except Exception:
        plan["audio_token"] = None
    return JSONResponse(plan)


@app.post("/api/execute")
async def api_execute(request: Request) -> JSONResponse:
    """Execute an approved plan: actions + note write + verification."""
    _require_model_ready()
    try:
        plan = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid plan JSON: {exc}")
    if not isinstance(plan, dict) or not plan.get("client_id"):
        raise HTTPException(status_code=400, detail="Plan is missing client_id.")
    # The plan round-trips through the browser, so its client id is checked
    # again here: it becomes a folder path when the note is written.
    plan["client_id"] = _existing_client_id(plan["client_id"])
    # The screen check is a screen capture, so the Settings toggle decides. A
    # plan can opt out of it, never back in.
    verify = bool(settings_store.load().get("features", {}).get("verify", True))
    if plan.get("verify") is False:
        verify = False
    # Resolve the stored original upload (never trust a client-sent path).
    plan.pop("audio_path", None)
    stored = _pop_original_audio(plan.get("audio_token"))
    if stored is not None:
        plan["audio_path"] = str(stored)
    try:
        result = await run_in_threadpool(pipeline.execute_plan, plan, verify=verify)
    except Exception as exc:
        if _model_unavailable(exc):
            raise HTTPException(
                status_code=503,
                detail={"error": str(exc), "fix": _MODEL_FIX_GENERIC},
            )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Execution failed: {exc}",
                "fix": (
                    "Debrief could not finish filing that note. Check that LM "
                    "Studio is running and your vault folder is writable, then "
                    "approve the plan again. Check the client's Sessions folder "
                    "first in case a partial note was already filed."
                ),
            },
        )
    finally:
        if stored is not None:
            try:
                stored.unlink(missing_ok=True)
            except OSError:
                pass
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Assistant (in-app agent): plan (propose) + execute (approve)
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "worksheet"


def _valid_client_id(client_id) -> str | None:
    """Return a safe client id (no path parts) or None."""
    if not client_id:
        return None
    cid = str(client_id).strip()
    if not cid or "/" in cid or "\\" in cid or cid.startswith("."):
        return None
    return cid


def _existing_client_id(client_id) -> str:
    """A client id that is safe as a path and has a profile on disk, or 4xx."""
    cid = _valid_client_id(client_id)
    if not cid:
        raise HTTPException(status_code=400, detail="Invalid client id.")
    if not (config.VAULT_DIR / "Clients" / cid / "_Profile.md").is_file():
        raise HTTPException(status_code=404, detail=f"No such client: {cid}")
    return cid


def _unique_path(base_dir: Path, slug: str, suffix: str) -> Path:
    """A non-colliding path base_dir/<slug><suffix>, adding -2, -3 as needed."""
    candidate = base_dir / f"{slug}{suffix}"
    n = 2
    while candidate.exists():
        candidate = base_dir / f"{slug}-{n}{suffix}"
        n += 1
    return candidate


def _worksheet_dir(client_id: str | None) -> Path:
    """Target directory for a filed worksheet, always inside the vault."""
    if client_id:
        return config.VAULT_DIR / "Clients" / client_id / "Documents"
    return config.VAULT_DIR / "Templates" / "Worksheets"


def _file_worksheet(proposal: dict, fallback_client: str | None) -> dict:
    """File a worksheet proposal into the vault. Returns a result dict.

    Writes the Markdown source and, when the PDF renderer is available, a PDF
    beside it. Falls back to Markdown only when the renderer is unavailable.
    """
    title = (proposal.get("title") or "Worksheet").strip()
    body = (proposal.get("markdown_body") or "").strip()
    client_id = _valid_client_id(proposal.get("client_id")) or fallback_client
    target_dir = _worksheet_dir(client_id).resolve()
    vault_root = config.VAULT_DIR.resolve()
    if vault_root not in target_dir.parents and target_dir != vault_root:
        return {"type": "worksheet", "status": "failed", "error": "target escaped the vault"}
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = _slug(title)
    md_path = _unique_path(target_dir, slug, ".md")
    full_md = f"# {title}\n\n{body}\n"
    md_path.write_text(full_md, encoding="utf-8")

    pdf_path = md_path.with_suffix(".pdf")
    try:
        render.render_pdf(full_md, title, pdf_path)
        return {"type": "worksheet", "status": "ok", "path": str(pdf_path)}
    except render.PdfUnavailable:
        return {
            "type": "worksheet",
            "status": "ok",
            "path": str(md_path),
            "detail": "Filed as Markdown (PDF renderer unavailable).",
        }
    except Exception as exc:  # noqa: BLE001
        return {"type": "worksheet", "status": "ok", "path": str(md_path), "detail": f"Markdown only: {exc}"}


def _client_email(client_id: str | None) -> str | None:
    if not client_id:
        return None
    try:
        ctx = vault.client_context(client_id)
    except Exception:  # noqa: BLE001
        return None
    return ctx.get("email")


def _transcribe_upload_sync(data: bytes) -> str:
    """Blocking convert + transcribe for assistant audio; runs in a worker thread."""
    with tempfile.TemporaryDirectory(prefix="debrief_asst_") as tmp:
        try:
            wav_path = _convert_to_wav(data, Path(tmp))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            return stt.transcribe(wav_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail={
                    "error": f"Transcription failed: {exc}",
                    "fix": _MODEL_FIX_TRANSCRIBE,
                },
            )


@app.post("/api/assistant/plan")
async def api_assistant_plan(request: Request) -> JSONResponse:
    """Transcribe (if audio) -> classify -> route to the debrief flow or agent."""
    _require_feature("assistant")
    _require_model_ready()

    transcript = ""
    client_id = None
    ctype = request.headers.get("content-type", "")

    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        client_id = _valid_client_id(form.get("client_id"))
        text_field = form.get("text")
        audio = form.get("audio")
        if audio is not None and hasattr(audio, "read"):
            data = await audio.read()
            if not data:
                raise HTTPException(status_code=400, detail="Empty audio upload.")
            transcript = await run_in_threadpool(_transcribe_upload_sync, data)
        elif text_field:
            transcript = str(text_field)
    else:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
        transcript = str(body.get("text") or "")
        client_id = _valid_client_id(body.get("client_id"))

    transcript = transcript.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Nothing to work with (empty request).")

    routing = await run_in_threadpool(
        classify.classify, transcript, has_selected_client=bool(client_id)
    )
    if routing["route"] == "session_debrief" and client_id:
        return JSONResponse({"route": "session_debrief", "transcript": transcript})

    hint = routing.get("client_hint") or client_id
    try:
        run = await run_in_threadpool(
            agent.run_agent, transcript, _dt.datetime.now(), client_hint=hint
        )
    except agent.AgentUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc), "fix": exc.hint})

    return JSONResponse(
        {
            "route": "assistant",
            "final_text": run["final_text"],
            "proposals": run["proposals"],
            "transcript": run["transcript"],
            "raw_transcript": transcript,
        }
    )


@app.post("/api/assistant/execute")
async def api_assistant_execute(request: Request) -> JSONResponse:
    """File approved worksheet proposals and stage approved email drafts."""
    _require_feature("assistant")
    _require_model_ready()
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    proposals = body.get("proposals") or []
    if not isinstance(proposals, list):
        raise HTTPException(status_code=400, detail="proposals must be a list.")
    req_client = _valid_client_id(body.get("client_id"))

    results = await run_in_threadpool(
        _execute_proposals_sync, proposals, req_client, body.get("request") or ""
    )
    return JSONResponse({"results": results})


def _execute_proposals_sync(proposals: list, req_client: str | None, request_text: str) -> list[dict]:
    """Blocking half of /api/assistant/execute; runs in a worker thread."""
    results: list[dict] = []
    last_worksheet_file: Path | None = None

    for proposal in proposals:
        if not isinstance(proposal, dict):
            results.append({"type": "?", "status": "failed", "error": "malformed proposal"})
            continue
        ptype = proposal.get("type")

        if ptype == "worksheet":
            res = _file_worksheet(proposal, req_client)
            if res.get("status") == "ok" and res.get("path"):
                last_worksheet_file = Path(res["path"])
            results.append(res)

        elif ptype == "email":
            client_id = _valid_client_id(proposal.get("client_id")) or req_client
            email = _client_email(client_id)
            if not email:
                results.append(
                    {"type": "email", "status": "failed", "error": "no client email on file"}
                )
                continue
            subject = proposal.get("subject") or "A note from your therapist"
            email_body = proposal.get("body") or ""
            attachment = None
            missing_attachment = False
            if proposal.get("attach_worksheet"):
                if last_worksheet_file is not None and last_worksheet_file.exists():
                    attachment = last_worksheet_file
                else:
                    missing_attachment = True
            try:
                ok = actions.create_mail_draft(email, subject, email_body, attachment)
            except Exception as exc:  # noqa: BLE001
                results.append({"type": "email", "status": "failed", "error": str(exc)})
                continue
            if ok:
                detail = f"Draft to {email} left open for review."
                if missing_attachment:
                    detail += (
                        " Note: the worksheet attachment was not available"
                        " (it may have been unchecked above), so the draft has no attachment."
                    )
                results.append({"type": "email", "status": "ok", "detail": detail})
            else:
                results.append({"type": "email", "status": "failed", "error": "Mail returned an error"})
        else:
            results.append({"type": str(ptype), "status": "failed", "error": "unknown proposal type"})

    audit.log_assistant_run({"request": request_text, "results": results, "errors": []})
    return results


# ---------------------------------------------------------------------------
# Client records: read, amend, rename, upload, pdf, email, reveal, trash, search
# ---------------------------------------------------------------------------


def _guard_or_400(rel_path: str) -> Path:
    """Resolve a browser-supplied vault-relative path or raise 400."""
    try:
        return records._guard(rel_path)
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _client_detail_sync(client_id: str) -> dict:
    ctx = vault.client_context(client_id)
    return {
        "client_id": client_id,
        "profile": ctx.get("profile", {}),
        "summary": ctx.get("summary", ""),
        "next_session": ctx.get("next_session"),
        "sessions": records.list_sessions(client_id),
        "documents": [
            d for d in records.list_documents(client_id) if d["kind"] != "session-note"
        ],
    }


@app.get("/api/clients/{client_id}")
async def api_client_detail(client_id: str) -> JSONResponse:
    """Full record for one client: profile, sessions, documents, next session."""
    cid = _valid_client_id(client_id)
    if not cid:
        raise HTTPException(status_code=400, detail="Invalid client id.")
    try:
        detail = await run_in_threadpool(_client_detail_sync, cid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No such client: {cid}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not read client: {exc}",
                "fix": _VAULT_UNREADABLE_FIX,
            },
        )
    # Profile frontmatter can hold YAML date objects; coerce to JSON-safe types.
    return JSONResponse(jsonable_encoder(detail))


def _note_view_sync(rel_path: str) -> dict:
    note = records.read_note(rel_path)
    fragment = render.markdown_to_fragment(
        vault._split_frontmatter(note["markdown"])[1]
    )
    return {
        "path": rel_path,
        "markdown": note["markdown"],
        "html": fragment,
        "frontmatter": note["frontmatter"],
        "kind": note["kind"],
    }


@app.get("/api/notes")
async def api_note(path: str) -> JSONResponse:
    """Read a markdown note: raw markdown, rendered HTML fragment, frontmatter."""
    _guard_or_400(path)
    try:
        view = await run_in_threadpool(_note_view_sync, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found.")
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(view)


@app.post("/api/notes/amend")
async def api_note_amend(request: Request) -> JSONResponse:
    """Append a dated amendment to a filed note (never rewrites history)."""
    body = await request.json()
    path = body.get("path")
    text = body.get("text")
    _guard_or_400(path)
    try:
        new_path = await run_in_threadpool(
            records.append_amendment, path, text, _dt.datetime.now()
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Note not found.")
    except (VaultPathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.log_records_change("Amended", new_path, "Dated amendment added.")
    return JSONResponse({"path": new_path})


@app.post("/api/documents/save")
async def api_document_save(request: Request) -> JSONResponse:
    """Rewrite a non-session markdown document (library/worksheet). Session
    notes are never rewritten; amend them instead."""
    body = await request.json()
    path = body.get("path")
    markdown = body.get("markdown")
    target = _guard_or_400(path)
    if target.parent.name == "Sessions" or target.suffix.lower() != ".md":
        raise HTTPException(
            status_code=400,
            detail="Session notes keep their history. Add an amendment instead.",
        )
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Document not found.")

    def _save() -> None:
        vault._atomic_write(target, markdown or "")

    await run_in_threadpool(_save)
    audit.log_records_change("Edited", path, "Document rewritten.")
    return JSONResponse({"path": path})


@app.post("/api/documents/upload")
async def api_document_upload(
    client_id: str = Form(...),
    file: UploadFile = None,
) -> JSONResponse:
    """Upload a file into a client's Documents/ folder (allowlisted types)."""
    cid = _valid_client_id(client_id)
    if not cid:
        raise HTTPException(status_code=400, detail="Invalid client id.")
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded.")
    data = await file.read()

    def _save() -> dict:
        return records.save_upload(cid, file.filename, data)

    try:
        meta = await run_in_threadpool(_save)
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.log_records_change("Uploaded", meta["path"], f"Added {meta['title']}.")
    return JSONResponse(meta)


@app.post("/api/documents/rename")
async def api_document_rename(request: Request) -> JSONResponse:
    """Rename a document's title. Session notes keep their filename."""
    body = await request.json()
    path = body.get("path")
    new_title = body.get("new_title")
    _guard_or_400(path)
    try:
        new_path = await run_in_threadpool(records.rename_title, path, new_title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except (VaultPathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.log_records_change("Renamed", new_path, f"Title set to {new_title}.")
    return JSONResponse({"path": new_path})


def _resolve_pdf_sync(rel_path: str) -> tuple[Path, str]:
    """Return (pdf_or_original_path, download_filename). Raises for bad kinds."""
    target = records._guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    kind = records._kind_for(target)
    if target.suffix.lower() == ".md":
        pdf = records.render_cache_pdf(rel_path)
        # Prefer a frontmatter/H1 title for the download filename.
        note = records.read_note(rel_path)
        title = note["frontmatter"].get("title") or target.stem
        return pdf, f"{records._slug(str(title))}.pdf"
    if kind in ("upload-pdf", "worksheet-pdf") or target.suffix.lower() == ".pdf":
        return target, target.name
    raise ValueError(f"cannot produce a PDF for {kind}")


@app.get("/api/documents/pdf")
async def api_document_pdf(path: str):
    """Return a PDF: rendered for markdown, passthrough for PDFs, 400 otherwise."""
    _guard_or_400(path)
    try:
        pdf_path, filename = await run_in_threadpool(_resolve_pdf_sync, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except render.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc), "fix": exc.fix})
    except (VaultPathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return FileResponse(
        str(pdf_path), media_type="application/pdf", filename=filename
    )


def _email_document_sync(client_id: str, rel_path: str, subject: str, body: str) -> dict:
    email = _client_email(client_id)
    if not email:
        return {"status": "failed", "error": "no client email on file"}
    target = records._guard(rel_path)
    if not target.is_file():
        return {"status": "failed", "error": "file not found"}
    # Markdown -> rendered PDF attachment; uploads attach the original file.
    attachment = target
    if target.suffix.lower() == ".md":
        try:
            attachment = records.render_cache_pdf(rel_path)
        except render.PdfUnavailable:
            attachment = target  # attach the markdown source as a fallback
    ok = actions.create_mail_draft(email, subject, body, attachment)
    if ok:
        audit.log_records_change("Emailed", rel_path, f"Draft to {email} opened.")
        return {"status": "ok", "detail": f"Draft to {email} left open for review."}
    return {"status": "failed", "error": "Mail returned an error"}


@app.post("/api/documents/email")
async def api_document_email(request: Request) -> JSONResponse:
    """Draft a Mail message to the client with the document attached. NEVER sends."""
    body = await request.json()
    cid = _valid_client_id(body.get("client_id"))
    if not cid:
        raise HTTPException(status_code=400, detail="Invalid client id.")
    path = body.get("path")
    _guard_or_400(path)
    subject = body.get("subject") or "A resource from your therapist"
    email_body = body.get("body") or ""
    result = await run_in_threadpool(_email_document_sync, cid, path, subject, email_body)
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("error", "Could not draft email."))
    return JSONResponse(result)


def _reveal_sync(rel_path: str) -> None:
    target = records._guard(rel_path)
    # A folder is opened, not revealed. "open -R" on a folder selects it inside
    # its parent, which answers a question nobody asked: somebody who asks for
    # a recording's folder wants to see what is in it. The guard has already
    # run, so this can only ever be a folder inside the vault.
    if target.is_dir():
        subprocess.run(["open", str(target)], check=False)
        return
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    reveal_target = target
    if target.suffix.lower() == ".md":
        try:
            reveal_target = records.render_cache_pdf(rel_path)
        except render.PdfUnavailable:
            reveal_target = target
    subprocess.run(["open", "-R", str(reveal_target)], check=False)


def _open_sync(rel_path: str) -> None:
    target = records._guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    subprocess.run(["open", str(target)], check=False)


@app.post("/api/reveal")
async def api_reveal(request: Request) -> JSONResponse:
    """Show something in Finder. A file is revealed (open -R), and markdown
    reveals its rendered PDF; a folder is opened, so its contents are what the
    clinician actually gets. Paths are vault relative, and "." is the vault."""
    body = await request.json()
    path = body.get("path")
    _guard_or_400(path)
    try:
        await run_in_threadpool(_reveal_sync, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    return JSONResponse({"ok": True})


@app.post("/api/open")
async def api_open(request: Request) -> JSONResponse:
    """Open a file with its default macOS application."""
    body = await request.json()
    path = body.get("path")
    _guard_or_400(path)
    try:
        await run_in_threadpool(_open_sync, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    return JSONResponse({"ok": True})


@app.post("/api/trash")
async def api_trash(request: Request) -> JSONResponse:
    """Move a file to the vault trash. Returns a restore token."""
    body = await request.json()
    path = body.get("path")
    _guard_or_400(path)
    try:
        token = await run_in_threadpool(records.trash, path, _dt.datetime.now())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.log_records_change("Moved to trash", path, "")
    return JSONResponse({"token": token})


@app.post("/api/trash/restore")
async def api_trash_restore(request: Request) -> JSONResponse:
    """Restore a trashed file from its token."""
    body = await request.json()
    token = body.get("token")
    try:
        restored = await run_in_threadpool(records.restore, token)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Nothing to restore for that token.")
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.log_records_change("Restored", restored, "")
    return JSONResponse({"path": restored})


@app.get("/api/trash")
async def api_trash_list() -> JSONResponse:
    """List trashed items awaiting restore or the 30-day sweep."""
    items = await run_in_threadpool(records.list_trash)
    return JSONResponse({"items": items})


@app.get("/api/search")
async def api_search(q: str = "") -> JSONResponse:
    """Grouped vault search: clients, notes, and library."""
    query = (q or "").strip()
    if not query:
        return JSONResponse({"clients": [], "notes": [], "library": []})
    hits = await run_in_threadpool(vault.search_vault, query)
    clients, notes, library = [], [], []
    for hit in hits:
        p = hit.get("path", "")
        if p.startswith("Clients/") and p.endswith("_Profile.md"):
            clients.append(hit)
        elif p.startswith("Templates/") or p.startswith("Interventions/"):
            library.append(hit)
        else:
            notes.append(hit)
    return JSONResponse({"clients": clients, "notes": notes, "library": library})


@app.get("/api/library")
async def api_library() -> JSONResponse:
    """Worksheet templates and reference interventions."""
    lib = await run_in_threadpool(records.get_library)
    return JSONResponse(lib)


# ---------------------------------------------------------------------------
# Settings (persistent _Settings store)
# ---------------------------------------------------------------------------


def _settings_payload() -> dict:
    """Assemble the settings screen payload: effective settings, the user
    dictionary, the profession list, and (Phase C) the format list."""
    return {
        "settings": settings_store.load(),
        "dictionary": settings_store.read_dictionary(),
        "professions": vocab.list_professions(),
        "formats": formats.list_specs(),
    }


@app.get("/api/settings")
async def api_settings_get() -> JSONResponse:
    """Return current settings, the user dictionary, and the profession list."""
    payload = await run_in_threadpool(_settings_payload)
    return JSONResponse(payload)


@app.post("/api/settings")
async def api_settings_post(request: Request) -> JSONResponse:
    """Validate and persist a settings patch and/or the user dictionary text.

    Body: {"settings": {<partial patch>}, "dictionary": "<text>"}. Both keys are
    optional. Invalid known values (profession, note_format, stt_engine) 400.
    Returns the refreshed settings payload.
    """
    body = await request.json()
    patch = body.get("settings")
    dictionary = body.get("dictionary")

    if patch is not None:
        if not isinstance(patch, dict):
            raise HTTPException(status_code=400, detail="settings must be an object.")
        # Profession is validated against the vocab registry here; note_format
        # and stt_engine are validated by the store.
        if "profession" in patch:
            valid_professions = {p["id"] for p in vocab.list_professions()}
            if patch["profession"] not in valid_professions:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown profession: {patch['profession']!r}",
                )
        try:
            settings_store.validate_patch(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if dictionary is not None and not isinstance(dictionary, str):
        raise HTTPException(status_code=400, detail="dictionary must be a string.")

    def _persist() -> dict:
        if patch:
            settings_store.save(patch)
        if dictionary is not None:
            settings_store.write_dictionary(dictionary)
        return _settings_payload()

    payload = await run_in_threadpool(_persist)
    return JSONResponse(payload)


@app.delete("/api/settings/formats/{format_id}")
async def api_format_delete(format_id: str) -> JSONResponse:
    """Remove an imported note format. Built-in formats are never deletable.

    Deleting the format that is currently active falls back to DAP so the app can
    never end up pointing at a format that no longer exists. The response always
    reports the active format so the UI can say what happened.
    """

    def _delete() -> dict:
        deleted = formats.delete_custom(format_id)
        active = settings_store.load().get("note_format")
        if active == deleted:
            settings_store.save({"note_format": formats.DEFAULT_FORMAT_ID})
            active = settings_store.load().get("note_format")
        return {"deleted": deleted, "active_note_format": active}

    try:
        result = await run_in_threadpool(_delete)
    except formats.InvalidFormatSpec as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except formats.UnknownFormat:
        raise HTTPException(status_code=404, detail="No such imported format.")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not remove that format: {exc}",
                "fix": _VAULT_UNWRITABLE_FIX,
            },
        )
    audit.log_records_change(
        "Removed note format",
        f"_Settings/formats/{result['deleted']}.json",
        f"Active format is now {result['active_note_format']}.",
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Template import + prompt compiler (upload -> compile -> preview -> save)
# ---------------------------------------------------------------------------

# Cap on an uploaded template. A blank template is tiny; this guards the reader.
_IMPORT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024


_UPLOAD_TOO_LARGE = "That file is too large. Use a blank or example template (under 2 MB)."


@app.post("/api/settings/import/upload")
async def api_import_upload(request: Request, file: UploadFile = None) -> JSONResponse:
    """Read an uploaded template into plain text. Nothing is persisted.

    Returns {doc_text, chars, truncated, pdf_unsupported}. The temp bytes are held
    only for the duration of the read; nothing touches the vault. The body is
    rejected early on a declared oversize content-length and read in bounded
    chunks so an oversized upload never fully lands in memory.
    """
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded.")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _IMPORT_MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=400, detail=_UPLOAD_TOO_LARGE)
        except ValueError:
            pass
    data = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        data += chunk
        if len(data) > _IMPORT_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=_UPLOAD_TOO_LARGE)
    data = bytes(data)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file upload.")
    try:
        result = await run_in_threadpool(
            compiler.extract_document_text, data, file.filename or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except compiler.CompilerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        {
            "doc_text": result["text"],
            "chars": result["chars"],
            "truncated": result["truncated"],
            "pdf_unsupported": result["pdf_unsupported"],
        }
    )


@app.post("/api/settings/import/compile")
async def api_import_compile(request: Request) -> JSONResponse:
    """Derive a candidate format spec from document text.

    Body: {doc_text, profession, mode: "local"|"cloud", api_key?, consent?}.
    local uses the offline model (requires it to be ready). cloud is hard-gated:
    it 400s unless consent is true AND a non-empty key is present, and a Gemini
    failure returns 502 {"error", "fallback": "local"} so the UI can offer local.
    The api_key is never persisted or logged.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    doc_text = str(body.get("doc_text") or "").strip()
    if not doc_text:
        raise HTTPException(status_code=400, detail="Nothing to compile (empty document).")
    # Hard cap the document here so nothing oversized ever reaches compile_local
    # or Gemini, no matter how the text arrived (paste bypasses the upload cap).
    doc_text, doc_truncated = compiler._cap(doc_text)
    profession = str(body.get("profession") or "therapy")
    mode = str(body.get("mode") or "local").strip().lower()

    if mode == "cloud":
        # HARD server-side gate: consent AND a key are both required, always.
        # consent must be a strict boolean true, not a truthy "false"/"0"/1.
        consent = body.get("consent") is True
        api_key = str(body.get("api_key") or "").strip()
        if not consent or not api_key:
            raise HTTPException(
                status_code=400,
                detail="Cloud compile requires explicit consent and an API key.",
            )
        try:
            out = await run_in_threadpool(compiler.compile_gemini, doc_text, api_key)
        except compiler.CompilerError as exc:
            # The key is already scrubbed inside CompilerError; return a fallback
            # hint so the UI can offer to compile locally instead.
            return JSONResponse(
                status_code=502,
                content={"error": str(exc), "fallback": "local"},
            )
        finally:
            # Drop the key reference as soon as the call returns, either way.
            api_key = ""
        return JSONResponse(
            {
                "spec": out["spec"],
                "prompt_layer": out["prompt_layer"],
                "truncated": doc_truncated,
            }
        )

    # Default: local compile.
    _require_model_ready()
    try:
        spec = await run_in_threadpool(compiler.compile_local, doc_text, profession)
    except compiler.CompilerError as exc:
        # The readiness guard above runs off a ~10s cache, so LM Studio can quit
        # between the check and the call. That used to surface the raw
        # "HTTPConnectionPool ... Connection refused" traceback verbatim in the
        # import modal. An unreachable model is a 503 the user can fix; a model
        # that answered with something unusable stays a 422.
        if _model_unavailable(exc):
            raise HTTPException(
                status_code=503,
                detail={"error": str(exc), "fix": _MODEL_FIX_COMPILE},
            )
        raise HTTPException(status_code=422, detail=str(exc))
    except llm.ModelUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": str(exc), "fix": _MODEL_FIX_COMPILE},
        )
    return JSONResponse({"spec": spec, "truncated": doc_truncated})


@app.post("/api/settings/import/preview")
async def api_import_preview(request: Request) -> JSONResponse:
    """Render a candidate spec against a bundled sample transcript (dry run).

    Body: {spec, profession}. Validates the spec, then runs one extraction with
    the transient spec and returns {note, sections}. No vault write.
    """
    _require_model_ready()
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    spec = body.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="spec must be an object.")
    profession = str(body.get("profession") or "therapy")
    try:
        validated = formats._validate_spec(spec)
    except formats.InvalidFormatSpec as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = await run_in_threadpool(compiler.dry_run, validated, profession)
    except compiler.CompilerError as exc:
        if _model_unavailable(exc):
            raise HTTPException(
                status_code=503,
                detail={"error": str(exc), "fix": _MODEL_FIX_PREVIEW},
            )
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        if _model_unavailable(exc):
            raise HTTPException(
                status_code=503,
                detail={"error": str(exc), "fix": _MODEL_FIX_PREVIEW},
            )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Preview failed: {exc}", "fix": _MODEL_FIX_PREVIEW},
        )
    return JSONResponse(result)


@app.post("/api/settings/import/save")
async def api_import_save(request: Request) -> JSONResponse:
    """Persist a reviewed spec (and its optional prompt layer).

    Body: {spec, prompt_layer?, set_active?}. Saves the custom format, writes the
    prompt layer to _Settings/profile/<id>.prompt.md when present, and sets the
    active note format when set_active is true. Returns the saved spec summary.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    spec = body.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="spec must be an object.")
    prompt_layer = body.get("prompt_layer")
    if prompt_layer is not None and not isinstance(prompt_layer, str):
        raise HTTPException(status_code=400, detail="prompt_layer must be a string.")
    if isinstance(prompt_layer, str) and len(prompt_layer) > 50_000:
        raise HTTPException(
            status_code=400, detail="prompt_layer is too long (50000 char max)."
        )
    set_active = bool(body.get("set_active"))

    def _persist() -> dict:
        saved = formats.save_custom(spec)
        fid = saved["id"]
        layer = (prompt_layer or "").strip()
        if layer:
            # id is already a bare slug out of save_custom; guard again so a hand
            # crafted id can never escape the profile directory.
            if fid != formats.slugify_id(fid):
                raise formats.InvalidFormatSpec(f"unsafe format id: {fid!r}")
            settings_store.profile_dir().mkdir(parents=True, exist_ok=True)
            settings_store._atomic_write(
                settings_store.profile_dir() / f"{fid}.prompt.md", layer + "\n"
            )
        if set_active:
            settings_store.save({"note_format": fid})
        return {
            "id": fid,
            "name": saved["name"],
            "clinical": saved["clinical"],
            "sections": len(saved["sections"]),
            "prompt_layer": bool(layer),
            "active": set_active,
        }

    try:
        summary = await run_in_threadpool(_persist)
    except formats.InvalidFormatSpec as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(summary)


# ---------------------------------------------------------------------------
# Session sources: bring material in, confirm what it is, read it, delete it
# ---------------------------------------------------------------------------
#
# Every route here is per client, because the storage tree is per client and
# because consent is a fact about a person, not about a file. client_id is
# therefore required rather than inferred, and both ids are validated against
# recstore's own rules before they are ever turned into a path.
#
# The one decision that matters: when the classifier is unsure what a document
# is, the source is stored with its guess and NO work is queued. The confirm
# route queues it after a human has answered. Classifying a meeting tool's AI
# write up as verbatim without that answer is how a sentence nobody said ends up
# in a clinical record as the client's own words, and it stays there.

# A 50-minute recording is roughly 25 MB and a video is far more, so the cap is
# generous. It exists so a mis-drop cannot fill the disk, not to police size.
_SOURCE_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

_SOURCE_TOO_LARGE = (
    "That file is larger than 2 GB. Export just the audio track, or split the "
    "recording, and bring it in again."
)

# What Debrief can actually read. Anything else is refused by name, with the
# list of what works, so the answer is never just "no".
_SOURCE_ALLOWED_EXTS = (
    sources.AUDIO_EXTS | sources.VIDEO_EXTS | sources.TRANSCRIPT_EXTS | sources.DOC_EXTS
)

_SOURCE_ACCEPTS = (
    "Debrief reads audio (.m4a, .mp3, .wav, .aac), video (.mp4, .mov, .webm), "
    "transcripts (.vtt, .srt), and documents (.txt, .md, .pdf, .docx)."
)

# Bodies this section will read. The paste cap is the interesting one: a
# transcript pasted into a text box is the one JSON body here that can be large.
_PASTE_MAX_BYTES = 2 * 1024 * 1024
_JSON_MAX_BYTES = 64 * 1024

_PASTE_TOO_LARGE = (
    "That text is too long to paste. Save it as a file and bring it in that way, "
    "or paste up to 2 MB at a time."
)
_JSON_TOO_LARGE = "That request is larger than this endpoint accepts."

_DIGEST_NOT_READY = (
    "The session digest is not ready yet. It appears here once Debrief has "
    "finished reading this material."
)

_NO_CLIENT_ID = "A client id is required: session material is filed under the client it belongs to."

# Said at every point consent is set or refused. It states whose job consent is
# and stops there. Recording law varies and this app takes no position on it.
_CONSENT_NOTE = (
    "Obtaining and documenting this client's consent to be recorded is your "
    "responsibility. Debrief stores the dated flag you set here and uses it to "
    "decide whether session material can be brought in for this client."
)


def _source_http_error(exc: Exception) -> HTTPException:
    """Map an ingestion failure onto a status code, keeping the module's words.

    ConsentError first: it is a SourceError, and the whole point of it is that a
    missing consent flag is not the same answer as a file that could not be read.
    """
    if isinstance(exc, sources.ConsentError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, sources.SourceError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


async def _json_body(request: Request, limit: int, too_large: str) -> dict:
    """Read a bounded JSON object body, or raise 400 / 413.

    The body is accumulated a chunk at a time and abandoned the moment it passes
    the limit, so an oversized paste is refused partway through rather than
    after it has all been held in memory. The declared content-length is checked
    first, which usually means it is refused before any of it is read.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail=too_large)
        except ValueError:
            pass
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail=too_large)
    try:
        body = json.loads(bytes(raw) or b"null")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected an object.")
    return body


def _source_client_id(client_id) -> str:
    """A client id safe to build a Recordings path from, or 400."""
    cid = _valid_client_id(client_id)
    if not cid:
        raise HTTPException(status_code=400, detail=_NO_CLIENT_ID)
    return cid


def _source_ids(client_id, source_id) -> tuple[str, str]:
    """Validate both ids together. source_dir creates nothing and raises on either."""
    cid = _source_client_id(client_id)
    try:
        recstore.source_dir(cid, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return cid, str(source_id)


def _copy_capped(src, dest: Path) -> int:
    """Copy a file object into dest in 64 KB chunks, capped. Bytes written.

    The cap is enforced against what actually arrives, not against what the
    request claimed: a body that under-declares its length is still stopped at
    the same byte. Raises HTTPException(413) the moment the cap is passed, and
    the caller's temp directory takes the partial file with it.
    """
    written = 0
    with dest.open("wb") as handle:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > _SOURCE_MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=_SOURCE_TOO_LARGE)
            handle.write(chunk)
    return written


async def _stream_upload(file: UploadFile, dest: Path) -> int:
    """Stage an upload on disk without blocking the event loop. Bytes written.

    The whole copy runs in a worker thread rather than a chunk at a time on the
    loop: at the 2 GB cap that is one hop instead of tens of thousands of
    blocking writes between them.
    """
    await file.seek(0)
    return await run_in_threadpool(_copy_capped, file.file, dest)


def _job_view(record: dict | None) -> dict | None:
    """The part of a job record a source panel needs: state, stage, and failure."""
    if not record:
        return None
    return {
        "id": record.get("id"),
        "state": record.get("state"),
        "stage": record.get("stage"),
        "done": record.get("done", 0),
        "total": record.get("total", 0),
        "error": record.get("error"),
        "interrupted": bool(record.get("interrupted")),
    }


def _newest_job_for(job_rows: list[dict], source_id: str) -> dict | None:
    """The most recent job naming this source. list_jobs is already newest first.

    Linear in the job list per source, so a listing is sources times jobs. That
    is the right trade for a single-user local app: the alternative is an index
    to keep in step with two directories, and the numbers here are dozens.
    """
    for record in job_rows:
        payload = record.get("payload") or {}
        if str(payload.get("source_id") or "") == source_id:
            return record
    return None


def _source_has_work(client_id: str, source_id: str) -> bool:
    """True when the ladder has something left to build for this source.

    A digest already on disk means there is nothing to do. Otherwise there is
    work, unless the source enters at rung 0 with neither a transcript nor any
    audio to make one from, which is a source there is nothing to build from at
    all: queueing that would be queueing a job to fail.
    """
    rungs = recstore.rungs_present(client_id, source_id)
    if 2 in rungs:
        return False
    meta = recstore.read_meta(client_id, source_id) or {}
    try:
        entry_rung = int(meta.get("entry_rung", 0))
    except (TypeError, ValueError):
        entry_rung = 0
    if entry_rung == 0 and not rungs:
        return recstore.audio_path(client_id, source_id) is not None
    return True


def _queue_ladder(client_id: str, source_id: str) -> tuple[str | None, str | None]:
    """Queue the ladder for a source. Returns (job_id, error message).

    A queue that cannot accept the job is reported rather than swallowed: the
    material is stored either way, and a source that silently never processes is
    the failure mode this whole feature was written to avoid.
    """
    if not _source_has_work(client_id, source_id):
        return None, None
    try:
        return jobs.submit("ladder", {"client_id": client_id, "source_id": source_id}), None
    except Exception as exc:  # noqa: BLE001 - the material is in; say what did not happen
        return None, (
            f"The material was saved but Debrief could not start processing it: {exc}"
        )


def _ingest_payload(
    source: sources.Source,
    needs_confirmation: bool,
    job_id: str | None,
    job_error: str | None,
) -> dict:
    return {
        "source_id": source.id,
        "client_id": source.client_id,
        "kind": source.kind,
        "verbatim": bool(source.verbatim),
        "produced_by": source.produced_by,
        "entry_rung": source.entry_rung,
        "needs_confirmation": bool(needs_confirmation),
        "job_id": job_id,
        "job_error": job_error,
    }


def _source_display(entry: dict) -> dict:
    """The two stored fields no caller should have to interpret for itself.

    session_date becomes the EFFECTIVE date: the one the clinician stated, else
    the day the material arrived. A source stored before session dates existed
    still comes back with a date, and it is the one that was true of it all
    along, so no surface has to carry the fallback rule.

    linked_notes is always a list. "No notes yet" and "stored before links
    existed" mean the same thing to a caller, so they arrive looking the same.
    """
    session_date = sources.effective_session_date(entry)
    entry["session_date"] = session_date.isoformat() if session_date else None
    entry["linked_notes"] = [
        str(item) for item in (entry.get("linked_notes") or []) if str(item).strip()
    ]
    return entry


def _enriched_source(client_id: str, source_id: str, job_rows: list[dict]) -> dict | None:
    """One source's provenance joined with its rungs, its audio, and its job."""
    meta = recstore.read_meta(client_id, source_id)
    if meta is None:
        return None
    entry = dict(meta)
    entry.update(
        {
            "client_id": client_id,
            "source_id": source_id,
            "rungs": sorted(recstore.rungs_present(client_id, source_id)),
            "has_audio": recstore.audio_path(client_id, source_id) is not None,
            "needs_confirmation": bool(meta.get("needs_confirmation")),
            "job": _job_view(_newest_job_for(job_rows, source_id)),
        }
    )
    return _source_display(entry)


@app.post("/api/sources/upload")
async def api_source_upload(
    request: Request,
    client_id: str = Form(...),
    declared_kind: str = Form(None),
    file: UploadFile = None,
) -> JSONResponse:
    """Bring a recording, video, transcript, or document in as a session source.

    The file is streamed to a temp directory in bounded chunks and never held
    whole in memory. Consent is checked inside sources.ingest_file, before a
    single byte is read as content, and a missing flag is a 403 carrying that
    module's own words.

    The declared content-length is checked as well as the streamed bytes, so an
    oversize upload is usually refused without the copy; the streamed check is
    what actually enforces the cap, because a declared length is a claim.

    Returns the stored source and, when nothing needs confirming and there is
    work to do, the id of the queued ladder job.
    """
    cid = _source_client_id(client_id)
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _SOURCE_ALLOWED_EXTS:
        named = f"a {ext} file" if ext else "that file"
        raise HTTPException(
            status_code=400,
            detail=f"Debrief cannot read {named}. {_SOURCE_ACCEPTS}",
        )

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _SOURCE_MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=_SOURCE_TOO_LARGE)
        except ValueError:
            pass

    # Consent before staging, not only inside ingest_file. The gate in the
    # module is the authoritative one and stays; this is the same question asked
    # early enough that a client with no consent on file does not first have
    # gigabytes of their session copied onto the disk to be told no.
    granted, _consent_date = await run_in_threadpool(vault.recording_consent, cid)
    if not granted:
        raise HTTPException(status_code=403, detail=sources.CONSENT_MESSAGE)

    # One temp directory for the whole request, removed on every path out of
    # here, including the oversize one and every ingestion failure below.
    tmp_dir = Path(tempfile.mkdtemp(prefix="debrief_source_upload_"))
    try:
        # The extension is the only part of the client's filename that is used,
        # and it is already known to be on the allowlist, so nothing a browser
        # sent becomes part of a path.
        staged = tmp_dir / f"upload{ext}"
        if await _stream_upload(file, staged) == 0:
            raise HTTPException(status_code=400, detail="Empty file upload.")
        try:
            source, needs_confirmation = await run_in_threadpool(
                sources.ingest_file, staged, cid, (declared_kind or "").strip() or None
            )
        except (sources.SourceError, ValueError) as exc:
            raise _source_http_error(exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    job_id, job_error = (None, None)
    if not needs_confirmation:
        job_id, job_error = await run_in_threadpool(_queue_ladder, cid, source.id)
    return JSONResponse(_ingest_payload(source, needs_confirmation, job_id, job_error))


@app.post("/api/sources/paste")
async def api_source_paste(request: Request) -> JSONResponse:
    """Bring pasted text in as a source. Body: {client_id, text, kind}.

    kind is pasted_transcript (word for word, rung 0) or clinician_notes (the
    clinician's own observation, rung 2). Nothing is guessed, so nothing needs
    confirming. Consent is gated exactly as it is on the upload path.
    """
    body = await _json_body(request, _PASTE_MAX_BYTES, _PASTE_TOO_LARGE)
    cid = _source_client_id(body.get("client_id"))
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string.")
    kind = str(body.get("kind") or "").strip()

    try:
        source, needs_confirmation = await run_in_threadpool(sources.ingest_text, text, cid, kind)
    except (sources.SourceError, ValueError) as exc:
        raise _source_http_error(exc)

    # Nothing is guessed on this path today, so ingest_text never asks for a
    # confirmation. The gate is still here rather than in the callee: "unsure
    # means queue nothing" is a rule about what the server does, and a route
    # that only holds it because of what another module happens to return is a
    # route that stops holding it the day that module changes.
    job_id, job_error = (None, None)
    if not needs_confirmation:
        job_id, job_error = await run_in_threadpool(_queue_ladder, cid, source.id)
    return JSONResponse(_ingest_payload(source, needs_confirmation, job_id, job_error))


@app.post("/api/sources/{source_id}/confirm")
async def api_source_confirm(source_id: str, request: Request) -> JSONResponse:
    """Record the clinician's answer about what a document actually is.

    Body: {client_id, verbatim: bool, produced_by: str}. Their answer overrides
    the classifier outright, the source is rebuilt from its retained text, and
    the ladder is queued afterwards. A recording is a 409: there is nothing to
    confirm about a microphone.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    cid, sid = _source_ids(body.get("client_id"), source_id)
    verbatim = body.get("verbatim")
    if not isinstance(verbatim, bool):
        raise HTTPException(
            status_code=400,
            detail="verbatim must be true (word for word) or false (a write up).",
        )
    produced_by = body.get("produced_by")
    if not isinstance(produced_by, str):
        raise HTTPException(status_code=400, detail="produced_by must be a string.")

    meta = await run_in_threadpool(recstore.read_meta, cid, sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="No such source for this client.")
    kind = str(meta.get("kind") or "")
    if kind in sources.MEDIA_KINDS:
        raise HTTPException(status_code=409, detail=sources.MEDIA_CONFIRM_MESSAGE)
    if kind in sources.REFERENCE_KINDS:
        # Refused here rather than one layer down, because the layer down is
        # behind _cancel_source_jobs. reclassify would raise the same message,
        # but by then a stale confirm click from a screen opened before the
        # source was made reference material would have killed the ladder run
        # that is building its reference digest, and nothing queues it again.
        raise HTTPException(status_code=422, detail=sources.REFERENCE_CONFIRM_MESSAGE)

    def _confirm() -> tuple[sources.Source, list[str]]:
        # Any ladder already running for this source is building rungs under
        # the provenance that is about to change. Stopping it first is not
        # tidiness: reclassify deletes rung 2, and a job still running under
        # the old answer would write its digest back afterwards, at which
        # point the next ladder finds a rung 2 on disk and hands out the stale
        # digest instead of rebuilding it. That is the exact failure the
        # confirm flow exists to prevent, arriving by another door.
        cancelled = _cancel_source_jobs(cid, sid)
        return sources.reclassify(cid, sid, verbatim, produced_by), cancelled

    try:
        source, cancelled = await run_in_threadpool(_confirm)
    except (sources.SourceError, ValueError) as exc:
        raise _source_http_error(exc)

    job_id, job_error = await run_in_threadpool(_queue_ladder, cid, source.id)
    payload = _ingest_payload(source, False, job_id, job_error)
    payload["cancelled_jobs"] = cancelled
    return JSONResponse(payload)


@app.post("/api/sources/{source_id}/reference")
async def api_source_reference(source_id: str, request: Request) -> JSONResponse:
    """Move a source between session material and reference material.

    Body: {client_id, reference: bool}. true files it as reference material (a
    talk, an article, something saved to read again), false files it back as
    material from a session with this client.

    Only what the material is FOR changes. Whether it is word for word is a fact
    about the bytes and survives the flip, so the transcript and the audio are
    untouched. The session summary does not survive: a session digest and a
    reference digest are different shapes with different rules, so the stale one
    is deleted and a fresh ladder is queued to rebuild it.

    Flipping the way a source already points does nothing at all, and does it
    without stopping a job or queueing one. A second click must not cost a
    summary and the minutes of work to rebuild it.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    cid, sid = _source_ids(body.get("client_id"), source_id)
    reference = body.get("reference")
    if not isinstance(reference, bool):
        raise HTTPException(
            status_code=400,
            detail=(
                "reference must be true (reference material) or false (material "
                "from a session with this client)."
            ),
        )

    meta = await run_in_threadpool(recstore.read_meta, cid, sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="No such source for this client.")

    def _row() -> dict | None:
        return _enriched_source(cid, sid, jobs.list_jobs(cid))

    already = str(meta.get("kind") or "") in sources.REFERENCE_KINDS
    if already == reference:
        # Nothing to rebuild, so nothing is stopped and nothing is queued. The
        # check lives here rather than only in reclassify (which is already a
        # no-op) because the cancel and the queue happen out here, and a route
        # that runs them anyway would charge a digest for a second click.
        entry = await run_in_threadpool(_row)
        return JSONResponse(
            jsonable_encoder(
                {**(entry or {}), "job_id": None, "job_error": None, "cancelled_jobs": []}
            )
        )

    def _flip() -> tuple[sources.Source, list[str]]:
        # Same reasoning as the confirm route: a ladder still running under the
        # old kind would write its digest back after the flip deleted rung 2,
        # and the next read would hand out a digest built to the wrong shape.
        cancelled = _cancel_source_jobs(cid, sid)
        return sources.reclassify(cid, sid, reference=reference), cancelled

    try:
        source, cancelled = await run_in_threadpool(_flip)
    except (sources.SourceError, ValueError) as exc:
        raise _source_http_error(exc)

    job_id, job_error = await run_in_threadpool(_queue_ladder, cid, source.id)
    entry = await run_in_threadpool(_row)
    return JSONResponse(
        jsonable_encoder(
            {
                **(entry or {}),
                "job_id": job_id,
                "job_error": job_error,
                "cancelled_jobs": cancelled,
            }
        )
    )


async def _existing_source(client_id, source_id) -> tuple[str, str]:
    """Both ids validated and the source known to exist, or 400 / 404.

    "There is no such source" and "that source cannot do this yet" are different
    answers, and every route below wants the first one settled before it starts.
    """
    cid, sid = _source_ids(client_id, source_id)
    if await run_in_threadpool(recstore.read_meta, cid, sid) is None:
        raise HTTPException(status_code=404, detail="No such source for this client.")
    return cid, sid


@app.post("/api/sources/{source_id}/draft")
async def api_source_draft(source_id: str, request: Request) -> JSONResponse:
    """Draft a note from stored material alone. Body: {client_id}.

    Returns the same plan shape a debrief returns, so it flows into the review
    screen the clinician already knows: a note they can edit, with no actions in
    it and no transcript behind it. Nothing is filed here. /api/execute files it
    after they approve it, exactly as it does for a debrief.

    409 when there is nothing to draft from yet, carrying the plain reason: the
    summary is still being built, was never built, or nobody has confirmed what
    the material is.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    cid, sid = await _existing_source(body.get("client_id"), source_id)

    try:
        plan = await run_in_threadpool(pipeline.plan_from_source, cid, sid)
    except sources.SourceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(jsonable_encoder(plan))


@app.post("/api/sources/{source_id}/assign")
async def api_source_assign(source_id: str, request: Request) -> JSONResponse:
    """Connect stored material to a note that is already filed.

    Body: {client_id, note_path}. The note gains a dated amendment naming the
    material and where it lives; nothing above that amendment is touched,
    because a filed note is never rewritten. Connecting the same material to the
    same note twice appends nothing and says so.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    cid, sid = await _existing_source(body.get("client_id"), source_id)
    note_path = body.get("note_path")
    if not isinstance(note_path, str) or not note_path.strip():
        raise HTTPException(
            status_code=400,
            detail="note_path must name one of this client's filed session notes.",
        )

    try:
        result = await run_in_threadpool(
            sources.link_source_to_note, cid, sid, note_path
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except sources.SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (VaultPathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not result["already_linked"]:
        audit.log_records_change(
            "Amended", result["note_path"], "Session material connected."
        )
    return JSONResponse(result)


@app.patch("/api/sources/{source_id}/session-date")
async def api_source_session_date(source_id: str, request: Request) -> JSONResponse:
    """Correct when this source's session actually happened.

    Body: {client_id, session_date}. captured_at is not touched: when the
    material arrived is the app's own statement about its own history, and it is
    nobody's to edit. Returns the updated source row.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    cid, sid = await _existing_source(body.get("client_id"), source_id)

    def _set() -> dict | None:
        sources.set_session_date(cid, sid, body.get("session_date"))
        return _enriched_source(cid, sid, jobs.list_jobs(cid))

    try:
        entry = await run_in_threadpool(_set)
    except sources.SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if entry is None:
        raise HTTPException(status_code=404, detail="No such source for this client.")
    return JSONResponse(jsonable_encoder(entry))


@app.get("/api/sources")
async def api_sources_list(client_id: str = "") -> JSONResponse:
    """Every source for one client: provenance, rungs built, audio, latest job."""
    cid = _source_client_id(client_id)

    def _read() -> dict:
        job_rows = jobs.list_jobs(cid)
        rows = []
        for row in recstore.list_sources(cid):
            row = dict(row)
            row["job"] = _job_view(_newest_job_for(job_rows, row["source_id"]))
            rows.append(_source_display(row))
        return {"sources": rows}

    try:
        payload = await run_in_threadpool(_read)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/sources/{source_id}")
async def api_source_detail(source_id: str, client_id: str = "") -> JSONResponse:
    """One source, with the same enrichment as the list."""
    cid, sid = _source_ids(client_id, source_id)

    def _read() -> dict | None:
        return _enriched_source(cid, sid, jobs.list_jobs(cid))

    entry = await run_in_threadpool(_read)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such source for this client.")
    return JSONResponse(jsonable_encoder(entry))


@app.get("/api/sources/{source_id}/digest")
async def api_source_digest(source_id: str, client_id: str = "") -> JSONResponse:
    """The session digest for one source, once the ladder has built it.

    load_digest re-applies the provenance rules on the way out, so a source
    reclassified since it was digested hands over a digest with its quotes
    already gone.
    """
    cid, sid = _source_ids(client_id, source_id)
    if await run_in_threadpool(recstore.read_meta, cid, sid) is None:
        # A source that is not there and a digest that is not built yet are two
        # different answers, and only one of them is worth waiting for.
        raise HTTPException(status_code=404, detail="No such source for this client.")
    digest = await run_in_threadpool(ladder.load_digest, cid, sid)
    if digest is None:
        raise HTTPException(status_code=404, detail=_DIGEST_NOT_READY)
    return JSONResponse(jsonable_encoder(digest))


def _cancel_source_jobs(client_id: str, source_id: str) -> list[str]:
    """Stop any unfinished job for this source. Returns the ids that were stopped.

    Called before a whole-source delete and before a reclassification, so a
    ladder is not still building rungs into a folder that is being trashed or
    under provenance that has just changed.

    What this actually buys is a job that stops at its next rung boundary, not
    one that stops now: a call already in flight finishes, and write_rung
    recreates the folder it writes into. The window is small and the alternative
    is killing a call mid run, which the queue deliberately refuses to do.
    """
    cancelled: list[str] = []
    for record in jobs.list_jobs(client_id):
        payload = record.get("payload") or {}
        if str(payload.get("source_id") or "") != source_id:
            continue
        job_id = str(record.get("id") or "")
        if job_id and jobs.cancel(job_id):
            cancelled.append(job_id)
    return cancelled


@app.delete("/api/sources/{source_id}")
async def api_source_delete(
    source_id: str, client_id: str = "", audio_only: bool = False
) -> JSONResponse:
    """Delete a source, or just its audio.

    audio_only removes the recording permanently and keeps everything else, so
    the note's evidence trail outlives the bytes. It deliberately does NOT stop
    a pending ladder: cancelling it would leave a source with no audio and no
    transcript either, which is not "the trail outlives the audio", it is the
    trail being deleted quietly by a route that says it kept it. A ladder that
    then finds the audio gone fails with a message saying so, which is the
    honest outcome and a recoverable one.

    A whole-source delete goes through the vault trash and its 30-day grace
    period, and does stop the ladder first, because nothing should be writing
    rungs into a folder on its way out.

    Nothing is cancelled before it is known that there is something to delete.
    A 404 must not have side effects.
    """
    cid, sid = _source_ids(client_id, source_id)

    def _delete() -> dict:
        # "That source has no audio" and "there is no such source" are different
        # answers, and telling a clinician the first when the second is true
        # says their material is filed somewhere when it is not.
        if not recstore.source_dir(cid, sid).is_dir():
            return {"deleted": False, "missing": True, "cancelled_jobs": []}
        if audio_only:
            if recstore.audio_path(cid, sid) is None:
                return {"deleted": False, "missing": False, "cancelled_jobs": []}
            return {
                "deleted": recstore.delete_audio(cid, sid),
                "missing": False,
                "cancelled_jobs": [],
            }
        cancelled = _cancel_source_jobs(cid, sid)
        return {
            "deleted": recstore.delete_source(cid, sid),
            "missing": False,
            "cancelled_jobs": cancelled,
        }

    try:
        result = await run_in_threadpool(_delete)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    missing = result.pop("missing", False)
    if not result["deleted"]:
        raise HTTPException(
            status_code=404,
            detail=(
                "There is no audio stored for that source."
                if audio_only and not missing
                else "No such source for this client."
            ),
        )
    audit.log_records_change(
        "Deleted audio" if audio_only else "Moved to trash",
        f"Recordings/{cid}/{sid}",
        "",
    )
    return JSONResponse(result)


@app.post("/api/clients/{client_id}/recording-consent")
async def api_recording_consent(client_id: str, request: Request) -> JSONResponse:
    """Set or withdraw a client's dated recording consent. Body: {granted: bool}.

    Withdrawing keeps the date it was granted on: that date is the answer to
    "was there consent at the time", and erasing it would erase the answer.
    """
    body = await _json_body(request, _JSON_MAX_BYTES, _JSON_TOO_LARGE)
    granted = body.get("granted")
    if not isinstance(granted, bool):
        raise HTTPException(
            status_code=400,
            detail=f"granted must be true or false. {_CONSENT_NOTE}",
        )
    cid = _source_client_id(client_id)

    def _set() -> dict:
        vault.set_recording_consent(cid, granted)
        on, date = vault.recording_consent(cid)
        return {"granted": on, "date": date, "note": _CONSENT_NOTE}

    try:
        payload = await run_in_threadpool(_set)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No such client: {cid}")
    except VaultPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        # A properties block that will not parse. Refusing is the safe answer:
        # writing one key into it would take the rest of the profile with it.
        raise HTTPException(status_code=409, detail=f"{exc}")
    audit.log_records_change(
        "Recording consent granted" if granted else "Recording consent withdrawn",
        f"Clients/{cid}/_Profile.md",
        "",
    )
    return JSONResponse(payload)


@app.get("/api/jobs")
async def api_jobs(client_id: str = "") -> JSONResponse:
    """Background jobs, newest first, plus any failure the records could not hold.

    worker_error is the last failure the worker could not write to a job record
    (a full disk, a vault that has gone away). It has nowhere else to surface,
    and a failure nobody can see is the one this queue was built to end.
    """
    wanted = (client_id or "").strip()
    cid = _valid_client_id(wanted) if wanted else None
    if wanted and not cid:
        raise HTTPException(status_code=400, detail=f"Invalid client id: {wanted!r}")

    def _read() -> dict:
        return {"jobs": jobs.list_jobs(cid), "worker_error": jobs.last_worker_error()}

    payload = await run_in_threadpool(_read)
    return JSONResponse(jsonable_encoder(payload))


# ---------------------------------------------------------------------------
# First-run wizard: permission triggers + setup marker
# ---------------------------------------------------------------------------
#
# Each permission endpoint makes the smallest possible real call that trips the
# relevant macOS TCC prompt, so the wizard can say "click Allow, then Re-check"
# instead of leaving the user to hunt through System Settings. Everything here
# is best-effort: a failure returns a friendly result, never a 500.


# The macOS "Automation" TCC denial. osascript surfaces it as error -1743
# ("Not authorized to send Apple events to <app>").
_TCC_DENIED = "-1743"


def _classify_osascript(ok: bool, out: str, deny_hint: str, ok_hint: str) -> dict:
    """Map an osascript (ok, output) pair into a permission result dict.

    granted=True on success, False on the TCC denial (-1743 / Not authorized),
    and "unknown" for any other error, with the error text carried in hint.
    """
    if ok:
        return {"granted": True, "hint": ok_hint}
    text = (out or "").strip()
    if _TCC_DENIED in text or "not authorized" in text.lower():
        return {"granted": False, "hint": deny_hint}
    return {"granted": "unknown", "hint": f"Could not tell yet: {text}" if text else "Could not tell yet."}


def _calendar_permission_sync() -> dict:
    """Ask Calendar for the name of the dedicated Debrief calendar, creating it
    if missing (same rule as actions.create_calendar_event). This is a pure
    read/create with no events, purely to trip the Automation prompt."""
    cal = actions.CALENDAR_NAME
    script = (
        'tell application "Calendar"\n'
        f'  if not (exists calendar "{cal}") then\n'
        f'    make new calendar with properties {{name:"{cal}"}}\n'
        "  end if\n"
        f'  return name of calendar "{cal}"\n'
        "end tell\n"
    )
    ok, out = actions._run_osascript(script)
    return _classify_osascript(
        ok,
        out,
        deny_hint=(
            "Open System Settings, Privacy and Security, Automation, and allow "
            "Debrief (or your terminal) to control Calendar, then Re-check."
        ),
        ok_hint="Calendar access is granted. Debrief can book follow-up appointments.",
    )


def _mail_permission_sync() -> dict:
    """Benign Mail scripting call: ask Mail its own name. It targets Mail by a
    tell block (so it sends a real Apple event to Mail and trips Mail's
    Automation prompt), and never creates a draft or sends anything."""
    script = 'tell application "Mail" to get name\n'
    ok, out = actions._run_osascript(script)
    return _classify_osascript(
        ok,
        out,
        deny_hint=(
            "Open System Settings, Privacy and Security, Automation, and allow "
            "Debrief (or your terminal) to control Mail, then Re-check."
        ),
        ok_hint="Mail access is granted. Debrief can prepare email drafts for your review.",
    )


def _screen_permission_sync() -> dict:
    """Attempt one screencapture via the verify.py capture path. Treats a real,
    non-trivial image on disk as granted. Screen Recording notoriously needs an
    app restart after granting, so the hint always says so."""
    hint = (
        "Screen Recording lets Debrief read the screen to verify what it did. "
        "If macOS just prompted you, allow Debrief, then quit and reopen the "
        "app so the grant takes effect, then Re-check."
    )
    tmp = Path(tempfile.gettempdir()) / f"debrief_perm_{uuid.uuid4().hex}.png"
    try:
        ok = verify._capture_and_downscale(str(tmp))
        size = tmp.stat().st_size if tmp.exists() else 0
        granted = bool(ok and size > 1024)
    except Exception:  # noqa: BLE001
        granted = False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return {"granted": granted, "hint": hint}


@app.post("/api/permissions/calendar")
async def api_permission_calendar() -> JSONResponse:
    """Trip the Calendar Automation prompt. Returns {granted, hint}."""
    return JSONResponse(await run_in_threadpool(_calendar_permission_sync))


@app.post("/api/permissions/mail")
async def api_permission_mail() -> JSONResponse:
    """Trip the Mail Automation prompt (no draft created). Returns {granted, hint}."""
    return JSONResponse(await run_in_threadpool(_mail_permission_sync))


@app.post("/api/permissions/screen")
async def api_permission_screen() -> JSONResponse:
    """Trip the Screen Recording prompt via a screencapture. Returns {granted, hint}."""
    return JSONResponse(await run_in_threadpool(_screen_permission_sync))


@app.post("/api/setup/complete")
def api_setup_complete() -> JSONResponse:
    """Write the .debrief_setup_done marker so first_run reports false."""
    marker = config.VAULT_DIR / _SETUP_MARKER
    try:
        config.VAULT_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text("done\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not write setup marker: {exc}",
                "fix": _VAULT_UNWRITABLE_FIX,
            },
        )
    return JSONResponse({"ok": True})


@app.post("/api/setup/reset")
def api_setup_reset() -> JSONResponse:
    """Delete the setup marker (dev convenience: reopen the wizard)."""
    marker = config.VAULT_DIR / _SETUP_MARKER
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Static SPA at /
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC_DIR / "index.html"))


# Mount the rest of static/ (kept after the routes so /api wins).
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
