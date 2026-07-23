"""Higher-level client record operations over the vault.

The records UI treats the vault as a client record system, not a file browser.
The app owns filenames and paths so a rename can never break the agent's
[[wikilinks]]. Everything here is path-guarded through vault._safe_join, so a
path coming from the browser can never escape VAULT_DIR.

Document kinds:
  session-note   a DAP note under Clients/<id>/Sessions/*.md (never renamed on
                 disk, never rewritten in place; edits append a dated amendment)
  worksheet-pdf  an agent-made worksheet PDF (has a sibling .md source)
  upload-pdf     an uploaded PDF
  upload-image   an uploaded png/jpg/jpeg
  upload-docx    an uploaded Word document
  markdown       any other markdown document (library worksheet, etc.)

No em dashes anywhere in generated copy.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
from pathlib import Path

from . import render
from .config import VAULT_DIR
from .vault import (
    VaultPathError,
    _atomic_write,
    _dump_frontmatter,
    _safe_join,
    _split_frontmatter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPLOAD_ALLOWLIST = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".md"}
_PROTECTED_NAMES = {"_Profile.md", "Treatment-Plan.md"}
_TRASH_DIRNAME = "_Trash"
_CACHE_DIRNAME = ".cache"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "document"


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(VAULT_DIR.resolve()))
    except ValueError:
        return str(path)


def _guard(rel_path: str) -> Path:
    """Resolve a vault-relative path inside VAULT_DIR, rejecting escapes."""
    return _safe_join(VAULT_DIR, rel_path)


def _valid_client_id(client_id) -> str:
    cid = str(client_id or "").strip()
    if not cid or "/" in cid or "\\" in cid or cid.startswith("."):
        raise VaultPathError(f"invalid client id: {client_id!r}")
    return cid


def _client_dir(client_id: str) -> Path:
    return VAULT_DIR / "Clients" / _valid_client_id(client_id)


def _documents_dir(client_id: str, create: bool = False) -> Path:
    d = _client_dir(client_id) / "Documents"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _mtime_iso(path: Path) -> str:
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0).isoformat()
    except OSError:
        return ""


def _date_display(value) -> str:
    """'Jul 18' style short date from an ISO date/datetime string."""
    s = str(value or "")[:10]
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return f"{_MONTHS[m - 1]} {d}"
    except (ValueError, IndexError):
        return ""


def _kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        if path.parent.name == "Sessions":
            return "session-note"
        return "markdown"
    if suffix == ".pdf":
        # Agent worksheets ship a sibling markdown source; bare PDFs are uploads.
        if path.with_suffix(".md").exists():
            return "worksheet-pdf"
        return "upload-pdf"
    if suffix in (".png", ".jpg", ".jpeg"):
        return "upload-image"
    if suffix == ".docx":
        return "upload-docx"
    return "markdown"


def _first_h1(body: str) -> str | None:
    m = _H1_RE.search(body or "")
    return m.group(1).strip() if m else None


def _section_text(body: str, heading: str) -> str:
    """Return the plain text under a '## <heading>' section, first paragraph."""
    lines = (body or "").splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## "):
            if capturing:
                break
            capturing = stripped[3:].strip().lower() == heading.lower()
            continue
        if capturing and stripped:
            out.append(stripped)
    return " ".join(out).strip()


def _preview(body: str, width: int = 180) -> str:
    text = _section_text(body, "Data") or " ".join((body or "").split())
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "..."


# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------


def _session_meta(path: Path) -> dict:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    number = fm.get("session_number")
    fmt = fm.get("format", "DAP")
    default_title = (
        f"Session {number}, {fmt} note" if number else f"{fmt} note"
    )
    title = fm.get("title") or default_title
    risk = str(fm.get("risk_assessment", "none-discussed"))
    risk_flag = risk not in ("", "none", "none-discussed")
    return {
        "path": _rel(path),
        "title": title,
        "kind": "session-note",
        "date": str(fm.get("session_date") or "")[:10],
        "date_display": _date_display(fm.get("session_date")),
        "session_number": number,
        "format": fmt,
        "risk_flag": risk_flag,
        "filed": True,
        "preview": _preview(body),
        "modified": _mtime_iso(path),
    }


def _document_meta(path: Path) -> dict:
    kind = _kind_for(path)
    fm: dict = {}
    if path.suffix.lower() == ".md":
        try:
            fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            body = ""
        title = fm.get("title") or _first_h1(body) or path.stem.replace("-", " ").title()
    else:
        title = path.stem.replace("-", " ").title()
    is_agent = kind == "worksheet-pdf" or (kind == "markdown" and bool(fm.get("agent_made")))
    return {
        "path": _rel(path),
        "title": str(title),
        "kind": kind,
        "date": _mtime_iso(path)[:10],
        "date_display": _date_display(_mtime_iso(path)),
        "agent_made": is_agent,
        "modified": _mtime_iso(path),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_sessions(client_id: str) -> list[dict]:
    """Session-note metadata for a client, newest first."""
    sessions = _client_dir(client_id) / "Sessions"
    if not sessions.exists():
        return []
    metas = [_session_meta(p) for p in sessions.glob("*.md") if p.is_file()]
    metas.sort(key=lambda m: (m.get("date") or "", m.get("path")), reverse=True)
    return metas


def list_documents(client_id: str) -> list[dict]:
    """Document metadata for a client's Documents/ folder (created lazily)
    plus the session notes, matching the plan's combined display list."""
    docs_dir = _documents_dir(client_id, create=True)
    docs: list[dict] = []
    for p in sorted(docs_dir.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            docs.append(_document_meta(p))
    docs.sort(key=lambda m: m.get("modified") or "", reverse=True)
    return list_sessions(client_id) + docs


def read_note(rel_path: str) -> dict:
    """Path-guarded read of a markdown note. Returns markdown, frontmatter, kind."""
    target = _guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"No such note: {rel_path}")
    if target.suffix.lower() != ".md":
        raise VaultPathError(f"not a markdown note: {rel_path}")
    text = target.read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(text)
    return {"markdown": text, "frontmatter": fm, "kind": _kind_for(target)}


