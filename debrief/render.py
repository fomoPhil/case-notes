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

import html as _html
import os
import shutil
from html.parser import HTMLParser
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_CSS_PATH = _STATIC_DIR / "print" / "quiet-sage.css"

# What the clinician reads when a PDF cannot be made. The developer command
# lives in PDF_FIX_COMMAND so the UI can offer it behind a details disclosure
# instead of putting shell syntax in front of a therapist.
PDF_FIX = (
    "PDF export is not set up on this Mac yet. Debrief can still save a styled "
    "page that prints the same way. Open Setup to finish PDF setup."
)
PDF_FIX_COMMAND = (
    "uv sync --extra pdf (on macOS also: brew install pango if weasyprint "
    "fails on native libs)."
)


class PdfUnavailable(RuntimeError):
    """Raised when a PDF was requested but WeasyPrint cannot render one.

    Carries clinician-readable fix text so callers can surface it or fall back
    to styled HTML. The developer command is available as PDF_FIX_COMMAND.
    """

    def __init__(self, message: str = "PDF export is not available.", fix: str = PDF_FIX):
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


# ---------------------------------------------------------------------------
# HTML sanitizer (stdlib-only allowlist)
# ---------------------------------------------------------------------------
#
# Uploaded markdown can carry raw inline HTML (python-markdown's `extra` passes
# it through). The records document view injects the rendered fragment via
# innerHTML, so an unsanitized <img onerror=...> or <script> would execute in
# the therapist's browser. We run the produced HTML through a small allowlist
# sanitizer built on the stdlib html.parser: no new dependency, and only known
# structural tags survive. All on* event handlers and style are stripped, and
# script/style/iframe/object/embed are dropped along with their contents.

_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "strong", "em",
    "code", "pre", "blockquote", "table", "thead", "tbody", "tr", "th", "td",
    "hr", "br", "a", "img", "details", "summary",
}
_VOID_TAGS = {"br", "hr", "img"}
# Dangerous containers: drop the tag and everything inside it.
_DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed"}
# Per-tag attribute allowlist (everything else, including on* and style, drops).
_ALLOWED_ATTRS = {"a": {"href"}, "img": {"src"}}


def _safe_href(val: str) -> bool:
    v = (val or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _safe_img_src(val: str) -> bool:
    # Vault-internal images only: a single leading slash, not protocol-relative.
    v = (val or "").strip()
    return v.startswith("/") and not v.startswith("//")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT_TAGS:
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set())
        clean: list[str] = []
        for name, val in attrs:
            lname = (name or "").lower()
            if lname.startswith("on") or lname == "style" or lname not in allowed:
                continue
            if val is None:
                continue
            if tag == "a" and lname == "href" and not _safe_href(val):
                continue
            if tag == "img" and lname == "src" and not _safe_img_src(val):
                continue
            clean.append(f' {lname}="{_html.escape(val, quote=True)}"')
        joined = "".join(clean)
        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}{joined} />")
        else:
            self.out.append(f"<{tag}{joined}>")

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        self._emit_start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        # Self-closing form (e.g. <img/>, <br/>): never opens a skip region.
        self._emit_start(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(_html.escape(data, quote=False))


def sanitize_fragment(html_fragment: str) -> str:
    """Allowlist-sanitize an HTML fragment so it is safe to inject via innerHTML.

    Keeps structural tags (headings, lists, tables, code, details/summary),
    strips every event-handler and style attribute, and removes
    script/style/iframe/object/embed along with their contents.
    """
    parser = _Sanitizer()
    parser.feed(html_fragment or "")
    parser.close()
    return "".join(parser.out)


def markdown_to_html(md: str, title: str = "Document") -> str:
    """Render markdown to a full, self-contained Quiet Sage HTML document.

    Uses python-markdown with the extra, tables, and sane_lists extensions. The
    stylesheet is inlined so the string is portable. Fonts are referenced
    relatively (see quiet-sage.css); WeasyPrint resolves them via base_url, and
    a browser falls back to system serif/sans if it cannot reach them.
    """
    import markdown as _markdown

    body_html = sanitize_fragment(
        _markdown.markdown(
            _no_em_dash(md or ""),
            extensions=["extra", "tables", "sane_lists"],
        )
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


def markdown_to_fragment(md: str) -> str:
    """Render markdown to a body-only HTML fragment (no document wrapper).

    Suitable for embedding inside a page that already provides the Quiet Sage
    styling (the records document view wraps this in a `.document` container).
    """
    import markdown as _markdown

    return sanitize_fragment(
        _markdown.markdown(
            _no_em_dash(md or ""),
            extensions=["extra", "tables", "sane_lists"],
        )
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
