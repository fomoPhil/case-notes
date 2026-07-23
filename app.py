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
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from debrief import (
    actions,
    agent,
    audit,
    classify,
    config,
    doctor,
    models,
    pipeline,
    render,
    stt,
    vault,
)

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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Make sure the vault + mock clients exist before the first request.
    try:
        vault.ensure_vault()
    except Exception:
        pass
    yield


app = FastAPI(title="Debrief", docs_url=None, redoc_url=None, lifespan=_lifespan)


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
        if c["name"] in ("Model server reachable", "Gemma model loaded") and not c["ok"]:
            raise HTTPException(
                status_code=503,
                detail={"error": c["detail"], "fix": c["fix"]},
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


@app.get("/api/clients")
def api_clients() -> JSONResponse:
    """Return the list of clients from the vault (profile frontmatter)."""
    try:
        clients = vault.list_clients()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read clients: {exc}")
    # Slim payload for the picker; keep the fields the UI shows.
    slim = [
        {
            "client_id": c.get("client_id"),
            "name": c.get("name"),
            "framework": c.get("framework"),
            "presenting_concerns": c.get("presenting_concerns", []),
            "risk_flags": c.get("risk_flags", []),
        }
        for c in clients
    ]
    return JSONResponse(slim)


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


@app.post("/api/debrief")
async def api_debrief(
    audio: UploadFile,
    client_id: str = Form(...),
) -> JSONResponse:
    """Transcribe + extract the uploaded debrief. Returns the plan, no execution."""
    _require_model_ready()
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    with tempfile.TemporaryDirectory(prefix="debrief_") as tmp:
        try:
            wav_path = _convert_to_wav(data, Path(tmp))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            plan = pipeline.transcribe_and_extract(wav_path, client_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Debrief failed: {exc}")
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
    verify = bool(plan.get("verify", True))
    # Resolve the stored original upload (never trust a client-sent path).
    plan.pop("audio_path", None)
    stored = _pop_original_audio(plan.get("audio_token"))
    if stored is not None:
        plan["audio_path"] = str(stored)
    try:
        result = pipeline.execute_plan(plan, verify=verify)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution failed: {exc}")
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


@app.post("/api/assistant/plan")
async def api_assistant_plan(request: Request) -> JSONResponse:
    """Transcribe (if audio) -> classify -> route to the debrief flow or agent."""
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
            with tempfile.TemporaryDirectory(prefix="debrief_asst_") as tmp:
                try:
                    wav_path = _convert_to_wav(data, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(exc))
                try:
                    transcript = stt.transcribe(wav_path)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
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

    routing = classify.classify(transcript, has_selected_client=bool(client_id))
    if routing["route"] == "session_debrief" and client_id:
        return JSONResponse({"route": "session_debrief", "transcript": transcript})

    hint = routing.get("client_hint") or client_id
    try:
        run = agent.run_agent(transcript, _dt.datetime.now(), client_hint=hint)
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
    _require_model_ready()
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    proposals = body.get("proposals") or []
    if not isinstance(proposals, list):
        raise HTTPException(status_code=400, detail="proposals must be a list.")
    req_client = _valid_client_id(body.get("client_id"))

    results: list[dict] = []
    last_worksheet_pdf: Path | None = None

    for proposal in proposals:
        if not isinstance(proposal, dict):
            results.append({"type": "?", "status": "failed", "error": "malformed proposal"})
            continue
        ptype = proposal.get("type")

        if ptype == "worksheet":
            res = _file_worksheet(proposal, req_client)
            if res.get("status") == "ok" and str(res.get("path", "")).endswith(".pdf"):
                last_worksheet_pdf = Path(res["path"])
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
            attachment = last_worksheet_pdf if proposal.get("attach_worksheet") else None
            try:
                ok = actions.create_mail_draft(email, subject, email_body, attachment)
            except Exception as exc:  # noqa: BLE001
                results.append({"type": "email", "status": "failed", "error": str(exc)})
                continue
            if ok:
                results.append(
                    {"type": "email", "status": "ok", "detail": f"Draft to {email} left open for review."}
                )
            else:
                results.append({"type": "email", "status": "failed", "error": "Mail returned an error"})
        else:
            results.append({"type": str(ptype), "status": "failed", "error": "unknown proposal type"})

    audit.log_assistant_run(
        {"request": body.get("request") or "", "results": results, "errors": []}
    )

    return JSONResponse({"results": results})


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