def get_library() -> dict:
    """Templates/Worksheets/* and Interventions/* display metadata."""
    worksheets: list[dict] = []
    reference: list[dict] = []
    ws_dir = VAULT_DIR / "Templates" / "Worksheets"
    if ws_dir.exists():
        seen: set[str] = set()
        for p in sorted(ws_dir.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            # Collapse a worksheet's .md + .pdf pair into one card (prefer .md).
            if p.suffix.lower() == ".pdf" and p.with_suffix(".md").exists():
                continue
            stem = p.stem
            if stem in seen:
                continue
            seen.add(stem)
            worksheets.append(_document_meta(p))
    ref_dir = VAULT_DIR / "Interventions"
    if ref_dir.exists():
        for p in sorted(ref_dir.iterdir()):
            if p.is_file() and p.suffix.lower() == ".md" and not p.name.startswith("."):
                reference.append(_document_meta(p))
    return {"worksheets": worksheets, "reference": reference}


# ---------------------------------------------------------------------------
# Amendments (filed session notes are never rewritten)
# ---------------------------------------------------------------------------


def append_amendment(rel_path: str, text: str, now: _dt.datetime | None = None) -> str:
    """Append a dated '## Amendment' section to a markdown note. Returns rel path."""
    target = _guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"No such note: {rel_path}")
    if target.suffix.lower() != ".md":
        raise VaultPathError("amendments are only allowed on markdown notes")
    body = (text or "").strip()
    if not body:
        raise ValueError("amendment text is empty")
    now = now or _dt.datetime.now()
    existing = target.read_text(encoding="utf-8").rstrip("\n")
    stamp = now.date().isoformat()
    addition = f"\n\n## Amendment ({stamp})\n\n{body}\n"
    _atomic_write(target, existing + addition)
    return _rel(target)


# ---------------------------------------------------------------------------
# Rename (app owns filenames)
# ---------------------------------------------------------------------------


def _rewrite_links(client_dir: Path, old_stem: str, new_stem: str) -> None:
    """Rewrite [[wikilinks]] and ![[embeds]] referencing old_stem across the
    client folder and _Activity/, matched on the file stem."""
    if old_stem == new_stem:
        return
    roots = [client_dir, VAULT_DIR / "_Activity"]
    # Match the stem when it appears as a whole path segment inside [[ ]].
    pattern = re.compile(r"(!?\[\[)([^\]]*?)(\]\])")

    def _sub_target(target: str) -> str:
        # target may be "Path/To/stem" or "stem|Alias"; only swap the stem.
        link, _, alias = target.partition("|")
        segs = link.split("/")
        if segs and segs[-1] == old_stem:
            segs[-1] = new_stem
            link = "/".join(segs)
            return f"{link}|{alias}" if alias else link
        return target

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            new_text = pattern.sub(lambda m: m.group(1) + _sub_target(m.group(2)) + m.group(3), text)
            if new_text != text:
                _atomic_write(path, new_text)


