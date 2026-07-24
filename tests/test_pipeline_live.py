#!/usr/bin/env python
"""Full end-to-end LIVE test of the Debrief pipeline.

Synthesizes the standard "Bob" debrief with macOS `say`, transcodes it to a
16 kHz mono wav with ffmpeg, and runs the whole pipeline for real:

    parakeet STT -> Gemma glossary + extraction -> Calendar event + Mail draft
    -> vault note write + profile update -> Gemma vision screen verification.

Drives the REAL Calendar, Mail, Obsidian, and the local model. It cleans up
after itself (deletes the calendar event, removes the new session note, and
restores the vault with `git checkout -- vault/`) so it stays idempotent across
repeated runs. Requires LM Studio + parakeet + macOS Automation / Screen
Recording permissions, all confirmed present in this environment.

Run:
    .venv/bin/python tests/test_pipeline_live.py         # standalone, runs TWICE
    .venv/bin/python -m pytest tests/test_pipeline_live.py -v -s -m live
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from debrief import actions, pipeline  # noqa: E402
from debrief.config import CALENDAR_NAME, VAULT_DIR  # noqa: E402

CLIENT_ID = "C-0001"

# First name of C-0001 is Bob, so pipeline creates a calendar title that starts
# with "Bob ". The dedicated Debrief calendar holds nothing else, so this prefix
# is a safe cleanup key.
CAL_PREFIX = "Bob"
MAIL_SUBJECT = "Your next appointment"

# A say-friendly spoken debrief: CBT workplace stress, passive SI with denied
# plan and intent, a Tuesday 3pm follow-up, and a worksheet email.
SPOKEN_DEBRIEF = (
    "Okay, debrief for Bob. This was session fourteen, in person, about fifty "
    "minutes. We spent most of the time on his workplace stress. He came in "
    "describing another week of feeling completely overwhelmed at the office, "
    "and he said, I am convinced everyone there thinks I am a fraud. That is "
    "the classic mind reading and the worthlessness core belief we have been "
    "tracking. We did some cognitive restructuring around that automatic "
    "thought, walked through a thought record together, and looked at the "
    "evidence for and against the idea that his manager is out to get him. He "
    "softened on it by the end and admitted the evidence was thin. I also want "
    "to note, he mentioned some passive suicidal ideation but denied any plan "
    "or intent, said he would never act on it, and pointed to his kids as the "
    "reason. We did a brief safety check and he was future oriented by the end. "
    "For homework I assigned continued thought records when the fraud feeling "
    "spikes. Book him for next Tuesday at 3, and send him the thought record "
    "worksheet with a quick confirmation email."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _osascript(script: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()


def count_calendar_events(prefix: str) -> int:
    """Number of events in the dedicated calendar whose summary starts prefix."""
    script = (
        'tell application "Calendar"\n'
        f'  if not (exists calendar "{CALENDAR_NAME}") then\n'
        "    return 0\n"
        "  end if\n"
        f'  tell calendar "{CALENDAR_NAME}"\n'
        "    return count of (every event whose summary starts with "
        f'"{prefix}")\n'
        "  end tell\n"
        "end tell\n"
    )
    ok, out = _osascript(script)
    try:
        return int(out) if ok else -1
    except ValueError:
        return -1


def close_test_mail_windows() -> None:
    """Best-effort close of the drafted appointment email so runs do not pile up.

    Targets only windows whose name contains the fixed subject line, then clicks
    Don't Save on the compose sheet. Requires Accessibility (granted here).
    """
    script = (
        'tell application "Mail" to activate\n'
        "delay 0.3\n"
        "repeat 4 times\n"
        '  tell application "Mail"\n'
        f'    set wins to (every window whose name contains "{MAIL_SUBJECT}")\n'
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
    try:
        _osascript(script)
    except Exception:
        pass


def make_wav(workdir: Path) -> str:
    """Synthesize the spoken debrief and transcode to 16 kHz mono wav."""
    aiff = workdir / "debrief.aiff"
    wav = workdir / "debrief.wav"
    subprocess.run(
        ["say", "-o", str(aiff), SPOKEN_DEBRIEF], check=True, timeout=120
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(wav)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return str(wav)


def restore_vault() -> None:
    """Restore tracked vault files (profile summary etc.) to HEAD. Safe: only vault/.

    Only meaningful when the vault is the in-repo DebriefVault. When the test is
    pointed at an out-of-repo vault (e.g. DEBRIEF_VAULT_DIR set to a scratch dir),
    there is nothing tracked to restore, so this is a no-op.
    """
    try:
        rel = VAULT_DIR.relative_to(REPO_ROOT)
    except ValueError:
        return
    subprocess.run(
        ["git", "checkout", "--", f"{rel}/"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


SESSIONS_DIR = VAULT_DIR / "Clients" / CLIENT_ID / "Sessions"


def session_notes() -> set[Path]:
    """Session artifacts this run could create: notes plus archived audio."""
    if not SESSIONS_DIR.exists():
        return set()
    found = set(SESSIONS_DIR.glob("*.md"))
    found |= set((SESSIONS_DIR / "audio").glob("*.m4a"))
    return found


def cleanup(new_notes: set[Path]) -> None:
    actions.delete_test_events(CAL_PREFIX)
    close_test_mail_windows()
    for p in new_notes:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    # Drop the audio dir if this run left it empty.
    try:
        (SESSIONS_DIR / "audio").rmdir()
    except OSError:
        pass
    restore_vault()


# ---------------------------------------------------------------------------
# one full run + assertions
# ---------------------------------------------------------------------------


def run_once(run_label: str) -> dict:
    print(f"\n==================== {run_label} ====================", flush=True)

    # Start from a clean calendar so leftovers never cause a false positive.
    actions.delete_test_events(CAL_PREFIX)

    # Snapshot existing session notes so cleanup removes exactly what this run
    # creates (base file plus any -N suffix), leaving the vault untouched.
    notes_before = session_notes()

    note_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="debrief_pipe_") as tmp:
            wav_path = make_wav(Path(tmp))
            result = pipeline.run_debrief(
                wav_path, CLIENT_ID, execute=True, verify=True
            )

        note_path = result.get("note_path")

        transcript = result.get("transcript", "")
        note = result.get("note", {}) or {}
        result_actions = result.get("actions", []) or []
        verification = result.get("verification", []) or []
        timings = result.get("timings", {}) or {}
        errors = result.get("errors", []) or []

        # --- print an at-a-glance report ---------------------------------
        print(f"transcript ({len(transcript)} chars): {transcript[:180]}...", flush=True)
        print(f"risk_present: {note.get('risk_present')}", flush=True)
        for a in result_actions:
            print(f"  action {a.get('type')}: status={a.get('status')} :: {a.get('detail')}", flush=True)
        print(f"note_path: {note_path}", flush=True)
        for v in verification:
            print(
                f"  verify[{v.get('surface')}] confirmed={v.get('confirmed')} :: "
                f"{str(v.get('what_i_see',''))[:200]}",
                flush=True,
            )
        print(f"timings (s): {timings}", flush=True)
        if errors:
            print(f"errors: {errors}", flush=True)

        # --- assertions --------------------------------------------------
        assert transcript.strip(), "transcript must be non-empty"
        assert note.get("risk_present") is True, "risk_present must be true (passive SI)"

        assert len(result_actions) == 2, f"expected 2 actions, got {len(result_actions)}"
        for a in result_actions:
            assert a.get("status") == "ok", (
                f"action {a.get('type')} not ok: {a.get('status')} / {a.get('detail')}"
            )

        # Note file exists and carries a Risk section.
        assert note_path, "run_debrief returned no note_path"
        note_file = Path(note_path)
        assert note_file.exists(), f"session note not written: {note_path}"
        note_text = note_file.read_text(encoding="utf-8")
        assert "## Risk" in note_text, "session note missing Risk section"
        assert (
            "Clinical judgment and final session planning remain the therapist's responsibility."
            in note_text
        ), "session note missing the required disclaimer line"

        # Dictation audio archived next to the note with a matching stem.
        audio_file = note_file.parent / "audio" / f"{note_file.stem}.m4a"
        assert audio_file.exists() and audio_file.stat().st_size > 0, (
            f"archived dictation audio missing: {audio_file}"
        )
        assert f"![[{note_file.stem}.m4a]]" in note_text, "note missing audio embed"
        assert f"audio/{note_file.stem}.m4a" in note_text, "frontmatter missing audio field"

        # Calendar event exists (queried independently via osascript).
        cal_count = count_calendar_events(CAL_PREFIX)
        assert cal_count >= 1, f"calendar event not found (count={cal_count})"

        # Verification: at least two surfaces confirmed on screen.
        confirmed = sum(1 for v in verification if v.get("confirmed"))
        assert len(verification) >= 2, f"expected >=2 verification checks, got {len(verification)}"
        assert confirmed >= 2, (
            f"expected >=2 confirmed screen checks, got {confirmed} of {len(verification)}"
        )

        print(f"[PASS] {run_label}: all assertions passed.", flush=True)
        return result
    finally:
        new_notes = session_notes() - notes_before
        cleanup(new_notes)
        # Confirm cleanup left no test calendar events and no new session notes.
        remaining = count_calendar_events(CAL_PREFIX)
        leftover = session_notes() - notes_before
        print(
            f"cleanup: remaining test calendar events = {remaining}, "
            f"leftover session notes = {len(leftover)}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# pytest entry (runs twice to prove idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_pipeline_live_runs_twice():
    run_once("LIVE RUN 1 of 2")
    run_once("LIVE RUN 2 of 2")


if __name__ == "__main__":
    run_once("LIVE RUN 1 of 2")
    run_once("LIVE RUN 2 of 2")
    print("\nBOTH LIVE RUNS PASSED.", flush=True)
