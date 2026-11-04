"""Read a document into plain text: md, txt, docx, pdf.

One extractor, shared. Template import (debrief.compiler) needs it to read the
blank note template a professional uploads; source ingestion needs it to read a
clinician's own written notes or a meeting-tool export. Both call this module, so
neither has to import the other and there is never a second implementation to
keep in step.

Deliberately thin on dependencies: the standard library only, no model calls, no
network, no vault. Both external converters are macOS tools already present on
the machine (textutil for docx) or best effort (pdftotext for pdf, which is
absent on a stock Mac; that case is reported, not raised, so the UI can suggest
another format).

Failures raise DocumentReadError. An unsupported extension raises ValueError,
because callers treat "this file type is not accepted" as a different answer from
"the reader broke".

No em dashes anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class DocumentReadError(RuntimeError):
    """A document could not be read. The message is safe to show the user."""


# Only these extensions are accepted. docx is read with the macOS textutil
# converter; pdf is best effort via pdftotext when present.
_ALLOWED_DOC_EXTS = {".md", ".txt", ".docx", ".pdf"}

# Hard cap on how much of a document we ever hand a model. A blank template is
# tiny; this only guards against someone uploading a novel.
MAX_DOC_CHARS = 20000


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _as_bytes(path_or_bytes, filename: str) -> bytes:
    """Return the raw bytes for either a bytes payload or a filesystem path."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return bytes(path_or_bytes)
    return Path(path_or_bytes).read_bytes()


def _cap(text: str) -> tuple[str, bool]:
    """Cap text at MAX_DOC_CHARS. Returns (text, truncated)."""
    if len(text) > MAX_DOC_CHARS:
        return text[:MAX_DOC_CHARS], True
    return text, False


def _docx_to_text(data: bytes) -> str:
    """Convert docx bytes to plain text via macOS textutil.

    textutil detects the format from the file extension, so the bytes are written
    to a temporary .docx first, then converted to txt on stdout.
    """
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            proc = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", tmp.name],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DocumentReadError(f"could not read the .docx file: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        raise DocumentReadError(f"could not read the .docx file: {detail}")
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _pdf_to_text(data: bytes) -> dict | None:
    """Convert pdf bytes to text via pdftotext when it is on PATH.

    Returns the extraction dict when pdftotext is available, or None to signal the
    caller that PDF is unsupported on this machine (UI then suggests md/docx/paste).
    """
    if not shutil.which("pdftotext"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            proc = subprocess.run(
                ["pdftotext", tmp.name, "-"],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DocumentReadError(f"could not read the .pdf file: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        raise DocumentReadError(f"could not read the .pdf file: {detail}")
    text, truncated = _cap((proc.stdout or b"").decode("utf-8", errors="replace"))
    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "pdf_unsupported": False,
    }


def extract_document_text(path_or_bytes, filename: str) -> dict:
    """Read a document into plain text.

    Accepts either raw bytes or a path plus the original filename (its extension
    decides the reader). Returns:

        {text, chars, truncated, pdf_unsupported}

    md/txt are decoded directly (utf-8, replacing undecodable bytes). docx goes
    through macOS textutil. pdf is best effort via pdftotext; when pdftotext is
    absent, text is "" and pdf_unsupported is True so the UI can suggest another
    format. Any other extension raises ValueError.
    """
    ext = _ext_of(filename)
    if ext not in _ALLOWED_DOC_EXTS:
        raise ValueError(
            f"unsupported file type {ext or '(none)'!r}. Use .md, .txt, .docx, or .pdf."
        )
    data = _as_bytes(path_or_bytes, filename)

    if ext in (".md", ".txt"):
        text, truncated = _cap(data.decode("utf-8", errors="replace"))
    elif ext == ".docx":
        text, truncated = _cap(_docx_to_text(data))
    else:  # .pdf
        result = _pdf_to_text(data)
        if result is None:
            return {"text": "", "chars": 0, "truncated": False, "pdf_unsupported": True}
        return result

    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "pdf_unsupported": False,
    }
