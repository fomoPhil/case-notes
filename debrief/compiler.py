"""Template import + the prompt compiler.

A professional pastes or uploads one blank example of the note template they
already use, and Debrief derives a matching FormatSpec from it: the section
headings, the house style, whether it is clinical. Two compile paths:

  compile_local   one constrained call to the local Gemma model. Default, fully
                  offline, nothing leaves the Mac.
  compile_gemini  one optional, consented, single-use call to Google Gemini with
                  a bring-your-own key, for a sharper structural read. The key is
                  never persisted, never logged, and exists only inside the
                  request scope. On any failure the UI falls back to local.

Neither path ever copies client or patient content. The model is told to read
only the structure and style, never the words. The derived spec is returned to
the caller for review and is NOT saved here; saving is a separate, explicit step.

dry_run renders the candidate spec against a bundled fictional sample transcript
so the user can see a real note in the new format before committing, with no
vault write.

No em dashes anywhere.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from . import config, extract as extract_mod, formats, llm

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CompilerError(RuntimeError):
    """A compile step failed. Carries an HTTP-ish status for the cloud path.

    status is the upstream HTTP status when a Gemini call returned non-200, else
    None. The message is always safe to surface: any occurrence of the API key
    has been scrubbed before the error is raised.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------

# Only these extensions are accepted for a template upload. docx is read with the
# macOS textutil converter; pdf is best effort via pdftotext when present.
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
            raise CompilerError(f"could not read the .docx file: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        raise CompilerError(f"could not read the .docx file: {detail}")
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
            raise CompilerError(f"could not read the .pdf file: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        raise CompilerError(f"could not read the .pdf file: {detail}")
    text, truncated = _cap((proc.stdout or b"").decode("utf-8", errors="replace"))
    return {
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "pdf_unsupported": False,
    }


def extract_document_text(path_or_bytes, filename: str) -> dict:
    """Read a template document into plain text.

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


# ---------------------------------------------------------------------------
# The format-spec schema and the compile system prompt
# ---------------------------------------------------------------------------

# Strict JSON Schema for the LOCAL (LM Studio json_schema) compile call.
FORMAT_SPEC_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "heading": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["key", "heading", "description"],
            },
        },
        "style_rules": {"type": "string"},
        "clinical": {"type": "boolean"},
    },
    "required": ["name", "sections", "style_rules", "clinical"],
}

# The Gemini responseSchema mirrors FORMAT_SPEC_SCHEMA but also asks for a
# compiled_prompt_layer string (extra note-writing guidance derived from the
# template's style). Gemini's controlled-generation schema is an OpenAPI subset:
# it rejects additionalProperties, so those are omitted here.
_GEMINI_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "heading": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["key", "heading", "description"],
            },
        },
        "style_rules": {"type": "string"},
        "clinical": {"type": "boolean"},
        "compiled_prompt_layer": {"type": "string"},
    },
    "required": [
        "name",
        "sections",
        "style_rules",
        "clinical",
        "compiled_prompt_layer",
    ],
}

_COMPILE_SYSTEM = """You derive a reusable note-format specification from one sample document.

You are given a single example of a professional's note template. Read ONLY its structure and style, never its content. Return a specification with these fields:

- name: a short display name for this note format.
- sections: the note's own sections in order. For each, give a slug-style key (lowercase words joined by underscores), a heading (the section title as a person would read it), and a one-line description of what belongs in that section.
- style_rules: one line describing the tone and structure (for example third person, past tense, clinical prose).
- clinical: true only if this is a clinical or medical note where risk or safety framing could apply, false otherwise.

Hard rules:
- NEVER copy any client name, patient name, personal detail, or sentence content from the document. You are extracting the empty shape of the template, not its words.
- The sections must mirror the document's own headings. Do not invent sections it does not have, and do not drop sections it does have.
- Do not use an em dash anywhere.

Return only the specification object."""

_COMPILE_SYSTEM_GEMINI = _COMPILE_SYSTEM + """

Also return compiled_prompt_layer: a short block of extra note-writing guidance (a few sentences) that captures this template's house style and any conventions worth preserving, phrased as instructions to a documentation assistant. It must not contain any client content."""


# ---------------------------------------------------------------------------
# Post-processing shared by both compile paths
# ---------------------------------------------------------------------------


def _finalize_spec(raw: dict, profession: str = "therapy") -> dict:
    """Turn a raw model spec into a validated, ready-to-save FormatSpec.

    Slugifies each section key, drops reserved and duplicate keys, derives
    risk_section from the clinical flag, and generates a collision-free id from
    the slugified name (checked against every existing builtin and saved custom
    spec). The result is run through the formats validator so a spec that leaves
    this function is always safe to save. Raises CompilerError when no usable
    section survives.
    """
    raw = raw or {}
    name = str(raw.get("name") or "").strip() or "Imported format"
    clinical = bool(raw.get("clinical", False))
    style_rules = str(raw.get("style_rules") or "").strip()

    cleaned: list[dict] = []
    seen: set[str] = set()
    for entry in raw.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        key = formats.slugify_key(entry.get("key") or entry.get("heading") or "")
        if not key or key in formats.RESERVED_KEYS or key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "key": key,
                "heading": str(entry.get("heading") or key.replace("_", " ").title()).strip(),
                # Truncate to the validator's per-description cap so a chatty model
                # response stays save-safe instead of raising on validation.
                "description": str(entry.get("description") or "").strip()[:500],
            }
        )
    if not cleaned:
        raise CompilerError("could not derive any usable sections from the document")
    # Keep at most the validator's section cap so an over-eager model response
    # cannot push the derived spec past validation.
    cleaned = cleaned[:12]

    base_id = formats.slugify_id(name) or "imported-format"
    fid = base_id
    n = 2
    while formats.is_known(fid):
        fid = f"{base_id}-{n}"
        n += 1

    candidate = {
        "id": fid,
        "name": name,
        "clinical": clinical,
        "sections": cleaned,
        "style_rules": style_rules,
        "prompt_guidance": "",
        "risk_section": clinical,
    }
    # Reuse the formats validator so a returned spec is always save-safe.
    return formats._validate_spec(candidate)