def _unique_sibling(directory: Path, stem: str, suffix: str, exclude: Path | None = None) -> Path:
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists() or candidate == exclude:
        return candidate
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists() or candidate == exclude:
            return candidate
        n += 1


def rename_title(rel_path: str, new_title: str) -> str:
    """Rename the display title of a note or document.

    Session notes keep their filename forever; only the frontmatter title
    changes. Documents get both a new frontmatter title (markdown) and a new
    slugged filename, with wikilinks/embeds rewritten across the client folder
    and _Activity/. Returns the (possibly new) vault-relative path.
    """
    target = _guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {rel_path}")
    title = (new_title or "").strip()
    if not title:
        raise ValueError("new title is empty")

    suffix = target.suffix.lower()
    is_session = target.parent.name == "Sessions" and suffix == ".md"

    # Session notes: change only the title, never the filename.
    if is_session:
        fm, body = _split_frontmatter(target.read_text(encoding="utf-8"))
        fm["title"] = title
        _atomic_write(target, _dump_frontmatter(fm) + "\n" + body)
        return _rel(target)

    old_stem = target.stem
    new_stem = _slug(title)

    if suffix == ".md":
        fm, body = _split_frontmatter(target.read_text(encoding="utf-8"))
        if fm:
            fm["title"] = title
            content = _dump_frontmatter(fm) + "\n" + body
        elif _first_h1(body):
            content = _H1_RE.sub(f"# {title}", body, count=1)
        else:
            content = f"# {title}\n\n{body}"
        _atomic_write(target, content)

    new_path = _unique_sibling(target.parent, new_stem, target.suffix, exclude=target)
    new_stem = new_path.stem
    if new_path != target:
        os.replace(target, new_path)
        # A worksheet PDF's sibling markdown source rides along.
        sibling = target.with_suffix(".md")
        if suffix == ".pdf" and sibling.exists():
            os.replace(sibling, new_path.with_suffix(".md"))

    # Rewrite links inside the owning client folder (best effort) and _Activity.
    client_dir = target
    for parent in target.parents:
        if parent.parent.name == "Clients":
            client_dir = parent
            break
    _rewrite_links(client_dir, old_stem, new_stem)
    return _rel(new_path)


# ---------------------------------------------------------------------------
# Trash / restore / sweep
# ---------------------------------------------------------------------------


def _trash_root() -> Path:
    return VAULT_DIR / _TRASH_DIRNAME


def _token_from_now(now: _dt.datetime) -> str:
    # Filesystem-safe, sortable, unique enough for interactive use.
    return now.strftime("%Y-%m-%dT%H-%M-%S-%f")


def trash(rel_path: str, now: _dt.datetime | None = None) -> str:
    """Move a file into _Trash/<token>/ with a .trashmeta.json. Returns token.

    Rejects the protected _Profile.md and Treatment-Plan.md files.
    """
    target = _guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {rel_path}")
    if target.name in _PROTECTED_NAMES:
        raise VaultPathError(f"{target.name} cannot be moved to trash")
    now = now or _dt.datetime.now()
    original_rel = _rel(target)
    token = _token_from_now(now)
    bucket = _trash_root() / token
    dest = bucket / original_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, dest)
    # A worksheet PDF carries its markdown source into the trash too.
    if target.suffix.lower() == ".pdf" and target.with_suffix(".md").exists():
        sib = target.with_suffix(".md")
        sib_dest = bucket / _rel(sib)
        sib_dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(sib, sib_dest)
    meta = {"original": original_rel, "trashed_at": now.replace(microsecond=0).isoformat()}
    _atomic_write(bucket / ".trashmeta.json", json.dumps(meta, indent=2))
    return token


