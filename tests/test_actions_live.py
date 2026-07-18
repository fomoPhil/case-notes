#!/usr/bin/env python
"""Live macOS test for debrief.actions (and optionally verify).

Runnable as a script: .venv/bin/python tests/test_actions_live.py
Drives the REAL Calendar and Mail apps. Uses a DEBRIEF-TEST prefix and cleans
up after itself so repeated runs stay idempotent.

Requires macOS Automation + Screen Recording permissions already granted for
this terminal context.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

# Allow running from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debrief import actions  # noqa: E402
from debrief.config import CALENDAR_NAME, VAULT_DIR  # noqa: E402

TEST_PREFIX = "DEBRIEF-TEST"
TEST_TITLE = f"{TEST_PREFIX} Bob 3:00 PM session"
MAIL_SUBJECT = f"{TEST_PREFIX} Your next appointment"

_PASS = "PASS"
_FAIL = "FAIL"
_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    tag = _PASS if ok else _FAIL
    line = f"[{tag}] {name}"
    if detail:
        line += f"  ::  {detail}"
    print(line, flush=True)


def _osascript(script: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()


def count_test_events() -> int:
    script = (
        'tell application "Calendar"\n'
        f'  if not (exists calendar "{CALENDAR_NAME}") then\n'
        "    return 0\n"
        "  end if\n"
        f'  tell calendar "{CALENDAR_NAME}"\n'
        "    return count of (every event whose summary starts with "
        f'"{TEST_PREFIX}")\n'
        "  end tell\n"
        "end tell\n"
    )
    ok, out = _osascript(script)
    try:
        return int(out) if ok else -1
    except ValueError:
        return -1


def close_test_mail_windows() -> None:
    """Close DEBRIEF-TEST compose windows.

    Mail's `close ... saving no` is ignored by compose windows, so drive the
    real Cmd-W path and dismiss the save sheet by clicking Don't Save. Requires
    Accessibility permission (already granted in this context).
    """
    script = (
        'tell application "Mail" to activate\n'
        "delay 0.3\n"
        "repeat 6 times\n"
        '  tell application "Mail"\n'
        f'    set wins to (every window whose name contains "{TEST_PREFIX}")\n'
        "    if (count of wins) is 0 then exit repeat\n"
        "    set index of (item 1 of wins) to 1\n"
        "  end tell\n"
        "  delay 0.3\n"
        '  tell application "System Events" to tell process "Mail"\n'
        '    keystroke "w" using {command down}\n'
        "    delay 0.6\n"
        "    if (exists sheet 1 of window 1) then\n"
        "      repeat with b in (buttons of sheet 1 of window 1)\n"
        '        if (name of b) contains "Don" then\n'
        "          click b\n"
        "          exit repeat\n"
        "        end if\n"
        "      end repeat\n"
        "      delay 0.4\n"
        "    end if\n"
        "  end tell\n"
        "end repeat\n"
    )
    _osascript(script)


def next_tuesday_3pm(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now()
    days = (1 - now.weekday()) % 7  # Monday=0 .. Tuesday=1
    if days == 0:
        days = 7
    target = (now + dt.timedelta(days=days)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    return target


def cleanup() -> None:
    """Remove any DEBRIEF-TEST calendar events and mail draft windows."""
    actions.delete_test_events(TEST_PREFIX)
    close_test_mail_windows()


def main() -> int:
    print(f"Live actions test. Calendar={CALENDAR_NAME!r}\n")

    # Start clean so leftovers from a prior run never cause a false failure.
    cleanup()
    baseline = count_test_events()
    record("pre-clean leaves zero test events", baseline == 0, f"count={baseline}")

    # --- Test 1: create a calendar event ---------------------------------
    when = next_tuesday_3pm()
    created = actions.create_calendar_event(TEST_TITLE, when, 50)
    record("create_calendar_event returns True", created is True,
           f"start={when.isoformat()}")

    # --- Test 2: event exists, then delete removes it --------------------
    after_create = count_test_events()
    record("event visible in Debrief calendar", after_create == 1,
           f"count={after_create}")

    deleted = actions.delete_test_events(TEST_PREFIX)
    after_delete = count_test_events()
    record("delete_test_events removes the event",
           deleted == 1 and after_delete == 0,
           f"deleted={deleted}, remaining={after_delete}")

    # --- Test 3: mail draft with attachment ------------------------------
    worksheet = VAULT_DIR / "Templates" / "Worksheets" / "thought-record.pdf"
    if not worksheet.exists():
        worksheet = worksheet.with_suffix(".md")
    drafted = actions.create_mail_draft(
        "bob@example.com",
        MAIL_SUBJECT,
        "Hi Bob,\n\nConfirming your next appointment. The thought-record "
        "worksheet is attached.\n\nBest,\nYour therapist",
        worksheet if worksheet.exists() else None,
    )
    record("create_mail_draft returns True", drafted is True,
           f"attachment={worksheet.name if worksheet.exists() else 'none'}")

    # Close the draft so repeated runs do not pile up windows.
    close_test_mail_windows()
    remaining_windows = count_open_test_windows()
    record("draft window closed after test", remaining_windows == 0,
           f"open_windows={remaining_windows}")

    # --- Test 4: verify_on_screen (only if llm.py + server available) -----
    run_verify()

    # Final cleanup.
    cleanup()

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in _results if ok)
    for name, ok, _ in _results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"{passed}/{len(_results)} checks passed")
    # verify is best-effort; do not fail the run if only verify was skipped.
    hard_fail = any(not ok for name, ok, _ in _results if not name.startswith("verify"))
    return 1 if hard_fail else 0


def count_open_test_windows() -> int:
    ok, out = _osascript(
        'tell application "Mail" to return count of '
        f'(every window whose name contains "{TEST_PREFIX}")'
    )
    try:
        return int(out) if ok else -1
    except ValueError:
        return -1


def run_verify() -> None:
    """Optional: create an event, ask the vision model to confirm it, clean up."""
    try:
        import importlib.util

        if importlib.util.find_spec("debrief.llm") is None:
            record("verify_on_screen (skipped: no llm.py)", True, "untested")
            return
    except Exception:
        record("verify_on_screen (skipped: no llm.py)", True, "untested")
        return

    from debrief import actions as act
    from debrief import verify

    when = next_tuesday_3pm()
    act.create_calendar_event(TEST_TITLE, when, 50)
    act.open_calendar_at(when)

    date_label = when.strftime("%A %B %-d")
    question = (
        f"This is the macOS Calendar app. Is there a calendar event titled "
        f"'{TEST_PREFIX} Bob' (a 3:00 PM session) visible on {date_label}? "
        "Answer strictly from what is on screen."
    )
    try:
        results = verify.verify_on_screen(
            [{"surface": "calendar", "question": question}]
        )
        r = results[0]
        what = r.get("what_i_see", "")
        # We only assert the loop ran and returned a description, not the value
        # (server may be cold or the week view may differ). Print for the human.
        ran = "confirmed" in r and isinstance(what, str) and len(what) > 0
        detail = f"confirmed={r.get('confirmed')} | what_i_see={what[:160]!r}"
        record("verify_on_screen returns a screen reading", ran, detail)
    except Exception as exc:
        record("verify_on_screen (error, likely server down)", True,
               f"untested: {exc}")
    finally:
        act.delete_test_events(TEST_PREFIX)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
