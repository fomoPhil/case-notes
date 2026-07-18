from __future__ import annotations

import base64
import json
import os
import platform
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from dateutil import parser as date_parser
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
VAULT = Path(os.getenv("CASE_NOTES_VAULT", ROOT / "demo_vault"))
MODE = os.getenv("CASE_NOTES_EXECUTION_MODE", "demo")
MODEL_BASE_URL = os.getenv("CASE_NOTES_MODEL_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
MODEL_ID = os.getenv("CASE_NOTES_MODEL_ID", "gemma4:12b-qat")

app = FastAPI(title="Debrief", docs_url=None, redoc_url=None)


class Request(BaseModel):
    mode: Literal["debrief", "voice_journal"]
    client_id: str
    transcript: str = Field(min_length=12, max_length=12000)


class Action(BaseModel):
    type: Literal["write_note", "schedule_followup", "draft_email"]
    summary: str
    datetime_utterance: str | None = None
    resolved_datetime: str | None = None
    duration_min: int | None = None
    subject: str | None = None


class Plan(BaseModel):
    mode: str
    client_id: str
    note_title: str
    note_markdown: str
    actions: list[Action]
    suggestions: list[str] = []
    sources: list[str] = []
    warnings: list[str] = []


class Approve(BaseModel):
    plan: Plan


def profile(client_id: str) -> dict[str, str]:
    profiles = {
        "C-0001": {"alias": "Bob", "framework": "CBT", "email": "bob@example.com"},
        "C-0002": {"alias": "Jordan", "framework": "ACT", "email": "jordan@example.com"},
    }
    if client_id not in profiles:
        raise HTTPException(404, "Unknown demo client")
    return profiles[client_id]


def resolve_datetime(utterance: str | None) -> str | None:
    if not utterance:
        return None
    # The model supplies the words it heard. Python, not the model, resolves dates.
    try:
        return date_parser.parse(utterance, fuzzy=True, default=datetime.now()).isoformat(timespec="minutes")
    except (OverflowError, TypeError, ValueError):
        return None


def fallback_plan(request: Request, client: dict[str, str]) -> Plan:
    title = "Voice journal" if request.mode == "voice_journal" else "Session debrief"
    note = f"# {title}: {client['alias']}\n\n## Source transcript\n{request.transcript}\n\n## Draft\nClinician review required before use.\n"
    return Plan(
        mode=request.mode,
        client_id=request.client_id,
        note_title=f"{datetime.now():%Y-%m-%d} {title}",
        note_markdown=note,
        actions=[Action(type="write_note", summary=f"Create {title.lower()} draft for {client['alias']}")],
        warnings=["Local model unavailable. This deterministic draft is for demo recovery only and must be reviewed."],
    )


async def model_plan(request: Request, client: dict[str, str]) -> Plan:
    system = """You are Debrief, a local documentation assistant for a licensed therapist. You are not a clinician and never diagnose, prescribe, sign notes, or send messages. Return JSON only. Ground every statement in the transcript. For voice_journal, create a dated draft observation or session-prep note and ONLY a write_note action. For debrief, create a DAP draft; optionally schedule_followup or draft_email only if explicitly requested. The therapist must review every draft."""
    schema = {
        "mode": request.mode,
        "client_id": request.client_id,
        "note_title": "string",
        "note_markdown": "string",
        "actions": [{"type": "write_note|schedule_followup|draft_email", "summary": "string", "datetime_utterance": "string|null", "duration_min": "integer|null", "subject": "string|null"}],
        "suggestions": ["string"], "sources": ["string"], "warnings": ["string"],
    }
    payload = {"model": MODEL_ID, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Client alias: {client['alias']}; framework: {client['framework']}.\nTranscript:\n{request.transcript}\n\nRequired JSON shape:\n{json.dumps(schema)}"},
    ]}
    try:
        async with httpx.AsyncClient(timeout=90) as http:
            response = await http.post(f"{MODEL_BASE_URL}/chat/completions", json=payload)
            response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
        plan = Plan.model_validate(data)
    except Exception:
        if MODE != "demo":
            raise HTTPException(503, "Local Gemma model is unavailable. Live actions are blocked.")
        return fallback_plan(request, client)
    plan.actions = [action for action in plan.actions if action.type == "write_note" or request.mode == "debrief"]
    if not any(action.type == "write_note" for action in plan.actions):
        plan.actions.insert(0, Action(type="write_note", summary="Create documentation draft"))
    for action in plan.actions:
        if action.type == "schedule_followup":
            action.resolved_datetime = resolve_datetime(action.datetime_utterance)
            if not action.resolved_datetime:
                plan.warnings.append("Follow-up date could not be resolved. It will not be scheduled.")
    return plan


