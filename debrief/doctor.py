"""Debrief doctor: a plain-text preflight check.

run_checks() inspects everything Debrief needs and returns a list of results.
main() prints an aligned pass/fail table and exits non-zero only when a HARD
requirement is missing (model server, gemma model, writable vault, ffmpeg).
Soft warnings (speech-to-text, PDF stack, afconvert) never fail the exit code;
they just tell the user what is degraded and how to fix it.

No color libraries. Markers are plain text: [ ok ], [FAIL], [warn].
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys

from . import config, models, settings_store, stt


def _importable(module: str) -> bool:
    """True if a module can be imported without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _vault_writable() -> tuple[bool, str]:
    """Check the vault dir exists (or can be made) and is writable."""
    vault = config.VAULT_DIR
    try:
        vault.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create {vault}: {exc}"
    if not os.access(vault, os.W_OK):
        return False, f"{vault} is not writable"
    return True, f"{vault}"


def run_checks() -> list[dict]:
    """Run every preflight check.

    Returns a list of dicts: {name, ok, detail, fix, hard}. `hard` marks the
    checks whose failure makes Debrief unusable; soft checks only degrade it.
    """
    checks: list[dict] = []

    # --- Model server + gemma model (one probe, two checks) -------------------
    servers = models.detect_servers()
    reachable = [s for s in servers if s["reachable"]]
    lmstudio = next((s for s in servers if s["provider"] == "lmstudio"), None)
    gemma = models.pick_gemma()

    if reachable:
        checks.append({
            "key": "model_server",
            "name": "Local AI",
            "ok": True,
            "detail": "LM Studio is running on this Mac.",
            "fix": "",
            "hard": True,
        })
    else:
        checks.append({
            "key": "model_server",
            "name": "Local AI",
            "ok": False,
            "detail": "Debrief cannot find LM Studio running on this Mac.",
            "fix": "Open LM Studio and press Start Server, then Re-check.",
            "command": "lms server start",
            "hard": True,
        })

    if gemma is not None:
        checks.append({
            "key": "model_loaded",
            "name": "AI model",
            "ok": True,
            "detail": "The Gemma model is loaded and ready.",
            "fix": "",
            "hard": True,
        })
    else:
        # Distinguish "server up but no gemma" from "nothing running", and note
        # an Ollama gemma if one exists (not yet usable by the agent).
        ollama = next((s for s in servers if s["provider"] == "ollama"), None)
        if ollama and ollama["reachable"] and ollama["gemma_model"]:
            detail = (
                "Ollama has a Gemma model, but Debrief needs it loaded in "
                "LM Studio."
            )
        elif lmstudio and lmstudio["reachable"]:
            detail = "LM Studio is running, but no model is loaded yet."
        else:
            detail = "No AI model is loaded yet."
        checks.append({
            "key": "model_loaded",
            "name": "AI model",
            "ok": False,
            "detail": detail,
            "fix": "In LM Studio, load the Gemma model using the line below, then Re-check.",
            "command": f"lms load {config.MODEL} --context-length 64000 -y",
            "hard": True,
        })

    # --- Vault ---------------------------------------------------------------
    vault_ok, vault_detail = _vault_writable()
    checks.append({
        "key": "records_folder",
        "name": "Your records folder",
        "ok": vault_ok,
        "detail": vault_detail if vault_ok else "Debrief cannot save to this folder.",
        "fix": "" if vault_ok else (
            "Choose a folder Debrief is allowed to write to, or set "
            "DEBRIEF_VAULT_DIR to one before opening Debrief."
        ),
        "hard": True,
    })

    # --- ffmpeg (audio transcode) --------------------------------------------
    ffmpeg = shutil.which("ffmpeg")
    checks.append({
        "key": "audio_tools",
        "name": "Audio tools",
        "ok": ffmpeg is not None,
        "detail": "Installed." if ffmpeg else (
            "Not installed yet. Debrief needs this to read your microphone."
        ),
        "fix": "" if ffmpeg else "Paste this into Terminal, then Re-check.",
        "command": "" if ffmpeg else "brew install ffmpeg",
        "hard": True,
    })

    # --- Speech-to-text (soft), reflects the SELECTED engine -----------------
    _STT_MODULE = {"parakeet": "parakeet_mlx", "mlx-whisper": "mlx_whisper"}
    _STT_LABEL = {"parakeet": "parakeet-mlx", "mlx-whisper": "mlx-whisper"}
    selected = settings_store.load().get("stt_engine", "parakeet")
    module = _STT_MODULE.get(selected, "parakeet_mlx")
    label = _STT_LABEL.get(selected, selected)
    stt_ok = _importable(module)
    pretty = "Parakeet" if selected == "parakeet" else "MLX Whisper"
    stt_command = ""
    if stt_ok:
        detail = "Ready."
        try:
            cached = stt.is_engine_model_cached(selected)
        except Exception:  # noqa: BLE001
            cached = True
        if not cached:
            size = "about 1.6 GB" if selected == "mlx-whisper" else "a few hundred MB"
            detail = f"Ready. The first transcription downloads {size}, so allow a minute."
        stt_fix = ""
    else:
        detail = (
            "Not installed yet. Debrief cannot turn speech into text until "
            "this is in place."
        )
        stt_fix = "Reinstall Debrief, or paste this into Terminal, then Re-check."
        stt_command = "uv sync"
    checks.append({
        "key": "transcription",
        "name": f"Transcription ({pretty})",
        "ok": stt_ok,
        "detail": detail,
        "fix": stt_fix,
        "command": stt_command,
        "hard": False,
    })

    # --- PDF stack (soft) -----------------------------------------------------
    # Reflect real render capability: markdown importable AND WeasyPrint can do a
    # trivial render (import plus native pango/gobject actually loading).
    _PDF_UNAVAILABLE = (
        "Not set up. Debrief will save a styled page instead, which prints the same."
    )
    if not _importable("markdown"):
        pdf_ok, pdf_detail = False, _PDF_UNAVAILABLE
    else:
        try:
            from . import render

            pdf_ok = render.pdf_available()
            pdf_detail = (
                "Notes and worksheets can be saved as PDF."
                if pdf_ok
                else _PDF_UNAVAILABLE
            )
        except Exception:  # noqa: BLE001
            pdf_ok, pdf_detail = False, _PDF_UNAVAILABLE
    checks.append({
        "key": "pdf_export",
        "name": "PDF export",
        "ok": pdf_ok,
        "detail": pdf_detail,
        "fix": "" if pdf_ok else "Optional. Paste this into Terminal, then Re-check.",
        "command": "" if pdf_ok else "uv sync --extra pdf",
        "hard": False,
    })

    # --- afconvert (macOS audio archive, soft) -------------------------------
    if platform.system() == "Darwin":
        afconvert = shutil.which("afconvert")
        checks.append({
            "key": "afconvert",
            "name": "afconvert (macOS)",
            "ok": afconvert is not None,
            "detail": afconvert or "afconvert not found",
            "fix": "" if afconvert else "afconvert ships with macOS; check your PATH.",
            "hard": False,
            # Plumbing detail: useful in the terminal, noise in the wizard.
            "cli_only": True,
        })

    return checks


def _marker(check: dict) -> str:
    if check["ok"]:
        return "[ ok ]"
    return "[FAIL]" if check.get("hard") else "[warn]"


def main() -> int:
    """Print the check table and exit non-zero only on hard failures."""
    checks = run_checks()
    width = max(len(c["name"]) for c in checks)

    print("Debrief doctor")
    print("=" * (width + 40))
    hard_failed = False
    for c in checks:
        marker = _marker(c)
        print(f"{marker}  {c['name'].ljust(width)}  {c['detail']}")
        if not c["ok"] and c["fix"]:
            print(f"        fix: {c['fix']}")
        if not c["ok"] and c.get("hard"):
            hard_failed = True

    print("=" * (width + 40))
    if hard_failed:
        print("Result: not ready (fix the FAIL items above).")
        return 1
    print("Result: ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
