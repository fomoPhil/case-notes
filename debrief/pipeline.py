"""End-to-end orchestration: one spoken debrief into filed note plus actions.

This is the integration layer that wires together the built modules:
transcribe -> correct -> client context -> extract (brain) ->
deterministic actions (hands: Calendar, Mail, vault) -> screen verify (eyes).

Design principle: deterministic hands, model brain, model eyes. Every stage is
wrapped so a single failure records an error and the pipeline keeps going where
it sensibly can. The approval-style plan (transcribe_and_extract) is separated
from execution (execute_plan) so the web UI can insert a human gate between the
two, while run_debrief runs the whole thing for the CLI / live test.

No em dashes in any generated copy (emails, calendar titles, summaries).
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import tempfile
import time
from pathlib import Path

# STT offline handling now lives in stt.py, which decides per engine whether to
# force HuggingFace offline (model already cached) or allow a one-time download
# (a not-yet-cached engine like whisper), then seals offline afterward. Setting
# the flags unconditionally here would block that deliberate first download.

from . import actions, audit, extract as extract_mod, llm, settings_store, stt, vault
from .config import DEFAULT_SESSION_MINUTES

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _first_name(full_name: str) -> str:
    """Return the first name only (calendars sync promiscuously; alias clients)."""
    full_name = (full_name or "").strip()
    return full_name.split()[0] if full_name else "Client"


def _parse_iso(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _naive_local(dt: _dt.datetime) -> _dt.datetime:
    """Strip tzinfo so osascript gets a naive local datetime (per actions.py)."""
    return dt.replace(tzinfo=None)


def _fmt_time(dt: _dt.datetime) -> str:
    """3:00 PM style, no leading zero on the hour."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _fmt_datetime_display(dt: _dt.datetime) -> str:
    """Tuesday, July 21, 2026 at 3:00 PM."""
    day = dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
    return f"{day} at {_fmt_time(dt)}"


def _calendar_title(first_name: str, dt: _dt.datetime) -> str:
    """Bob 3:00 PM session."""
    return f"{first_name} {_fmt_time(dt)} session"


def _short_iso(dt: _dt.datetime) -> str:
    """2026-07-21T15:00 for the actions_taken audit trail."""
    return dt.strftime("%Y-%m-%dT%H:%M")


def _worksheet_path() -> Path | None:
    """Locate the thought-record worksheet to attach (PDF, else markdown)."""
    base = vault.VAULT_DIR / "Templates" / "Worksheets" / "thought-record"
    for suffix in (".pdf", ".md"):
        candidate = base.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def _clean_attachment_name(raw: str | None) -> str:
    """Human-readable worksheet name for the email body."""
    name = (raw or "thought record worksheet").strip()
    # Normalise a few common phrasings; keep it warm and plain.
    if "worksheet" not in name.lower():
        name = f"{name} worksheet"
    return name


def _compose_client_email(
    first_name: str,
    appointment_display: str | None,
    attachment_name: str | None,
) -> tuple[str, str]:
    """Build a warm, fixed-template client email. NOT an LLM call.

    No clinical content beyond the appointment time and the worksheet name.
    Returns (subject, body). No em dashes.
    """
    subject = "Your next appointment"
    lines = [f"Hi {first_name},", ""]
    opening = "Thank you for coming in today."
    if appointment_display:
        opening += f" This is a quick note to confirm our next session on {appointment_display}."
    else:
        opening += " This is a quick note to follow up after today's session."
    lines.append(opening)

    if attachment_name:
        lines.append(
            f"I have attached the {attachment_name} for you to look over before we meet."
        )
    lines.append("Please reply here if anything comes up in the meantime.")
    lines.append("")
    lines.append("Warm regards,")
    lines.append("Your therapist")
    return subject, "\n".join(lines)