def restore(token: str) -> str:
    """Restore a trashed file to its original location. Returns the rel path."""
    tok = str(token or "").strip()
    if not tok or "/" in tok or "\\" in tok or tok.startswith("."):
        raise VaultPathError(f"invalid trash token: {token!r}")
    bucket = _trash_root() / tok
    meta_path = bucket / ".trashmeta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"No trash record for token {tok}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    original_rel = meta["original"]
    dest = _guard(original_rel)
    src = bucket / original_rel
    if not src.exists():
        raise FileNotFoundError(f"Trashed file is missing for token {tok}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    # Restore a worksheet's markdown source if it was trashed alongside.
    if dest.suffix.lower() == ".pdf":
        sib_src = bucket / str(Path(original_rel).with_suffix(".md"))
        if sib_src.exists():
            os.replace(sib_src, dest.with_suffix(".md"))
    shutil.rmtree(bucket, ignore_errors=True)
    return _rel(dest)


def list_trash() -> list[dict]:
    """List trashed items: {token, original, title, trashed_at}."""
    root = _trash_root()
    if not root.exists():
        return []
    items: list[dict] = []
    for bucket in sorted(root.iterdir(), reverse=True):
        meta_path = bucket / ".trashmeta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        original = meta.get("original", "")
        items.append(
            {
                "token": bucket.name,
                "original": original,
                "title": Path(original).stem.replace("-", " ").title(),
                "trashed_at": meta.get("trashed_at", ""),
            }
        )
    return items


def sweep_trash(now: _dt.datetime | None = None, days: int = 30) -> int:
    """Remove trash buckets older than `days`. Returns the count removed."""
    root = _trash_root()
    if not root.exists():
        return 0
    now = now or _dt.datetime.now()
    cutoff = now - _dt.timedelta(days=days)
    removed = 0
    for bucket in root.iterdir():
        if not bucket.is_dir():
            continue
        meta_path = bucket / ".trashmeta.json"
        trashed_at = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                trashed_at = _dt.datetime.fromisoformat(meta.get("trashed_at", ""))
            except (OSError, ValueError):
                trashed_at = None
        if trashed_at is None:
            try:
                trashed_at = _dt.datetime.fromtimestamp(bucket.stat().st_mtime)
            except OSError:
                continue
        if trashed_at < cutoff:
            shutil.rmtree(bucket, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def save_upload(client_id: str, filename: str, data: bytes) -> dict:
    """Save an uploaded file into Clients/<id>/Documents/. Returns its meta.

    Allowlist: .pdf .png .jpg .jpeg .docx .md. The filename is slugged and the
    original extension kept; collisions get a -2, -3 suffix.
    """
    cid = _valid_client_id(client_id)
    name = Path(str(filename or "")).name
    suffix = Path(name).suffix.lower()
    if suffix not in _UPLOAD_ALLOWLIST:
        raise VaultPathError(f"file type not allowed: {suffix or '(none)'}")
    if not data:
        raise ValueError("empty upload")
    stem = _slug(Path(name).stem)
    docs_dir = _documents_dir(cid, create=True)
    dest = _unique_sibling(docs_dir, stem, suffix)
    # Guard: the resolved destination must remain inside the vault.
    _safe_join(VAULT_DIR, _rel(dest))
    dest.write_bytes(data)
    return _document_meta(dest)


# ---------------------------------------------------------------------------
# Rendered-PDF cache for markdown notes
# ---------------------------------------------------------------------------


def _cache_pdf_path(rel_path: str) -> Path:
    mirrored = Path(rel_path)
    return VAULT_DIR / _CACHE_DIRNAME / "pdf" / mirrored.with_suffix(".pdf")


def render_cache_pdf(rel_path: str) -> Path:
    """Render a markdown note to a cached PDF, re-rendering when stale.

    Returns the cached PDF path. Raises render.PdfUnavailable when WeasyPrint
    cannot render on this machine.
    """
    target = _guard(rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"No such note: {rel_path}")
    if target.suffix.lower() != ".md":
        raise VaultPathError("only markdown notes can be rendered to PDF")
    cache = _cache_pdf_path(rel_path)
    fresh = (
        cache.exists()
        and cache.stat().st_mtime >= target.stat().st_mtime
    )
    if fresh:
        return cache
    fm, body = _split_frontmatter(target.read_text(encoding="utf-8"))
    title = str(fm.get("title") or _first_h1(body) or target.stem.replace("-", " ").title())
    # Render the body only; frontmatter is metadata, not document content.
    render.render_pdf(body, title, cache)
    return cache
