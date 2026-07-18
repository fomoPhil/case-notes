"""macOS actions: Calendar events and Mail drafts via osascript.

Deterministic hands. These run reliably instead of driving the UI by clicks.

Hard rules:
  - Calendar: only ever touch the dedicated CALENDAR_NAME calendar. Create it
    if missing. Never create or modify events in any other calendar.
  - Mail: drafts only, visible:true, NEVER send.
  - Event titles use a first name only (caller's responsibility).
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

from .config import CALENDAR_NAME


def _run_osascript(script: str) -> tuple[bool, str]:
    """Run an AppleScript. Returns (ok, stdout_or_stderr)."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()


def _as_string(value: str) -> str:
    """Return an AppleScript string expression for value.

    Escapes quotes/backslashes and turns newlines into `& return &` so the
    expression is safe to embed anywhere a string literal is expected.
    """
    parts = value.split("\n")
    escaped = [p.replace("\\", "\\\\").replace('"', '\\"') for p in parts]
    return " & return & ".join(f'"{p}"' for p in escaped)


def _applescript_date(dt: _dt.datetime) -> str:
    """AppleScript snippet building a date variable `theStart` from dt.

    Sets fields numerically to avoid locale-dependent date-string parsing.
    Day is set to 1 first so setting month never rolls over a short month.
    """
    return (
        "set theStart to (current date)\n"
        "set day of theStart to 1\n"
        f"set year of theStart to {dt.year}\n"
        f"set month of theStart to {dt.month}\n"
        f"set day of theStart to {dt.day}\n"
        f"set hours of theStart to {dt.hour}\n"
        f"set minutes of theStart to {dt.minute}\n"
        "set seconds of theStart to 0\n"
    )


def create_calendar_event(title: str, dt: _dt.datetime, duration_min: int) -> bool:
    """Create an event in the dedicated calendar. Returns True on success."""
    script = (
        f"{_applescript_date(dt)}"
        f"set theEnd to theStart + ({int(duration_min)} * minutes)\n"
        'tell application "Calendar"\n'
        f'  if not (exists calendar "{CALENDAR_NAME}") then\n'
        f'    make new calendar with properties {{name:"{CALENDAR_NAME}"}}\n'
        "  end if\n"
        f'  tell calendar "{CALENDAR_NAME}"\n'
        "    make new event with properties {summary:"
        f"{_as_string(title)}, start date:theStart, end date:theEnd}}\n"
        "  end tell\n"
        "end tell\n"
    )
    ok, _ = _run_osascript(script)
    return ok


def open_calendar_at(dt: _dt.datetime) -> None:
    """Bring Calendar frontmost and navigate to the week of dt."""
    script = (
        f"{_applescript_date(dt)}"
        'tell application "Calendar"\n'
        "  activate\n"
        "  switch view to week view\n"
        "  view calendar at theStart\n"
        "end tell\n"
    )
    _run_osascript(script)


def create_mail_draft(
    to: str, subject: str, body: str, attachment: Path | None
) -> bool:
    """Create a visible Mail draft. NEVER sends. Returns True on success."""
    lines = [
        'tell application "Mail"',
        "  set newMessage to make new outgoing message with properties "
        f"{{subject:{_as_string(subject)}, content:{_as_string(body)}, visible:true}}",
        "  tell newMessage",
        f"    make new to recipient with properties {{address:{_as_string(to)}}}",
        "  end tell",
    ]
    if attachment is not None:
        att = Path(attachment)
        if att.exists():
            posix = _as_string(str(att.resolve()))
            lines.append(
                "  tell content of newMessage to make new attachment "
                f"with properties {{file name:(POSIX file ({posix}))}} "
                "at after the last paragraph"
            )
    lines.append("  activate")
    lines.append("end tell")
    ok, _ = _run_osascript("\n".join(lines))
    return ok


def delete_test_events(title_prefix: str) -> int:
    """Delete events in the dedicated calendar whose summary starts with the
    given prefix. Returns the number deleted. Cleanup for repeated test runs.
    """
    script = (
        'tell application "Calendar"\n'
        f'  if not (exists calendar "{CALENDAR_NAME}") then\n'
        "    return 0\n"
        "  end if\n"
        f'  tell calendar "{CALENDAR_NAME}"\n'
        "    set matches to (every event whose summary starts with "
        f"{_as_string(title_prefix)})\n"
        "    set n to count of matches\n"
        "    repeat with anEvent in matches\n"
        "      delete anEvent\n"
        "    end repeat\n"
        "    return n\n"
        "  end tell\n"
        "end tell\n"
    )
    ok, out = _run_osascript(script)
    if not ok:
        return 0
    try:
        return int(out)
    except (ValueError, TypeError):
        return 0