def _convert_to_m4a(src: Path, dst: Path) -> bool:
    """Convert any dictation audio to AAC m4a. afconvert first (native, fast
    for wav/aiff/caf), ffmpeg fallback (handles webm/opus from MediaRecorder).
    Returns True when dst exists.
    """
    try:
        proc = subprocess.run(
            ["afconvert", str(src), str(dst), "-d", "aac", "-f", "m4af"],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode == 0 and dst.exists():
            return True
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c:a", "aac", "-b:a", "96k", str(dst)],
            capture_output=True,
            timeout=120,
        )
        return proc.returncode == 0 and dst.exists()
    except (subprocess.SubprocessError, OSError):
        return False


def _archive_audio(note_path: Path, tmp_m4a: Path) -> Path:
    """Move a converted m4a into Sessions/audio/<note-stem>.m4a atomically.

    The temp file may live on another volume, so stage it inside the target
    directory first, then os.replace into place.
    """
    import shutil

    audio_dir = note_path.parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    final = audio_dir / f"{note_path.stem}.m4a"
    staging = audio_dir / f".{note_path.stem}.m4a.tmp"
    shutil.move(str(tmp_m4a), str(staging))
    os.replace(staging, final)
    return final


def _followup_key(action: dict) -> str:
    """A comparison key for a schedule_followup: prefer the resolved slot, else
    the normalised spoken phrase. Used to catch the model emitting the same
    booking twice (an attendee-detail clause promoted to a second action)."""
    resolved = action.get("resolved_datetime")
    if resolved:
        return f"dt::{resolved}"
    utter = (action.get("datetime_utterance") or "").strip().lower()
    utter = " ".join(utter.split())
    return f"utter::{utter}"


