"""Screen verification: model eyes.

Bring a surface frontmost, screenshot it, downscale, and ask the local Gemma 4
vision model whether the requested artifact is actually on screen. This is the
loop that closes Track 2: the model reads the live screen and reports what it
sees. Everything stays on the Mac.
"""

from __future__ import annotations

import subprocess
import time

_SHOT_PATH = "/tmp/debrief_verify.png"
_SETTLE_SECONDS = 1.5
_DOWNSCALE_MAX = 1512

# Map a logical surface name to the macOS application to activate.
_SURFACE_APP = {
    "calendar": "Calendar",
    "obsidian": "Obsidian",
    "mail": "Mail",
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "what_i_see": {"type": "string"},
    },
    "required": ["confirmed", "what_i_see"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a careful visual verifier looking at a macOS screenshot. "
    "Answer only from what is actually visible in the image. Do not assume or "
    "invent. If the requested item is not clearly visible, set confirmed to "
    "false. Describe concretely what you see on the screen."
)


def _activate(app_name: str) -> None:
    """Bring an application frontmost via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _capture_and_downscale(path: str) -> bool:
    """Screenshot the display and downscale it. Returns True on success."""
    try:
        shot = subprocess.run(
            ["screencapture", "-x", path], capture_output=True, timeout=30
        )
        if shot.returncode != 0:
            return False
        subprocess.run(
            ["sips", "-Z", str(_DOWNSCALE_MAX), path],
            capture_output=True,
            timeout=30,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def verify_on_screen(checks: list[dict]) -> list[dict]:
    """For each check, bring its surface frontmost, screenshot it, and ask the
    vision model the question. Returns the checks with confirmed / what_i_see.

    check: {"surface": "calendar"|"obsidian"|"mail", "question": str}
    result adds: {"confirmed": bool, "what_i_see": str}
    """
    # Lazy import: llm.py may be built by another agent and is only needed here.
    from . import llm

    results: list[dict] = []
    for check in checks:
        surface = check.get("surface", "")
        question = check.get("question", "")
        app_name = _SURFACE_APP.get(surface, surface.capitalize())

        _activate(app_name)
        time.sleep(_SETTLE_SECONDS)

        result = dict(check)
        if not _capture_and_downscale(_SHOT_PATH):
            result["confirmed"] = False
            result["what_i_see"] = "Verification error: could not capture the screen."
            results.append(result)
            continue

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        try:
            answer = llm.chat(
                messages, schema=VERIFY_SCHEMA, images=[_SHOT_PATH]
            )
            if isinstance(answer, dict):
                result["confirmed"] = bool(answer.get("confirmed", False))
                result["what_i_see"] = str(answer.get("what_i_see", ""))
            else:
                result["confirmed"] = False
                result["what_i_see"] = f"Unexpected model reply: {answer!r}"
        except Exception as exc:  # keep the demo alive on any model failure
            result["confirmed"] = False
            result["what_i_see"] = f"Verification error: {exc}"

        results.append(result)

    return results