# ---------------------------------------------------------------------------
# Local compile (Gemma, offline)
# ---------------------------------------------------------------------------


def compile_local(doc_text: str, profession: str = "therapy") -> dict:
    """Derive a candidate FormatSpec from a document using the local model.

    One constrained llm.chat call. Returns the validated candidate spec dict; it
    is NOT saved. Raises CompilerError when the model output cannot be turned into
    a usable spec.
    """
    messages = [
        {"role": "system", "content": _COMPILE_SYSTEM},
        {
            "role": "user",
            "content": (
                "Here is the sample template document. Derive its note-format "
                "specification, structure and style only:\n\n" + (doc_text or "").strip()
            ),
        },
    ]
    try:
        raw = llm.chat(messages, schema=FORMAT_SPEC_SCHEMA, max_tokens=1500, temperature=0.1)
    except Exception as exc:  # noqa: BLE001
        raise CompilerError(f"local compile failed: {exc}")
    if not isinstance(raw, dict):
        raise CompilerError("local compile returned an unexpected shape")
    return _finalize_spec(raw, profession)


# ---------------------------------------------------------------------------
# Cloud compile (Gemini, optional, consented, single-use key)
# ---------------------------------------------------------------------------

_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _scrub(text: str, secret: str) -> str:
    """Remove every occurrence of the secret from a string. Defensive.

    The key never reaches this module's output on purpose, but if an upstream
    error message ever echoes the key back, this guarantees it cannot ride out in
    an exception, log line, or returned payload.
    """
    if not secret:
        return text
    return (text or "").replace(secret, "[redacted]")


def compile_gemini(doc_text: str, api_key: str) -> dict:
    """Derive a candidate FormatSpec (plus a prompt layer) using Google Gemini.

    Single-use: the api_key is used only inside this call, never persisted, never
    logged, never returned. On non-200 or any parse failure a CompilerError is
    raised with the key scrubbed from its message. Returns {spec, prompt_layer}.
    """
    url = _GEMINI_ENDPOINT.format(model=config.GEMINI_MODEL)
    headers = {
        # The key rides in a header, NEVER a query parameter (query params leak
        # into server logs and proxies).
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            _COMPILE_SYSTEM_GEMINI
                            + "\n\nHere is the sample template document. Derive its "
                            "note-format specification, structure and style only:\n\n"
                            + (doc_text or "").strip()
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": copy.deepcopy(_GEMINI_RESPONSE_SCHEMA),
        },
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        raise CompilerError(_scrub(f"Gemini request failed: {exc}", api_key))

    if resp.status_code != 200:
        trimmed = _scrub((resp.text or "")[:500], api_key)
        raise CompilerError(
            f"Gemini returned {resp.status_code}: {trimmed}", status=resp.status_code
        )

    try:
        payload = resp.json()
        content = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise CompilerError(
            _scrub(f"could not read the Gemini response: {exc}", api_key)
        )

    try:
        raw = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise CompilerError(
            _scrub(f"Gemini did not return valid JSON: {exc}", api_key)
        )
    if not isinstance(raw, dict):
        raise CompilerError("Gemini returned an unexpected shape")

    spec = _finalize_spec(raw, "therapy")
    prompt_layer = str(raw.get("compiled_prompt_layer") or "").strip()
    return {"spec": spec, "prompt_layer": prompt_layer}


# ---------------------------------------------------------------------------
# Dry run: render the candidate spec against a bundled sample transcript
# ---------------------------------------------------------------------------

_SAMPLE_DIR = config.REPO_ROOT / "prompts" / "samples"

# Which bundled fictional transcript and representative framework each profession
# uses for the preview. All transcripts are fictional, no real client content.
_PROFESSION_SAMPLE = {
    "therapy": ("therapy.md", "CBT"),
    "slp": ("slp.md", "ARTICULATION"),
    "coaching": ("coaching.md", "GROW"),
    "legal_meeting": ("legal_meeting.md", "MATTER"),
}


def _sample_transcript(profession: str) -> tuple[str, str]:
    """Return (transcript_text, framework) for a profession, defaulting to therapy."""
    fname, framework = _PROFESSION_SAMPLE.get(
        (profession or "").strip().lower(), _PROFESSION_SAMPLE["therapy"]
    )
    try:
        text = (_SAMPLE_DIR / fname).read_text(encoding="utf-8")
    except OSError:
        text = (_SAMPLE_DIR / "therapy.md").read_text(encoding="utf-8")
        framework = "CBT"
    return text, framework


def dry_run(spec: dict, profession: str = "therapy") -> dict:
    """Render the candidate spec against a bundled sample transcript.

    The spec is passed straight into the extractor as a transient object, so
    nothing is saved to disk and no vault note is written. Returns {note, sections}
    for the UI to render a preview in the same style as the review screen.
    """
    transcript, framework = _sample_transcript(profession)
    result = extract_mod.extract(
        transcript,
        {},
        framework,
        _dt.datetime.now(),
        profession=profession,
        spec=spec,
    )
    return {
        "note": result.get("note", {}),
        "sections": [
            {"key": s["key"], "heading": s["heading"]} for s in spec.get("sections", [])
        ],
    }
