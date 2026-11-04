"""Tests for debrief.doctext: the shared document text readers.

Non-live. No real textutil, no real pdftotext: subprocess.run and shutil.which
are monkeypatched. The last two tests pin the compiler's delegation contract, so
the template-import API keeps seeing exactly the errors it already handles
(CompilerError for a failed read, ValueError for an unsupported extension).
"""

from __future__ import annotations

import pytest

from debrief import compiler, doctext


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def test_md_txt_decode():
    out = doctext.extract_document_text(b"# Heading\n\nBody text.", "note.md")
    assert out["text"] == "# Heading\n\nBody text."
    assert out["chars"] == len("# Heading\n\nBody text.")
    assert out["truncated"] is False
    assert out["pdf_unsupported"] is False

    # An invalid utf-8 byte is replaced, never raised.
    txt = doctext.extract_document_text(b"good \xff text", "note.txt")
    assert "good" in txt["text"] and "text" in txt["text"]


def test_docx_via_textutil(monkeypatch):
    seen: dict = {}

    class FakeProc:
        returncode = 0
        stdout = "Subjective\nObjective\nAssessment\nPlan\n".encode("utf-8")
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["capture_output"] = kw.get("capture_output")
        return FakeProc()

    monkeypatch.setattr(doctext.subprocess, "run", fake_run)
    out = doctext.extract_document_text(b"PK\x03\x04fake docx", "template.docx")

    assert seen["cmd"][:4] == ["textutil", "-convert", "txt", "-stdout"]
    assert seen["cmd"][4].endswith(".docx"), "textutil reads the format off the extension"
    assert seen["capture_output"] is True
    assert out["text"] == "Subjective\nObjective\nAssessment\nPlan\n"
    assert out["pdf_unsupported"] is False


def test_docx_failure_raises_document_read_error(monkeypatch):
    class FailedProc:
        returncode = 1
        stdout = b""
        stderr = b"textutil: could not read file"

    monkeypatch.setattr(doctext.subprocess, "run", lambda cmd, **kw: FailedProc())
    with pytest.raises(doctext.DocumentReadError) as excinfo:
        doctext.extract_document_text(b"not really a docx", "template.docx")
    assert "could not read" in str(excinfo.value)


def test_pdf_absent_pdftotext_returns_unsupported(monkeypatch):
    monkeypatch.setattr(doctext.shutil, "which", lambda name: None)
    out = doctext.extract_document_text(b"%PDF-1.4 fake", "template.pdf")
    assert out["pdf_unsupported"] is True
    assert out["text"] == ""
    assert out["chars"] == 0
    assert out["truncated"] is False


def test_unknown_ext_raises_valueerror():
    with pytest.raises(ValueError):
        doctext.extract_document_text(b"data", "note.rtf")
    with pytest.raises(ValueError):
        doctext.extract_document_text(b"data", "no-extension")


def test_cap_at_max_chars():
    big = ("x" * (doctext.MAX_DOC_CHARS + 500)).encode("utf-8")
    out = doctext.extract_document_text(big, "big.txt")
    assert out["truncated"] is True
    assert out["chars"] == doctext.MAX_DOC_CHARS
    assert len(out["text"]) == doctext.MAX_DOC_CHARS


# ---------------------------------------------------------------------------
# The compiler still speaks its old error language
# ---------------------------------------------------------------------------


def test_compiler_wraps_reader_error(monkeypatch):
    class FailedProc:
        returncode = 1
        stdout = b""
        stderr = b"textutil: could not read file"

    monkeypatch.setattr(doctext.subprocess, "run", lambda cmd, **kw: FailedProc())
    # The API layer catches CompilerError, so a reader failure must arrive as one
    # and never as the new DocumentReadError.
    with pytest.raises(compiler.CompilerError):
        compiler.extract_document_text(b"not really a docx", "template.docx")


def test_compiler_unknown_ext_still_valueerror():
    # app.py distinguishes ValueError (unsupported type) from CompilerError, so
    # the ValueError must pass through the delegate untouched.
    with pytest.raises(ValueError) as excinfo:
        compiler.extract_document_text(b"data", "note.rtf")
    assert not isinstance(excinfo.value, compiler.CompilerError)
    assert compiler.MAX_DOC_CHARS == doctext.MAX_DOC_CHARS
    assert compiler._ALLOWED_DOC_EXTS == doctext._ALLOWED_DOC_EXTS
