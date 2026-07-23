"""Agent activity log: a human-readable record of actions the app actually took.

Every executed debrief appends one markdown entry to
DebriefVault/_Activity/YYYY-MM-DD.md. The log is written for the therapist to
read (and annotate) inside Obsidian: what was heard, what note was filed, what
was booked, what email was drafted, what the vision check saw, and how long each
stage took. It records ACTIONS TAKEN, so an un-executed plan is never logged.

Design principle: logging is a side effect, never a dependency. A failure here
must never break the pipeline, so log_debrief_run swallows its own exceptions
and records them in result["errors"] as an "audit" stage entry.

No em dashes anywhere (a final sweep replaces any the model produced).
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from .config import VAULT_DIR

_ACTIVITY_DIRNAME = "_Activity"
_WHAT_I_SEE_MAX = 120


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _no_em_dash(text: str) -> str:
    """Strip em dashes (and the longer horizontal bar) from any string."""
    return text.replace("—", "-").replace("―", "-")


def _fmt_time(dt: _dt.datetime) -> str:
    """3:47 PM style, no leading zero on the hour."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _truncate(text: str, limit: int = _WHAT_I_SEE_MAX) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _client_wikilink(client_id: str, name: str) -> str:
    """[[Clients/C-0001/_Profile|Bob Smith]]."""
    label = (name or client_id or "Client").strip()
    return f"[[Clients/{client_id}/_Profile|{label}]]"


def _note_wikilink(note_path: str | None) -> str | None:
    """Wikilink to the session note relative to the vault, extension dropped."""
    if not note_path:
        return None
    try:
        rel = Path(note_path).relative_to(VAULT_DIR)
    except ValueError:
        rel = Path(note_path)
    target = str(rel.with_suffix(""))
    return f"[[{target}|{rel.stem}]]"


def _booked_line(actions: list[dict]) -> str:
    """Human datetime of the booked follow-up, else a plain status phrase."""
    followups = [a for a in actions if a.get("type") == "schedule_followup"]
    booked = [a for a in followups if a.get("status") == "ok"]
    if booked:
        a = booked[0]
        return a.get("datetime_display") or a.get("resolved_datetime") or "booked"
    if followups:
        return f"requested, not booked ({followups[0].get('status', 'skipped')})"
    return "none requested"


def _email_line(actions: list[dict]) -> str:
    emails = [a for a in actions if a.get("type") == "draft_client_email"]
    if any(a.get("status") == "ok" for a in emails):
        return "drafted"
    return "none"


def _timing_line(timings: dict) -> str | None:
    if not timings:
        return None
    parts = [f"{stage} {value}s" for stage, value in timings.items()]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def _build_entry(result: dict, now: _dt.datetime) -> str:
    """Render one activity-log entry (heading + compact bullets) as markdown."""
    client_id = result.get("client_id") or "?"
    client = result.get("client", {}) or {}
    name = client.get("name") or client_id
    actions = result.get("actions", []) or []

    transcript = result.get("corrected_transcript") or result.get("transcript") or ""
    word_count = len(transcript.split())

    lines: list[str] = []
    lines.append(f"### {_fmt_time(now)} · {_client_wikilink(client_id, name)}")

    # Heard: word count, plus dictation duration when a value is supplied.
    heard = f"{word_count} words"
    duration = result.get("audio_duration_sec")
    if duration:
        try:
            heard += f", {round(float(duration))} s"
        except (TypeError, ValueError):
            pass
    lines.append(f"- Heard: {heard}")

    note_link = _note_wikilink(result.get("note_path"))
    if note_link:
        lines.append(f"- Note: {note_link}")

    lines.append(f"- Booked: {_booked_line(actions)}")
    lines.append(f"- Email: {_email_line(actions)}")

    verification = result.get("verification", []) or []
    if verification:
        lines.append("- Verified:")
        for v in verification:
            mark = "✓" if v.get("confirmed") else "✗"
            surface = v.get("surface", "?")
            what = _truncate(v.get("what_i_see", ""))
            detail = f" *{what}*" if what else ""
            lines.append(f"    - {surface}: {mark}{detail}")

    timing = _timing_line(result.get("timings", {}) or {})
    if timing:
        lines.append(f"- Timing: {timing}")

    deduped = result.get("deduped_actions") or []
    if deduped:
        noun = "action" if len(deduped) == 1 else "actions"
        lines.append(f"- Deduped: {len(deduped)} duplicate {noun} dropped")

    unsupported = result.get("unsupported_requests") or []
    if unsupported:
        lines.append("- Unsupported requests:")
        for u in unsupported:
            lines.append(f"    - {str(u).strip()}")

    errors = result.get("errors") or []
    if errors:
        lines.append("- Errors:")
        for e in errors:
            if isinstance(e, dict):
                lines.append(f"    - {e.get('stage', '?')}: {e.get('error', '')}")
            else:
                lines.append(f"    - {e}")

    return _no_em_dash("\n".join(lines) + "\n")


