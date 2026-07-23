"""Shared document renderer: markdown to Quiet Sage HTML and PDF.

Used by the in-app agent (worksheet previews, Phase 2) and the client records
document view (Phase 3). One code path produces the styled HTML; WeasyPrint
turns that same HTML into a PDF when its native libraries are available.

Design:
  - markdown_to_html(md, title): python-markdown (extra, tables, sane_lists)
    wrapped in a full HTML document with static/print/quiet-sage.css inlined.
  - render_pdf / render_pdf_bytes: WeasyPrint (lazy import). When WeasyPrint or
    its native stack is unavailable, raises PdfUnavailable carrying the doctor's
    fix text so callers can fall back to HTML.
  - pdf_available(): import + trivial render probe, cached.

WeasyPrint on macOS needs Homebrew's pango/gobject on the dynamic loader path.
We prepend the Homebrew lib dir to DYLD_FALLBACK_LIBRARY_PATH before importing
weasyprint, which makes the import succeed inside a plain `python` process.

No em dashes anywhere in generated copy.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_CSS_PATH = _STATIC_DIR / "print" / "quiet-sage.css"

# The doctor's fix text, mirrored here so PdfUnavailable can carry it.
PDF_FIX = (
    "uv sync --extra pdf (on macOS also: brew install pango if weasyprint "
    "fails on native libs)."
)


class PdfUnavailable(RuntimeError):
    """Raised when a PDF was requested but WeasyPrint cannot render one.

    Carries the doctor's fix text so callers can surface it or fall back to
    styled HTML.
    """

    def __init__(self, message: str = "PDF rendering is unavailable.", fix: str = PDF_FIX):
        super().__init__(message)
        self.fix = fix


def _ensure_native_lib_path() -> None:
    """Prepend Homebrew's lib dir to DYLD_FALLBACK_LIBRARY_PATH (macOS).

    WeasyPrint's cffi dlopen honors this at import time, so setting it before
    the lazy import lets `libgobject-2.0-0` and friends resolve without the
    caller having to export anything.
    """
    brew = shutil.which("brew")
    candidates = []
    if brew:
        prefix = Path(brew).resolve().parent.parent
        candidates.append(prefix / "lib")
    candidates.append(Path("/opt/homebrew/lib"))
    candidates.append(Path("/usr/local/lib"))

    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    changed = False
    for lib in candidates:
        s = str(lib)
        if lib.is_dir() and s not in parts:
            parts.append(s)
            changed = True
    if changed:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(parts)


def _read_css() -> str:
    try:
        return _CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _no_em_dash(text: str) -> str:
    return text.replace("—", "-").replace("―", "-")


def markdown_to_html(md: str, title: str = "Document") -> str:
    """Render markdown to a full, self-contained Quiet Sage HTML document.

    Uses python-markdown with the extra, tables, and sane_lists extensions. The
    stylesheet is inlined so the string is portable. Fonts are referenced
    relatively (see quiet-sage.css); WeasyPrint resolves them via base_url, and
    a browser falls back to system serif/sans if it cannot reach them.
    """
    import markdown as _markdown

    body_html = _markdown.markdown(
        _no_em_dash(md or ""),
        extensions=["extra", "tables", "sane_lists"],
    )
    css = _read_css()
    safe_title = _no_em_dash(title or "Document")
    # Escape only the characters that matter inside <title>.
    safe_title = (
        safe_title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n<body>\n"
        f'<article class="document">\n{body_html}\n</article>\n'
        "</body>\n</html>\n"
    )


def pdf_available() -> bool:
    """True if WeasyPrint can import and produce a trivial PDF on this machine.

    Cached: the native import + a one-line render is the honest signal, but it
    is slow, so the result is memoized for the process.
    """
    if _PDF_PROBE["done"]:
        return _PDF_PROBE["ok"]
    ok = False
    try:
        _ensure_native_lib_path()
        from weasyprint import HTML  # noqa: F401

        data = HTML(string="<p>probe</p>").write_pdf()
        ok = bool(data) and data[:4] == b"%PDF"
    except Exception:
        ok = False
    _PDF_PROBE["ok"] = ok
    _PDF_PROBE["done"] = True
    return ok


_PDF_PROBE: dict = {"done": False, "ok": False}


def render_pdf_bytes(md: str, title: str = "Document") -> bytes:
    """Render markdown to PDF bytes via WeasyPrint. Raises PdfUnavailable.

    The lazy import isolates the optional dependency so importing this module
    never requires WeasyPrint.
    """
    _ensure_native_lib_path()
    try:
        from weasyprint import HTML
    except Exception as exc:  # noqa: BLE001 - any import/native failure is "unavailable"
        raise PdfUnavailable(f"WeasyPrint is not available: {exc}") from exc

    html = markdown_to_html(md, title)
    try:
        # base_url = static dir so quiet-sage.css's relative font URLs resolve.
        data = HTML(string=html, base_url=str(_STATIC_DIR)).write_pdf()
    except Exception as exc:  # noqa: BLE001
        raise PdfUnavailable(f"WeasyPrint failed to render: {exc}") from exc
    if not data or data[:4] != b"%PDF":
        raise PdfUnavailable("WeasyPrint produced no PDF output.")
    return data


def render_pdf(md: str, title: str, dest: Path) -> Path:
    """Render markdown to a PDF file at dest. Returns the path. Raises PdfUnavailable."""
    dest = Path(dest)
    data = render_pdf_bytes(md, title)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp file in the same dir, then replace.
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest
