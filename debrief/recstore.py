"""Where session recordings live on disk.

One tree, at the vault root beside Clients/, holding everything a session source
produces:

    <vault>/Recordings/<client_id>/<source_id>/
        source.m4a          rung 0 audio, in the container it arrived in
        transcript-raw.md   rung 0, straight out of speech to text
        transcript.md       rung 1, corrected
        digest.json         rung 2, the machine artifact code reads
        digest.md           rung 2, rendered for a human, generated from digest.json
        source-text.md      the document text, for a source that enters at rung 2
        meta.json           provenance: kind, verbatim, produced_by, captured_at

Media is large and the clinical text folders stay lean and greppable, so this
tree is separate from Clients/. It is also one directory to exclude from cloud
sync or delete wholesale. Nothing here ever touches the psychotherapy-notes
folder that the vault keeps walled off from every model and every feature.

Every write is atomic (temp file plus os.replace, the same helper the vault uses),
so an interrupted write can never leave a reader a half file. The rungs are their
own checkpoint: a job that dies partway resumes by building the first rung that
is missing, which is why write_rung lands digest.md before digest.json, the file
that marks rung 2 as done.

config.VAULT_DIR is read at call time, never bound at import, so a test that
repoints the vault repoints this module with it.

Two deletions, deliberately different:

  delete_audio        permanent, and audio only. Audio is bulky and expendable;
                      the transcripts and the digest are the evidence trail and
                      they stay. sweep_retention is this, on a timer.
  delete_source       the whole folder, through the vault trash, so it keeps the
                      existing 30-day grace period.

No em dashes anywhere.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import config, records, settings_store
from .vault import _atomic_write

# The file whose presence means a rung is finished. digest.md is a rendering of
# digest.json, so it is not the marker for rung 2.
RUNG_FILES = {0: "transcript-raw.md", 1: "transcript.md", 2: "digest.json"}

_DIGEST_MD = "digest.md"
_SOURCE_TEXT = "source-text.md"
_META = "meta.json"
_AUDIO_STEM = "source"

# A client id or source id is exactly one path segment: it starts with a letter
# or digit and continues with letters, digits, dot, dash, or underscore. No
# separator, no leading dot, and ".." is rejected outright, so a constructed path
# can never climb out of Recordings/.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# An audio container is a short alphanumeric extension. Anything else is refused
# rather than turned into a filename.
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}")

# The settings key for the retention window, in days. 0 means keep forever.
_RETENTION_KEY = "recording_retention_days"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _valid_id(value, label: str) -> str:
    """Return value as a safe single path segment, else raise ValueError."""
    text = "" if value is None else str(value)
    if not _ID_RE.fullmatch(text) or ".." in text:
        raise ValueError(f"invalid {label}: {value!r}")
    return text


def recordings_root() -> Path:
    """The Recordings/ tree for the currently configured vault."""
    return config.VAULT_DIR / "Recordings"


def source_dir(client_id: str, source_id: str) -> Path:
    """The folder for one source. Validates both ids and creates nothing.

    Raises ValueError when either id is not a safe single path segment, so a
    caller can never talk this module into writing outside Recordings/.
    """
    return recordings_root() / _valid_id(client_id, "client id") / _valid_id(source_id, "source id")


def _existing_source_dir(client_id: str, source_id: str) -> Path | None:
    """The source folder if it exists on disk, else None."""
    directory = source_dir(client_id, source_id)
    return directory if directory.is_dir() else None


def _atomic_copy(src: Path, dest: Path) -> None:
    """Copy bytes into dest atomically: temp file in the same folder, then replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def write_rung(
    client_id: str,
    source_id: str,
    rung: int,
    content: str,
    rendered_md: str | None = None,
) -> Path:
    """Write one rung of the ladder atomically. Returns the file written.

    rung 0 is the raw transcript, rung 1 the corrected one, rung 2 the digest.
    For rung 2, content is the digest JSON string and rendered_md is its human
    rendering; rendered_md is required there and its absence raises ValueError
    before anything is written. digest.md lands first so digest.json, the file
    that marks the rung complete, is never the one left without its partner.
    """
    if rung not in RUNG_FILES:
        raise ValueError(f"unknown rung: {rung!r}. Rungs are 0, 1, and 2.")
    if rung == 2 and rendered_md is None:
        raise ValueError("rung 2 needs rendered_md: digest.md is written alongside digest.json")

    directory = source_dir(client_id, source_id)
    directory.mkdir(parents=True, exist_ok=True)
    if rung == 2:
        _atomic_write(directory / _DIGEST_MD, rendered_md or "")
    path = directory / RUNG_FILES[rung]
    _atomic_write(path, content or "")
    return path


def write_audio(client_id: str, source_id: str, src_path: Path) -> Path:
    """Copy an audio file in as source.<original ext>. Returns the stored path.

    The container is kept as it arrived so the archive stays byte-faithful. Any
    previously stored audio in a different container is removed after the new
    file lands, so a source always has exactly one source.* file.
    """
    src = Path(src_path)
    ext = src.suffix.lower()
    if not _EXT_RE.fullmatch(ext):
        raise ValueError(f"audio file has no usable extension: {src.name!r}")

    directory = source_dir(client_id, source_id)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{_AUDIO_STEM}{ext}"
    _atomic_copy(src, dest)
    for stale in directory.glob(f"{_AUDIO_STEM}.*"):
        if stale != dest and stale.is_file():
            stale.unlink()
    return dest


def write_source_text(client_id: str, source_id: str, text: str) -> Path:
    """Write source-text.md, the document text of a source that enters at rung 2."""
    directory = source_dir(client_id, source_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _SOURCE_TEXT
    _atomic_write(path, text or "")
    return path


def write_meta(client_id: str, source_id: str, meta: dict) -> Path:
    """Write meta.json, the source's provenance record."""
    directory = source_dir(client_id, source_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _META
    _atomic_write(path, json.dumps(meta or {}, indent=2, default=str) + "\n")
    return path


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_meta(client_id: str, source_id: str) -> dict | None:
    """The source's provenance dict, or None when it is missing or unreadable.

    A malformed meta.json reads as absent rather than raising: a listing must
    never be taken down by one bad file.
    """
    path = source_dir(client_id, source_id) / _META
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_rung(client_id: str, source_id: str, rung: int) -> str | None:
    """The text of one rung, or None when that rung has not been built."""
    if rung not in RUNG_FILES:
        raise ValueError(f"unknown rung: {rung!r}. Rungs are 0, 1, and 2.")
    path = source_dir(client_id, source_id) / RUNG_FILES[rung]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_source_text(client_id: str, source_id: str) -> str | None:
    """The document text of a source, or None when there is none stored.

    This is the whole input for a source that enters at rung 2: a meeting tool's
    write up or the clinician's own notes, where there is no transcript to build
    because nobody said those words.
    """
    path = source_dir(client_id, source_id) / _SOURCE_TEXT
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def audio_path(client_id: str, source_id: str) -> Path | None:
    """The stored source.* audio file, or None when there is none."""
    directory = source_dir(client_id, source_id)
    for path in sorted(directory.glob(f"{_AUDIO_STEM}.*")):
        if path.is_file():
            return path
    return None


def rungs_present(client_id: str, source_id: str) -> set[int]:
    """Which rungs are already built for this source."""
    directory = source_dir(client_id, source_id)
    return {rung for rung, name in RUNG_FILES.items() if (directory / name).is_file()}


def list_sources(client_id: str | None = None) -> list[dict]:
    """Every source, newest first, optionally narrowed to one client.

    Each entry is the source's meta dict plus client_id, source_id, rungs (a
    sorted list), and has_audio. A folder without a readable meta.json is not a
    source yet, so it is skipped rather than half-reported.
    """
    root = recordings_root()
    if not root.is_dir():
        return []

    if client_id is None:
        client_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    else:
        target = root / _valid_id(client_id, "client id")
        client_dirs = [target] if target.is_dir() else []

    rows: list[dict] = []
    for client_dir in client_dirs:
        try:
            cid = _valid_id(client_dir.name, "client id")
        except ValueError:
            continue
        for source in sorted(p for p in client_dir.iterdir() if p.is_dir()):
            try:
                sid = _valid_id(source.name, "source id")
            except ValueError:
                continue
            meta = read_meta(cid, sid)
            if meta is None:
                continue
            entry = dict(meta)
            entry.update(
                {
                    "client_id": cid,
                    "source_id": sid,
                    "rungs": sorted(rungs_present(cid, sid)),
                    "has_audio": audio_path(cid, sid) is not None,
                }
            )
            rows.append(entry)

    rows.sort(key=lambda row: (row["source_id"], row["client_id"]), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_audio(client_id: str, source_id: str) -> bool:
    """Permanently remove a source's audio. Returns False when there is none.

    Audio only. The transcripts, the digest, the source text, and the meta are
    the evidence trail and are never touched here, which is what makes losing the
    audio survivable.
    """
    path = audio_path(client_id, source_id)
    if path is None:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def delete_rung(client_id: str, source_id: str, rung: int) -> bool:
    """Remove one built rung. Returns False when it was not there.

    Only for rungs that can be built again from what is still on disk, which is
    what makes this safe: rung 1 is rebuilt from rung 0 and rung 2 from rung 1
    or the source text. The originals, source.* and source-text.md, are never
    touched here, and there is no helper that removes them: losing the material
    itself is delete_audio or delete_source, both of which say so.

    The caller is the confirm flow. When a clinician answers "this is an AI
    write up, not word for word", a digest built under the old provenance is
    stale evidence, and stale evidence in a clinical record is worse than no
    evidence. Rung 2 goes as a pair, digest.json and its digest.md rendering, so
    the human view can never outlive the artifact it was rendered from.
    """
    if rung not in RUNG_FILES:
        raise ValueError(f"unknown rung: {rung!r}. Rungs are 0, 1, and 2.")
    directory = source_dir(client_id, source_id)
    names = [RUNG_FILES[rung]] + ([_DIGEST_MD] if rung == 2 else [])
    removed = False
    for name in names:
        path = directory / name
        try:
            path.unlink()
            removed = True
        except OSError:
            continue
    return removed


def delete_source(client_id: str, source_id: str) -> bool:
    """Move a whole source folder to the vault trash. False when it is not there.

    Through records.trash, so it keeps the existing 30-day retention and can be
    restored whole.
    """
    cid = _valid_id(client_id, "client id")
    sid = _valid_id(source_id, "source id")
    if _existing_source_dir(cid, sid) is None:
        return False
    records.trash(f"Recordings/{cid}/{sid}")
    return True


def delete_client_sources(client_id: str) -> int:
    """Move a client's whole Recordings folder to the trash. Returns how many
    sources it held, or 0 when the client has none.

    This is the cascade a client deletion needs, and the path most likely to be
    forgotten: deleting a client must not leave their recordings behind.
    """
    cid = _valid_id(client_id, "client id")
    client_dir = recordings_root() / cid
    if not client_dir.is_dir():
        return 0
    count = sum(1 for p in client_dir.iterdir() if p.is_dir())
    records.trash(f"Recordings/{cid}")
    return count


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _parse_captured_at(value, now: _dt.datetime) -> _dt.datetime | None:
    """Parse a meta captured_at into a datetime comparable with now, else None.

    A stored timestamp may or may not carry a timezone, and mixing the two raises
    rather than compares, so both sides are brought to the same awareness before
    the comparison. Anything unparseable returns None, which means skip.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        captured = _dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if (captured.tzinfo is None) == (now.tzinfo is None):
        return captured
    if captured.tzinfo is None:
        return captured.astimezone()
    return captured.astimezone().replace(tzinfo=None)


def sweep_retention(now: _dt.datetime) -> int:
    """Delete audio older than the configured retention window. Returns the count.

    settings_store's recording_retention_days sets the window; 0, the default,
    means keep until the clinician deletes it and this does nothing. Only
    source.* files are removed. transcript-raw.md, transcript.md, digest.json,
    digest.md, source-text.md, and meta.json are never touched, so the note's
    evidence trail outlives its audio. Each deletion is recorded as
    audio_deleted_at in the source's meta, so the UI can say what happened rather
    than showing an unexplained gap.

    Deliberately timid: a source whose captured_at is missing or unparseable is
    skipped, never deleted. An unknown age is not an expired one.
    """
    try:
        days = int(settings_store.load().get(_RETENTION_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0

    cutoff = now - _dt.timedelta(days=days)
    stamp = now.replace(microsecond=0).isoformat()
    removed = 0
    for entry in list_sources():
        captured = _parse_captured_at(entry.get("captured_at"), now)
        if captured is None or captured >= cutoff:
            continue
        cid, sid = entry["client_id"], entry["source_id"]
        if not delete_audio(cid, sid):
            continue
        # Only annotate meta that can still be read. Writing a fresh dict here
        # would replace the source's whole provenance with one timestamp.
        meta = read_meta(cid, sid)
        if meta is not None:
            meta["audio_deleted_at"] = stamp
            write_meta(cid, sid, meta)
        removed += 1
    return removed