def _header(today: str) -> str:
    """Frontmatter + a small heading, written only when the day file is new."""
    return (
        "---\n"
        "type: activity-log\n"
        f"date: {today}\n"
        "---\n\n"
        f"# Activity Log {today}\n\n"
        "Written automatically by the Debrief app after each executed debrief.\n\n"
    )


def _atomic_append(path: Path, content: str) -> None:
    """Write content to path atomically (tmp file in same dir + os.replace).

    Uses the builtin open so a monkeypatched open surfaces as a failure the
    caller can catch and record, matching the failure-safe contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _build_assistant_entry(run: dict, now: _dt.datetime) -> str:
    """Render one activity-log entry for an approved assistant run."""
    request = _truncate(run.get("request") or "", 160)
    results = run.get("results", []) or []

    lines: list[str] = []
    lines.append(f"### {_fmt_time(now)} · Assistant")
    if request:
        lines.append(f"- Request: {request}")
    if not results:
        lines.append("- Filed: nothing (no proposals approved)")
    for r in results:
        rtype = r.get("type", "item")
        status = r.get("status", "?")
        if status == "ok" and r.get("path"):
            try:
                rel = Path(r["path"]).relative_to(VAULT_DIR)
                target = f"[[{str(rel.with_suffix(''))}|{rel.stem}]]"
            except (ValueError, TypeError):
                target = str(r.get("path"))
            lines.append(f"- {rtype.capitalize()}: {target}")
        elif status == "ok":
            lines.append(f"- {rtype.capitalize()}: {r.get('detail', 'done')}")
        else:
            lines.append(f"- {rtype.capitalize()}: {status} ({r.get('error', '')})")

    return _no_em_dash("\n".join(lines) + "\n")


def log_assistant_run(run: dict) -> Path | None:
    """Append one activity-log entry for an approved assistant execution.

    Failure-safe, mirroring log_debrief_run: a logging failure is recorded in
    run["errors"] and never raised.
    """
    try:
        now = _dt.datetime.now()
        today = now.date().isoformat()
        day_file = VAULT_DIR / _ACTIVITY_DIRNAME / f"{today}.md"

        entry = _build_assistant_entry(run, now)
        if day_file.exists():
            existing = day_file.read_text(encoding="utf-8").rstrip("\n")
            content = f"{existing}\n\n{entry}"
        else:
            content = _header(today) + entry

        _atomic_append(day_file, content)
        return day_file
    except Exception as exc:  # noqa: BLE001 - logging must never break the run
        try:
            run.setdefault("errors", []).append({"stage": "audit", "error": str(exc)})
        except Exception:
            pass
        return None


def log_records_change(action: str, path: str, detail: str = "") -> Path | None:
    """Append one activity-log entry for a records-UI change. Failure-safe.

    action: a short verb phrase (e.g. "Renamed", "Amended", "Uploaded",
            "Moved to trash", "Restored"). path: the vault-relative path
            affected. detail: an optional human note.
    """
    try:
        now = _dt.datetime.now()
        today = now.date().isoformat()
        day_file = VAULT_DIR / _ACTIVITY_DIRNAME / f"{today}.md"

        try:
            rel = Path(path).relative_to(VAULT_DIR)
        except ValueError:
            rel = Path(path)
        target = f"[[{str(rel.with_suffix(''))}|{rel.stem}]]" if str(rel) else "?"

        lines = [f"### {_fmt_time(now)} · {action}"]
        lines.append(f"- File: {target}")
        if detail:
            lines.append(f"- Detail: {_truncate(detail, 160)}")
        entry = _no_em_dash("\n".join(lines) + "\n")

        if day_file.exists():
            existing = day_file.read_text(encoding="utf-8").rstrip("\n")
            content = f"{existing}\n\n{entry}"
        else:
            content = _header(today) + entry
        _atomic_append(day_file, content)
        return day_file
    except Exception:  # noqa: BLE001 - logging must never break the request
        return None


def log_debrief_run(result: dict) -> Path | None:
    """Append one activity-log entry for an executed debrief. Failure-safe.

    Returns the day-file path on success, or None on failure (a logging failure
    is recorded in result["errors"] as an "audit" stage entry and never raised).
    """
    try:
        now = _dt.datetime.now()
        today = now.date().isoformat()
        day_file = VAULT_DIR / _ACTIVITY_DIRNAME / f"{today}.md"

        entry = _build_entry(result, now)
        if day_file.exists():
            existing = day_file.read_text(encoding="utf-8").rstrip("\n")
            content = f"{existing}\n\n{entry}"
        else:
            content = _header(today) + entry

        _atomic_append(day_file, content)
        return day_file
    except Exception as exc:  # noqa: BLE001 - logging must never break the run
        try:
            result.setdefault("errors", []).append(
                {"stage": "audit", "error": str(exc)}
            )
        except Exception:
            pass
        return None
