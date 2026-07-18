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

import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from debrief import pipeline, vault

APP_HOST = "127.0.0.1"
APP_PORT = 8377

_STATIC_DIR = Path(__file__).resolve().parent / "static"


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
# API
# ---------------------------------------------------------------------------


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
    return JSONResponse(plan)


@app.post("/api/execute")
async def api_execute(request: Request) -> JSONResponse:
    """Execute an approved plan: actions + note write + verification."""
    try:
        plan = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid plan JSON: {exc}")
    if not isinstance(plan, dict) or not plan.get("client_id"):
        raise HTTPException(status_code=400, detail="Plan is missing client_id.")
    verify = bool(plan.get("verify", True))
    try:
        result = pipeline.execute_plan(plan, verify=verify)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution failed: {exc}")
    return JSONResponse(result)


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