def safe_filename(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._ -]+", "", title).strip().replace(" ", "-")[:90] or "draft"


def write_note(plan: Plan, client: dict[str, str]) -> Path:
    folder = "Voice-Journal" if plan.mode == "voice_journal" else "Sessions"
    destination = VAULT / "Clients" / plan.client_id / folder
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{safe_filename(plan.note_title)}.md"
    header = f"---\ntype: {'clinician-voice-journal' if plan.mode == 'voice_journal' else 'session-note-draft'}\nclient_id: {plan.client_id}\nclient_alias: {client['alias']}\nstatus: draft-review-required\ncreated_at: {datetime.now().isoformat(timespec='seconds')}\n---\n\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(header + plan.note_markdown, encoding="utf-8")
    temporary.replace(path)
    return path


def run_osascript(script: str) -> None:
    if MODE != "live" or platform.system() != "Darwin":
        return
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=30)


def create_calendar_event(client: dict[str, str], action: Action) -> None:
    if not action.resolved_datetime:
        return
    dt = date_parser.isoparse(action.resolved_datetime)
    date_string = dt.strftime("%A, %B %d, %Y %I:%M:%S %p")
    script = f'''tell application "Calendar"
activate
if not (exists calendar "Sessions") then make new calendar with properties {{name:"Sessions"}}
tell calendar "Sessions" to make new event with properties {{summary:"{client['alias']} follow-up", start date:date "{date_string}", duration:{(action.duration_min or 50) * 60}}}
end tell'''
    run_osascript(script)


def draft_email(client: dict[str, str], action: Action) -> None:
    subject = (action.subject or "Follow-up draft").replace('"', "")
    script = f'''tell application "Mail"
activate
set messageDraft to make new outgoing message with properties {{subject:"{subject}", content:"Draft prepared by Debrief. Clinician review required.", visible:true}}
tell messageDraft to make new to recipient with properties {{address:"{client['email']}"}}
end tell'''
    run_osascript(script)


async def verify_screen(expected: str) -> dict[str, str | bool]:
    if MODE != "live" or platform.system() != "Darwin":
        return {"confirmed": True, "what_i_see": "Demo mode: local artifact written. Live screen verification is disabled."}
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as screenshot:
        image_path = Path(screenshot.name)
    try:
        subprocess.run(["screencapture", "-x", str(image_path)], check=True, timeout=20)
        encoded = base64.b64encode(image_path.read_bytes()).decode()
        prompt = f"Read this live screenshot. Does it visibly confirm: {expected}? Return JSON only: {{\"confirmed\": true|false, \"what_i_see\": \"short factual description\"}}"
        payload = {"model": MODEL_ID, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]}]}
        async with httpx.AsyncClient(timeout=90) as http:
            response = await http.post(f"{MODEL_BASE_URL}/chat/completions", json=payload)
            response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception as exc:
        return {"confirmed": False, "what_i_see": f"Screen verification failed: {exc}"}
    finally:
        image_path.unlink(missing_ok=True)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status() -> dict[str, str | bool]:
    return {"mode": MODE, "model": MODEL_ID, "model_base_url": MODEL_BASE_URL, "live_actions_allowed": MODE == "live" and platform.system() == "Darwin"}


@app.post("/api/plan")
async def create_plan(request: Request) -> Plan:
    return await model_plan(request, profile(request.client_id))


@app.post("/api/approve")
async def approve(request: Approve) -> dict:
    plan = request.plan
    client = profile(plan.client_id)
    results = []
    note_path = write_note(plan, client)
    results.append({"action": "write_note", "ok": True, "artifact": str(note_path)})
    for action in plan.actions:
        try:
            if action.type == "schedule_followup" and action.resolved_datetime:
                create_calendar_event(client, action)
                results.append({"action": action.type, "ok": True})
            elif action.type == "draft_email":
                draft_email(client, action)
                results.append({"action": action.type, "ok": True})
        except Exception as exc:
            results.append({"action": action.type, "ok": False, "error": str(exc)})
    verification = await verify_screen(f"draft note for {client['alias']} titled {plan.note_title}")
    return {"results": results, "verification": verification, "review_required": True}