def _dedup_followups(plan_actions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop duplicate schedule_followup actions (same resolved slot or phrase).

    Keeps the first occurrence, returns (kept_actions, dropped_actions). Never
    book the same slot twice. Non-followup actions pass through untouched.
    """
    seen: set[str] = set()
    kept: list[dict] = []
    dropped: list[dict] = []
    for a in plan_actions:
        if a.get("type") != "schedule_followup":
            kept.append(a)
            continue
        key = _followup_key(a)
        # A followup with no resolvable time and no phrase is not a real dup key;
        # let it through rather than collapse unrelated unresolved bookings.
        if key in ("utter::",):
            kept.append(a)
            continue
        if key in seen:
            dropped.append(a)
            continue
        seen.add(key)
        kept.append(a)
    return kept, dropped


def _condense_summary(existing_summary: str, note: dict) -> str:
    """One small model call: fold this session's data + assessment into a fresh
    running summary paragraph, capped at 120 words. Falls back to the existing
    summary on any failure so the profile is never blanked.
    """
    data = (note.get("data") or "").strip()
    assessment = (note.get("assessment") or "").strip()
    system = (
        "You maintain a therapist's running client summary. Rewrite it as one "
        "cohesive paragraph in professional clinical prose, third person, at most "
        "120 words. Preserve durable context (presenting concerns, framework, "
        "risk history) and fold in what this session adds. Do not use an em dash. "
        "Return only the paragraph, no preamble."
    )
    user = (
        f"CURRENT RUNNING SUMMARY:\n{existing_summary.strip()}\n\n"
        f"THIS SESSION DATA:\n{data}\n\n"
        f"THIS SESSION ASSESSMENT:\n{assessment}\n\n"
        "Produce the updated running summary paragraph now."
    )
    try:
        out = llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=260,
            temperature=0.3,
        )
        if isinstance(out, str) and out.strip():
            return out.strip().replace("—", ", ")
    except Exception:
        pass
    return existing_summary.strip()


# ---------------------------------------------------------------------------
# Phase 1: transcribe + extract (produces the approval plan, no side effects)
# ---------------------------------------------------------------------------


def transcribe_and_extract(wav_path: str, client_id: str) -> dict:
    """Run the read-only half of the pipeline and return a JSON-serialisable plan.

    Stages: transcribe -> correct -> client_context -> extract (+date resolve).
    Never touches Calendar, Mail, or the vault. The web UI shows this plan for
    approval; execute_plan then runs the approved actions.
    """
    timings: dict[str, float] = {}
    errors: list[dict] = []
    now = _dt.datetime.now()

    # Read persistent settings once for this request (profession + dictionary
    # feed the correction pass; not once per correction layer).
    settings = settings_store.load()
    profession = settings.get("profession", "therapy")
    note_format = settings.get("note_format", "DAP")
    stt_engine = settings.get("stt_engine", "parakeet")
    features = settings.get("features", {}) or {}
    calendar_on = features.get("calendar", True)
    email_on = features.get("email", True)
    dictionary = settings_store.read_dictionary()

    # --- transcribe --------------------------------------------------------
    transcript = ""
    t0 = time.perf_counter()
    try:
        transcript = stt.transcribe(wav_path, engine_id=stt_engine)
    except Exception as exc:
        errors.append({"stage": "transcribe", "error": str(exc)})
    timings["transcribe"] = round(time.perf_counter() - t0, 2)

    # --- client context ----------------------------------------------------
    # Fetched before correction so the glossary pass can bias ambiguous words
    # toward this client's known name, diagnoses, medications, and framework.
    client_ctx: dict = {}
    t0 = time.perf_counter()
    try:
        client_ctx = vault.client_context(client_id)
    except Exception as exc:
        errors.append({"stage": "context", "error": str(exc)})
    timings["context"] = round(time.perf_counter() - t0, 2)

    framework = client_ctx.get("framework") or "CBT"
    client_name = client_ctx.get("name") or client_id
    first_name = _first_name(client_name)

    # --- glossary correction ----------------------------------------------
    corrected = transcript
    t0 = time.perf_counter()
    try:
        corrected = stt.correct_transcript(
            transcript, client_ctx, framework, profession=profession, dictionary=dictionary
        )
    except Exception as exc:
        errors.append({"stage": "correct", "error": str(exc)})
        corrected = transcript
    timings["correct"] = round(time.perf_counter() - t0, 2)

    # --- extraction (brain) ------------------------------------------------
    # client_context() nests a full "profile" frontmatter dict that duplicates
    # the spread keys and carries date objects; the extractor json.dumps dict
    # values, so hand it a flat, JSON-safe context instead.
    extract_ctx = {k: v for k, v in client_ctx.items() if k != "profile"}
    extraction: dict = {}
    t0 = time.perf_counter()
    try:
        extraction = extract_mod.extract(
            corrected, extract_ctx, framework, now,
            format_id=note_format, profession=profession, features=features,
        )
    except Exception as exc:
        errors.append({"stage": "extract", "error": str(exc)})
        extraction = {
            "note": {},
            "actions": [],
            "unsupported_requests": [],
            "next_session_suggestions": [],
        }
    timings["extract"] = round(time.perf_counter() - t0, 2)

    note = extraction.get("note", {}) or {}
    raw_actions = extraction.get("actions", []) or []

    # --- normalise actions for the UI / executor --------------------------
    plan_actions: list[dict] = []
    for a in raw_actions:
        atype = a.get("type")
        # Feature toggles: never surface an action type the user turned off, even
        # if the model emitted one against the (stable) schema enum.
        if atype == "schedule_followup" and not calendar_on:
            continue
        if atype == "draft_client_email" and not email_on:
            continue
        if atype == "schedule_followup":
            resolved = a.get("resolved_datetime")
            dt = _parse_iso(resolved)
            duration = a.get("duration_min") or DEFAULT_SESSION_MINUTES
            display = _fmt_datetime_display(dt) if dt else None
            title = _calendar_title(first_name, dt) if dt else None
            label = (
                f"Book follow-up: {display} ({duration} min)"
                if display
                else "Book follow-up (could not resolve the spoken date)"
            )
            plan_actions.append(
                {
                    "type": "schedule_followup",
                    "datetime_utterance": a.get("datetime_utterance"),
                    "resolved_datetime": dt.isoformat() if dt else None,
                    "datetime_display": display,
                    "duration_min": duration,
                    "title": title,
                    "label": label,
                    "enabled": True,
                }
            )
        elif atype == "draft_client_email":
            attachment = a.get("attachment")
            att_name = _clean_attachment_name(attachment) if attachment else None
            label = "Draft confirmation email"
            if att_name:
                label += f" with the {att_name}"
            plan_actions.append(
                {
                    "type": "draft_client_email",
                    "purpose": a.get("purpose"),
                    "attachment": attachment,
                    "attachment_name": att_name,
                    "label": label,
                    "enabled": True,
                }
            )

    # Safety net: the model occasionally emits the same booking twice. Drop the
    # duplicates deterministically so we never create two events for one slot.
    plan_actions, deduped_actions = _dedup_followups(plan_actions)

    # Session metadata for the note write (session_number = existing files + 1).
    # The active format spec also travels in the plan so the review UI can render
    # section-by-section, and the feature toggles ride along for the same UI.
    session_number = _next_session_number(client_id)
    from . import formats

    spec = formats.get_spec_or_default(note_format)
    session_meta = {
        "session_date": now.date().isoformat(),
        "session_number": session_number,
        "format": spec["id"],
        "modality": "in-person",
        "duration_min": DEFAULT_SESSION_MINUTES,
        "framework": framework,
        "sections": spec["sections"],
        "features": settings.get("features", {}),
    }

    return {
        "client_id": client_id,
        "client": {
            "name": client_name,
            "first_name": first_name,
            "email": client_ctx.get("email"),
            "framework": framework,
        },
        "transcript": transcript,
        "corrected_transcript": corrected,
        "note": note,
        "extraction": extraction,
        "actions": plan_actions,
        "deduped_actions": deduped_actions,
        "unsupported_requests": extraction.get("unsupported_requests", []) or [],
        "next_session_suggestions": extraction.get("next_session_suggestions", []) or [],
        "session_meta": session_meta,
        "timings": timings,
        "errors": errors,
    }


def _next_session_number(client_id: str) -> int:
    """Count existing session note files for the client and add one."""
    try:
        sessions = vault.VAULT_DIR / "Clients" / client_id / "Sessions"
        if sessions.exists():
            return len(list(sessions.glob("*.md"))) + 1
    except Exception:
        pass
    return 1


# ---------------------------------------------------------------------------
# Phase 2: execute the approved plan (actions + note + verify)
# ---------------------------------------------------------------------------


def execute_plan(plan: dict, verify: bool = True) -> dict:
    """Run the approved plan: Calendar, Mail, vault write, profile update, verify.

    Only actions with enabled != False are executed (the UI drops or unchecks
    the rest). Every action is wrapped: a failure records status "failed" and
    an error, and the remaining actions still run. Returns action statuses, the
    note path, verification results, and timings.
    """
    client_id = plan.get("client_id")
    client = plan.get("client", {}) or {}
    first_name = client.get("first_name") or _first_name(client.get("name", ""))
    email = client.get("email")
    corrected = plan.get("corrected_transcript") or plan.get("transcript") or ""
    note = plan.get("note", {}) or {}
    session_meta = dict(plan.get("session_meta", {}) or {})
    plan_actions = plan.get("actions", []) or []

    timings: dict[str, float] = {}
    errors: list[dict] = list(plan.get("errors", []) or [])

    # Feature toggles are read at execute time (settings can change between the
    # plan and the approval), so a disabled action type is skipped defensively
    # even if it rode along in the plan.
    exec_features = settings_store.load().get("features", {}) or {}
    exec_calendar_on = exec_features.get("calendar", True)
    exec_email_on = exec_features.get("email", True)

    result_actions: list[dict] = []
    deduped_actions: list[dict] = list(plan.get("deduped_actions", []) or [])
    actions_taken: list[str] = []
    booked_dt: _dt.datetime | None = None
    booked_keys: set[str] = set()
    email_drafted = False

    # --- run the requested admin actions ----------------------------------
    t0 = time.perf_counter()
    for a in plan_actions:
        entry = dict(a)
        if a.get("enabled") is False:
            entry["status"] = "skipped"
            entry["detail"] = "Unchecked by the therapist."
            result_actions.append(entry)
            continue

        atype = a.get("type")
        if (atype == "schedule_followup" and not exec_calendar_on) or (
            atype == "draft_client_email" and not exec_email_on
        ):
            entry["status"] = "skipped"
            entry["detail"] = "disabled in settings"
            result_actions.append(entry)
            continue

        if atype == "schedule_followup":
            dt = _parse_iso(a.get("resolved_datetime"))
            if dt is None:
                entry["status"] = "skipped"
                entry["detail"] = "No resolvable date in the spoken request."
                result_actions.append(entry)
                continue
            # Defensive: never book the same resolved slot twice in one run.
            slot_key = dt.isoformat()
            if slot_key in booked_keys:
                entry["status"] = "skipped"
                entry["detail"] = "Duplicate of an already booked slot."
                deduped_actions.append(entry)
                result_actions.append(entry)
                continue
            booked_keys.add(slot_key)
            duration = int(a.get("duration_min") or DEFAULT_SESSION_MINUTES)
            title = a.get("title") or _calendar_title(first_name, dt)
            try:
                ok = actions.create_calendar_event(title, _naive_local(dt), duration)
                if ok:
                    actions.open_calendar_at(_naive_local(dt))
                    booked_dt = dt
                    actions_taken.append(f"followup-booked-{_short_iso(dt)}")
                    entry["status"] = "ok"
                    entry["detail"] = f"Created '{title}' on {_fmt_datetime_display(dt)}."
                else:
                    entry["status"] = "failed"
                    entry["detail"] = "Calendar returned an error."
                    errors.append({"stage": "calendar", "error": "create returned False"})
            except Exception as exc:
                entry["status"] = "failed"
                entry["detail"] = str(exc)
                errors.append({"stage": "calendar", "error": str(exc)})
            entry["title"] = title
            result_actions.append(entry)

        elif atype == "draft_client_email":
            attachment_name = a.get("attachment_name") or (
                _clean_attachment_name(a.get("attachment")) if a.get("attachment") else None
            )
            appt_display = _fmt_datetime_display(booked_dt) if booked_dt else None
            subject, body = _compose_client_email(first_name, appt_display, attachment_name)
            worksheet = _worksheet_path() if a.get("attachment") else None
            try:
                if not email:
                    entry["status"] = "failed"
                    entry["detail"] = "No client email address on file."
                    errors.append({"stage": "mail", "error": "no email address"})
                else:
                    ok = actions.create_mail_draft(email, subject, body, worksheet)
                    if ok:
                        email_drafted = True
                        actions_taken.append("email-drafted")
                        att = f" with {worksheet.name}" if worksheet else ""
                        entry["status"] = "ok"
                        entry["detail"] = f"Draft to {email}{att} left open for review."
                    else:
                        entry["status"] = "failed"
                        entry["detail"] = "Mail returned an error."
                        errors.append({"stage": "mail", "error": "draft returned False"})
            except Exception as exc:
                entry["status"] = "failed"
                entry["detail"] = str(exc)
                errors.append({"stage": "mail", "error": str(exc)})
            entry["subject"] = subject
            result_actions.append(entry)
        else:
            entry["status"] = "skipped"
            entry["detail"] = f"Unknown action type {atype!r}."
            result_actions.append(entry)
    timings["actions"] = round(time.perf_counter() - t0, 2)

    # --- archive the dictation audio (convert first; reference only works) --
    # Convert BEFORE the note is written so the note never links a file that
    # failed to convert. The move happens after the note reserves its stem.
    tmp_m4a: Path | None = None
    audio_src = plan.get("audio_path")
    t0 = time.perf_counter()
    if audio_src and Path(audio_src).exists():
        try:
            fd, tmp_name = tempfile.mkstemp(suffix=".m4a")
            os.close(fd)
            tmp_m4a = Path(tmp_name)
            if not _convert_to_m4a(Path(audio_src), tmp_m4a):
                tmp_m4a.unlink(missing_ok=True)
                tmp_m4a = None
                errors.append({"stage": "audio", "error": "conversion to m4a failed"})
        except Exception as exc:
            tmp_m4a = None
            errors.append({"stage": "audio", "error": str(exc)})
    timings["audio_convert"] = round(time.perf_counter() - t0, 2)

    # --- write the session note (note-filed leads the audit trail) --------
    note_path: str | None = None
    audio_archive_path: str | None = None
    t0 = time.perf_counter()
    try:
        meta = dict(session_meta)
        meta["actions_taken"] = ["note-filed"] + actions_taken
        meta["next_session_suggestions"] = plan.get("next_session_suggestions", []) or []
        if tmp_m4a is not None:
            meta["audio_filename"] = True  # vault normalizes to the note stem
        path = vault.write_session_note(client_id, note, corrected, meta)
        note_path = str(path)
        if tmp_m4a is not None:
            try:
                audio_archive_path = str(_archive_audio(path, tmp_m4a))
                tmp_m4a = None
            except Exception as exc:
                errors.append({"stage": "audio", "error": f"archive move failed: {exc}"})
    except Exception as exc:
        errors.append({"stage": "note", "error": str(exc)})
    finally:
        if tmp_m4a is not None:
            tmp_m4a.unlink(missing_ok=True)
    timings["note"] = round(time.perf_counter() - t0, 2)

    # --- open the note in Obsidian ----------------------------------------
    obsidian_uri = None
    if note_path:
        t0 = time.perf_counter()
        try:
            obsidian_uri = vault.obsidian_open_uri(Path(note_path))
        except Exception as exc:
            errors.append({"stage": "obsidian", "error": str(exc)})
        timings["obsidian"] = round(time.perf_counter() - t0, 2)

    # --- update the running profile summary -------------------------------
    t0 = time.perf_counter()
    try:
        ctx = vault.client_context(client_id)
        new_summary = _condense_summary(ctx.get("summary", ""), note)
        updates: dict = {
            "last_session": _dt.date.fromisoformat(session_meta["session_date"]),
            "summary_updated": _dt.datetime.now(),
        }
        if booked_dt is not None:
            updates["next_session"] = _naive_local(booked_dt)
        vault.update_profile(client_id, new_summary, updates)
    except Exception as exc:
        errors.append({"stage": "profile", "error": str(exc)})
    timings["profile"] = round(time.perf_counter() - t0, 2)

    # --- verify on screen (eyes) ------------------------------------------
    verification: list[dict] = []
    if verify:
        # Settle the visual surfaces before the model reads them. A freshly
        # written note can miss Obsidian's first open while it indexes, so
        # re-open it; re-center Calendar on the booked week for the same reason.
        if note_path:
            try:
                vault.obsidian_open_uri(Path(note_path))
            except Exception:
                pass
        if booked_dt is not None:
            try:
                actions.open_calendar_at(_naive_local(booked_dt))
            except Exception:
                pass
        time.sleep(2.5)

        checks: list[dict] = []
        if booked_dt is not None:
            date_label = booked_dt.strftime("%A %B %d").replace(" 0", " ")
            checks.append(
                {
                    "surface": "calendar",
                    "question": (
                        "This is the macOS Calendar app. Is there a calendar event for "
                        f"'{first_name}' (a {_fmt_time(booked_dt)} session) visible on "
                        f"{date_label}? Answer strictly from what is on screen."
                    ),
                }
            )
        if note_path and vault.obsidian_available():
            # Only verify the note surface when Obsidian can actually show it
            # (unregistered vaults would leave nothing on screen to read).
            # Ask about the ACTIVE format's own headings, not a hardcoded DAP
            # trio, so a correctly filed SOAP or GROW note is not failed for
            # lacking Data/Assessment/Plan.
            sections = session_meta.get("sections") or []
            headings = [s.get("heading") for s in sections if s.get("heading")][:3]
            if headings:
                heading_phrase = ", ".join(headings)
                note_question = (
                    "This is the Obsidian note editor. Is a session note open "
                    f"showing headings such as {heading_phrase}? Answer strictly "
                    "from what is on screen."
                )
            else:
                note_question = (
                    "This is the Obsidian note editor. Is a clinical session note "
                    "open showing DAP headings such as Data, Assessment, and Plan? "
                    "Answer strictly from what is on screen."
                )
            checks.append({"surface": "obsidian", "question": note_question})
        if email_drafted:
            checks.append(
                {
                    "surface": "mail",
                    "question": (
                        "This is the macOS Mail app. Is there an email draft window open "
                        "with a subject about a next appointment? Answer strictly from "
                        "what is on screen."
                    ),
                }
            )
        t0 = time.perf_counter()
        try:
            from . import verify as verify_mod

            verification = verify_mod.verify_on_screen(checks)
        except Exception as exc:
            errors.append({"stage": "verify", "error": str(exc)})
            verification = [
                {**c, "confirmed": False, "what_i_see": f"Verification error: {exc}"}
                for c in checks
            ]
        timings["verify"] = round(time.perf_counter() - t0, 2)

    result = {
        "actions": result_actions,
        "deduped_actions": deduped_actions,
        "actions_taken": ["note-filed"] + actions_taken,
        "note_path": note_path,
        "audio_archive_path": audio_archive_path,
        "obsidian_uri": obsidian_uri,
        "verification": verification,
        "timings": timings,
        "errors": errors,
    }

    # --- activity log (failure-safe; a logging failure never breaks the run) --
    # This is the single place both the HTTP path (/api/execute) and the CLI
    # path (run_debrief) flow through, so every executed debrief is logged once.
    # The plan carries the phase-1 context (client, transcript, phase-1 timings,
    # unsupported requests) that the returned result does not, so merge them for
    # the log. errors is the same list object held by result, so an audit
    # failure recorded here also surfaces in result["errors"].
    audit.log_debrief_run(
        {
            "client_id": client_id,
            "client": client,
            "corrected_transcript": corrected,
            "note_path": note_path,
            "actions": result_actions,
            "deduped_actions": deduped_actions,
            "verification": verification,
            "timings": {**(plan.get("timings") or {}), **timings},
            "unsupported_requests": plan.get("unsupported_requests", []) or [],
            "errors": errors,
        }
    )

    return result


# ---------------------------------------------------------------------------
# Full pipeline (CLI / live test)
# ---------------------------------------------------------------------------


def run_debrief(
    wav_path: str,
    client_id: str,
    execute: bool = True,
    verify: bool = True,
) -> dict:
    """Run the whole debrief: transcribe -> correct -> extract -> (execute) -> (verify).

    Returns one merged dict with the transcript, corrected transcript, full
    extraction, per-action status, note path, verification results, per-stage
    timings, and an errors list. Every stage is wrapped so one failure does not
    kill the rest.
    """
    plan = transcribe_and_extract(wav_path, client_id)

    merged: dict = {
        "client_id": client_id,
        "client": plan["client"],
        "transcript": plan["transcript"],
        "corrected_transcript": plan["corrected_transcript"],
        "note": plan["note"],
        "extraction": plan["extraction"],
        "next_session_suggestions": plan["next_session_suggestions"],
        "unsupported_requests": plan["unsupported_requests"],
        "actions": plan["actions"],
        "deduped_actions": plan.get("deduped_actions", []),
        "note_path": None,
        "verification": [],
        "timings": dict(plan["timings"]),
        "errors": list(plan["errors"]),
    }

    if not execute:
        return merged

    # Carry the source audio through so the dictation gets archived in the vault.
    plan["audio_path"] = wav_path

    exec_result = execute_plan(plan, verify=verify)
    merged["actions"] = exec_result["actions"]
    merged["deduped_actions"] = exec_result["deduped_actions"]
    merged["actions_taken"] = exec_result["actions_taken"]
    merged["note_path"] = exec_result["note_path"]
    merged["audio_archive_path"] = exec_result.get("audio_archive_path")
    merged["obsidian_uri"] = exec_result.get("obsidian_uri")
    merged["verification"] = exec_result["verification"]
    merged["timings"].update(exec_result["timings"])
    merged["errors"].extend(
        e for e in exec_result["errors"] if e not in merged["errors"]
    )
    return merged
