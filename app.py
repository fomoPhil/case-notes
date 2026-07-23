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
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
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
    records,
    render,
    stt,
    vault,
    verify,
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


def _debrief_sync(data: bytes, client_id: str) -> dict:
    """Blocking half of /api/debrief; runs in a worker thread."""
    with tempfile.TemporaryDirectory(prefix="debrief_") as tmp:
        try:
            wav_path = _convert_to_wav(data, Path(tmp))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            return pipeline.transcribe_and_extract(wav_path, client_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Debrief failed: {exc}")


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
    plan = await run_in_threadpool(_debrief_sync, data, client_id)
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
        result = await run_in_threadpool(pipeline.execute_plan, plan, verify=verify)
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
            raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")


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
        raise HTTPException(status_code=500, detail=f"Could not read client: {exc}")
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
    """Reveal a file in Finder (open -R). Markdown reveals its rendered PDF."""
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
        raise HTTPException(status_code=500, detail=f"Could not write setup marker: {exc}")
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
