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

from . import config, models


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
        names = ", ".join(s["provider"] for s in reachable)
        checks.append({
            "name": "Model server reachable",
            "ok": True,
            "detail": f"reachable: {names}",
            "fix": "",
            "hard": True,
        })
    else:
        checks.append({
            "name": "Model server reachable",
            "ok": False,
            "detail": "no model server answered on :1234 (LM Studio) or :11434 (Ollama)",
            "fix": "Open LM Studio and start its server (lms server start).",
            "hard": True,
        })

    if gemma is not None:
        checks.append({
            "name": "Gemma model loaded",
            "ok": True,
            "detail": f"{gemma['model']} via {gemma['base_url']}",
            "fix": "",
            "hard": True,
        })
    else:
        # Distinguish "server up but no gemma" from "nothing running", and note
        # an Ollama gemma if one exists (not yet usable by the agent).
        ollama = next((s for s in servers if s["provider"] == "ollama"), None)
        if ollama and ollama["reachable"] and ollama["gemma_model"]:
            detail = (
                f"only Ollama has a gemma ({ollama['gemma_model']}); "
                "the agent needs it in LM Studio"
            )
            fix = f"In LM Studio, load {config.MODEL} --context-length 64000."
        elif lmstudio and lmstudio["reachable"]:
            detail = "LM Studio is up but no gemma model is loaded"
            fix = f"In LM Studio: lms load {config.MODEL} --context-length 64000 -y"
        else:
            detail = "no gemma model available"
            fix = f"Start LM Studio and load {config.MODEL}."
        checks.append({
            "name": "Gemma model loaded",
            "ok": False,
            "detail": detail,
            "fix": fix,
            "hard": True,
        })

    # --- Vault ---------------------------------------------------------------
    vault_ok, vault_detail = _vault_writable()
    checks.append({
        "name": "Vault writable",
        "ok": vault_ok,
        "detail": vault_detail,
        "fix": "" if vault_ok else "Set DEBRIEF_VAULT_DIR to a writable folder.",
        "hard": True,
    })

    # --- ffmpeg (audio transcode) --------------------------------------------
    ffmpeg = shutil.which("ffmpeg")
    checks.append({
        "name": "ffmpeg on PATH",
        "ok": ffmpeg is not None,
        "detail": ffmpeg or "ffmpeg not found",
        "fix": "" if ffmpeg else "brew install ffmpeg",
        "hard": True,
    })

    # --- Speech-to-text (soft) -----------------------------------------------
    stt_ok = _importable("parakeet_mlx")
    checks.append({
        "name": "Speech-to-text (parakeet-mlx)",
        "ok": stt_ok,
        "detail": "parakeet_mlx importable" if stt_ok else "parakeet_mlx not importable",
        "fix": "" if stt_ok else "uv sync (parakeet-mlx installs on Apple Silicon).",
        "hard": False,
    })

    # --- PDF stack (soft) -----------------------------------------------------
    # Reflect real render capability: markdown importable AND WeasyPrint can do a
    # trivial render (import plus native pango/gobject actually loading).
    pdf_fix = (
        "uv sync --extra pdf (on macOS also: brew install pango if weasyprint "
        "fails on native libs)."
    )
    if not _importable("markdown"):
        pdf_ok, pdf_detail = False, "markdown library not importable"
    else:
        try:
            from . import render

            pdf_ok = render.pdf_available()
            pdf_detail = (
                "PDF rendering works (weasyprint + native libs)"
                if pdf_ok
                else "weasyprint present but native libs did not render (falling back to HTML)"
            )
        except Exception as exc:  # noqa: BLE001
            pdf_ok, pdf_detail = False, f"PDF probe failed: {exc}"
    checks.append({
        "name": "PDF export (weasyprint + markdown)",
        "ok": pdf_ok,
        "detail": pdf_detail,
        "fix": "" if pdf_ok else pdf_fix,
        "hard": False,
    })

    # --- afconvert (macOS audio archive, soft) -------------------------------
    if platform.system() == "Darwin":
        afconvert = shutil.which("afconvert")
        checks.append({
            "name": "afconvert (macOS)",
            "ok": afconvert is not None,
            "detail": afconvert or "afconvert not found",
            "fix": "" if afconvert else "afconvert ships with macOS; check your PATH.",
            "hard": False,
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
