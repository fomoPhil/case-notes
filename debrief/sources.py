"""Get session material in, work out what it actually is, and record where it came from.

Material arrives as an audio file, a video file, a transcript export, a meeting
tool's write up, or the clinician's own notes. Each one enters the ladder at a
different rung and each one carries a different claim about truth, so every
source is stored with its provenance beside it in meta.json.

The claim that matters is `verbatim`. An Otter export may be a word for word
transcript, Otter's AI summary of the session, or both stapled together. A
Granola note is almost entirely generated interpretation. If a generated summary
is ingested as verbatim, a paraphrased or invented sentence eventually lands in a
clinical record as the client's own quote, and it stays there. So:

    verbatim=True  always pairs with entry_rung 0, the archive of what was said
    verbatim=False always pairs with entry_rung 2, an observation about it

The Source constructor enforces that pairing. It is not a convention.

Classification is layered, deterministic, and never a model call: file headers
first, then export structure, then speaker turn shape, then prose. Every layer
returns a confidence, and anything under CONFIRM_BELOW is stored with its best
guess and flagged for the clinician to confirm with one tap. When a file looks
like both a summary and a transcript, the guess is the non-verbatim one, because
an unattributed observation is recoverable and a misattributed quote is not.

Consent comes first on every path. Import is gated on a dated recording consent
flag on the client profile, exactly as the record button is. The message when it
is missing says what is missing and nothing else: the app takes no position on
recording law and offers no legal advice.

Writes go through recstore, so everything lands under Recordings/<client>/<source>
and nowhere else in the vault. Video is the one input that is never retained: the
audio track is extracted to a temp file, stored, and the temp file is removed.

No em dashes anywhere.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import doctext, recstore, vault


class SourceError(RuntimeError):
    """Session material could not be brought in. The message is safe to show."""


class ConsentError(SourceError):
    """No dated recording consent is on file for this client."""


# What the clinician sees when consent is missing. It names the missing thing and
# stops. No jurisdictions, no law, no advice.
CONSENT_MESSAGE = (
    "Recording consent has not been recorded for this client. Add it on their "
    "profile, then bring the session material in."
)

# Below this, the classification is a guess and the clinician is asked to confirm.
CONFIRM_BELOW = 0.8

# Every kind, with the two facts that follow from it: is it word for word, and
# which rung of the ladder does it enter at. One table, so a kind can never be
# stored with a pairing that contradicts it.
KIND_RULES: dict[str, tuple[bool, int]] = {
    "live_recording": (True, 0),
    "uploaded_audio": (True, 0),
    "uploaded_video": (True, 0),
    "transcript_file": (True, 0),
    "pasted_transcript": (True, 0),
    "third_party_summary": (False, 2),
    "clinician_notes": (False, 2),
}

PRODUCERS = {"debrief", "zoom", "otter", "granola", "clinician", "unknown"}

# What a file's extension alone is enough to decide.
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}
TRANSCRIPT_EXTS = {".vtt", ".srt"}
DOC_EXTS = {".txt", ".md", ".pdf", ".docx"}

# The two kinds a clinician can paste in directly.
PASTE_KINDS = {"pasted_transcript", "clinician_notes"}

# The kinds a file of text can honestly be. The rest describe media, and a
# document declared as one of those would enter a rung expecting audio.
TEXT_KINDS = {
    "transcript_file",
    "pasted_transcript",
    "third_party_summary",
    "clinician_notes",
}


# ---------------------------------------------------------------------------
# The Source record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One piece of session material, with the provenance that governs its use.

    Frozen, because provenance that can be edited in place is provenance that can
    drift away from the bytes it describes. A correction after the clinician
    confirms a classification rewrites meta.json with a new Source.
    """

    id: str
    client_id: str
    kind: str
    verbatim: bool
    produced_by: str
    entry_rung: int
    captured_at: _dt.datetime
    duration_min: int | None
    diarized: bool
    partial: bool
    audio_path: Path | None
    consent_ref: str | None

    def __post_init__(self) -> None:
        if self.kind not in KIND_RULES:
            raise ValueError(
                f"unknown source kind: {self.kind!r}. "
                f"Known kinds are {', '.join(sorted(KIND_RULES))}."
            )
        expected_verbatim, expected_rung = KIND_RULES[self.kind]
        if bool(self.verbatim) is not expected_verbatim:
            raise ValueError(
                f"{self.kind} is always verbatim={expected_verbatim}. "
                "A source's kind decides whether it is word for word."
            )
        if self.entry_rung != expected_rung:
            raise ValueError(
                f"{self.kind} always enters at rung {expected_rung}, not "
                f"{self.entry_rung!r}. Verbatim material is rung 0, an "
                "observation about it is rung 2."
            )

    def to_meta(self) -> dict:
        """A JSON safe dict for meta.json: ISO datetime, path as a string."""
        return {
            "id": self.id,
            "client_id": self.client_id,
            "kind": self.kind,
            "verbatim": bool(self.verbatim),
            "produced_by": self.produced_by,
            "entry_rung": int(self.entry_rung),
            "captured_at": self.captured_at.replace(microsecond=0).isoformat(),
            "duration_min": self.duration_min,
            "diarized": bool(self.diarized),
            "partial": bool(self.partial),
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "consent_ref": self.consent_ref,
        }

    @classmethod
    def from_meta(cls, meta: dict) -> Source:
        """Rebuild a Source from meta.json. Raises ValueError on a broken record.

        recstore.list_sources adds source_id to each row, so either spelling of
        the id is accepted.
        """
        meta = meta or {}
        captured = meta.get("captured_at")
        if isinstance(captured, _dt.datetime):
            captured_at = captured
        else:
            try:
                captured_at = _dt.datetime.fromisoformat(str(captured))
            except ValueError:
                raise ValueError(f"unreadable captured_at: {captured!r}")
        duration = meta.get("duration_min")
        audio = meta.get("audio_path")
        return cls(
            id=str(meta.get("id") or meta.get("source_id") or ""),
            client_id=str(meta.get("client_id") or ""),
            kind=str(meta.get("kind") or ""),
            verbatim=bool(meta.get("verbatim")),
            produced_by=str(meta.get("produced_by") or "unknown"),
            entry_rung=int(meta.get("entry_rung", -1)),
            captured_at=captured_at,
            duration_min=int(duration) if duration is not None else None,
            diarized=bool(meta.get("diarized")),
            partial=bool(meta.get("partial")),
            audio_path=Path(audio) if audio else None,
            consent_ref=meta.get("consent_ref") or None,
        )


# ---------------------------------------------------------------------------
# Timestamps and turns
# ---------------------------------------------------------------------------

# "00:01:30.400", "01:30,400", and "1:30" all parse. Cue lines use the arrow.
_ARROW_RE = re.compile(r"^\s*(?P<start>[\d:.,]+)\s*-->\s*(?P<end>[\d:.,]+)")

# A WebVTT voice span: <v Dana Ruiz>text</v>.
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[a-z][^>]*>", re.IGNORECASE)

# A speaker label at the head of a line: "Dana Ruiz: text", "Speaker 1: text".
_SPEAKER_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 .'\-]{0,38}):[ \t]+(?P<text>\S.*)$")

# An Otter style utterance header, on its own line:
#   "Dana Ruiz  0:03"        name, whitespace, then the timestamp
#   "Speaker 1 (00:03):"     the parenthesised variant
_OTTER_HEADER_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 .'\-]{0,38}?)"
    r"(?:[ \t]{1,}(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)"
    r"|[ \t]*\((?P<ts2>\d{1,2}:\d{2}(?::\d{2})?)\):)"
    r"[ \t]*$"
)

_INT_LINE_RE = re.compile(r"^\s*\d+\s*$")

# A SubRip cue line, in full: 00:00:03,480 --> 00:00:08,120. Nothing looser, so
# an arrow inside prose can never pass for one.
_SRT_CUE_RE = re.compile(
    r"^\s*\d{1,3}:\d{2}:\d{2},\d{1,3}\s*-->\s*\d{1,3}:\d{2}:\d{2},\d{1,3}"
)

# Headings an AI meeting tool puts on a generated write up. Otter, Granola,
# Fathom, and Fireflies all reach for the same handful of words, and none of them
# appear above an utterance in a verbatim export. Kept broad on purpose: a false
# positive here costs one confirmation tap, and a false negative files generated
# prose as somebody's words.
_SUMMARY_HEADINGS = {
    "action items",
    "chapters",
    "highlights",
    "key takeaways",
    "keywords",
    "meeting purpose",
    "next steps",
    "notes",
    "outline",
    "overview",
    "summary",
    "topics",
}


def _seconds(token: str) -> float | None:
    """Parse "00:01:30.400", "01:30,400", or "1:30" into seconds."""
    text = (token or "").strip().replace(",", ".")
    parts = text.split(":")
    if not text or len(parts) > 3:
        return None
    total = 0.0
    for part in parts:
        try:
            total = total * 60 + float(part)
        except ValueError:
            return None
    return round(total, 3)


def _clock(seconds: float) -> str:
    """Format seconds as MM:SS. Minutes keep counting past 59 rather than wrap."""
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _turn(start, end, speaker, text) -> dict:
    """One speaker turn, with whitespace collapsed to a single line of prose."""
    return {
        "start": start,
        "end": end,
        "speaker": speaker or None,
        "text": " ".join((text or "").split()),
    }


def _split_speaker(text: str) -> tuple[str | None, str]:
    """Pull a leading "Name: " label off a line of cue or transcript text."""
    match = _SPEAKER_RE.match(text.strip())
    if not match:
        return None, text.strip()
    name = match.group("name").strip()
    if len(name.split()) > 4:
        return None, text.strip()
    return name, match.group("text").strip()


def _text_of(path_or_text) -> str:
    """Read a Path, or take a str as the text itself."""
    if isinstance(path_or_text, Path):
        return path_or_text.read_text(encoding="utf-8", errors="replace")
    return str(path_or_text or "")


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------


def _looks_like_vtt(text: str) -> bool:
    return text.lstrip("﻿ \t\r\n").upper().startswith("WEBVTT")


def _cue_times(line: str) -> tuple[float, float] | None:
    """The (start, end) of a subtitle cue line, or None when it is not one.

    The arrow alone proves nothing. A clinician's formulation note is full of
    "trigger --> thought --> spiral", and treating that as a cue would file the
    note as somebody's words. So the line must open with the range, and both
    tokens must carry a colon and read as times.
    """
    match = _ARROW_RE.match(line)
    if not match:
        return None
    start, end = match.group("start"), match.group("end")
    if ":" not in start or ":" not in end:
        return None
    seconds_start, seconds_end = _seconds(start), _seconds(end)
    if seconds_start is None or seconds_end is None:
        return None
    return seconds_start, seconds_end


def _timed_cues(text: str) -> int:
    """How many lines of this text are real, timed subtitle cues."""
    return sum(1 for line in text.splitlines() if _cue_times(line) is not None)


def _looks_like_srt(text: str) -> bool:
    """A digit-only cue index immediately above a real SubRip timestamp line."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        if _INT_LINE_RE.match(line) and _SRT_CUE_RE.match(lines[i + 1]):
            return True
    return False


def _otter_headers(text: str) -> list[tuple[int, str, float]]:
    """Every Otter style utterance header: (line index, speaker, seconds)."""
    found: list[tuple[int, str, float]] = []
    for i, line in enumerate(text.splitlines()):
        match = _OTTER_HEADER_RE.match(line.rstrip())
        if not match:
            continue
        name = match.group("name").strip()
        if not name or len(name.split()) > 4:
            continue
        seconds = _seconds(match.group("ts") or match.group("ts2") or "")
        if seconds is None:
            continue
        found.append((i, name, seconds))
    return found


def _dense_utterance_headers(text: str) -> bool:
    """True when timestamped speaker headers structure the whole document.

    Three of them, and at least a fifth of the non-blank lines, so a stray line
    that happens to end in a time is never mistaken for a transcript export.
    """
    headers = _otter_headers(text)
    non_blank = sum(1 for line in text.splitlines() if line.strip())
    if len(headers) < 3 or not non_blank:
        return False
    return len(headers) / non_blank >= 0.2


def _summary_headings(text: str) -> int:
    """How many generated-write-up headings the document carries."""
    seen = set()
    for line in text.splitlines():
        label = line.strip().strip("#*_").strip().rstrip(":").strip().lower()
        if label in _SUMMARY_HEADINGS:
            seen.add(label)
    return len(seen)


def _looks_like_granola(text: str) -> bool:
    """A Granola note: named as one, and shaped like a bulleted write up."""
    low = text.lower()
    if "granola" not in low and "notes generated" not in low:
        return False
    headings = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    bullets = sum(1 for line in text.splitlines() if line.lstrip()[:2] in ("- ", "* "))
    return headings >= 1 or bullets >= 3


def _looks_like_dialogue(text: str) -> bool:
    """Speaker labelled turns over most of the document, from two or more people.

    Deliberately hard to trip. A clinician's own notes routinely start lines with
    "Risk:" or "Plan:", and those must fall through to prose rather than be filed
    as somebody's words.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    labelled = [name for name, _ in (_split_speaker(line) for line in lines) if name]
    return len(labelled) >= 4 and len(set(labelled)) >= 2 and len(labelled) / len(lines) >= 0.4


def _is_zoom_vtt(text: str, turns: list[dict]) -> bool:
    """Zoom's cloud transcript: numbered cues carrying "Name: text" payloads."""
    lines = text.splitlines()
    numbered = any(
        _INT_LINE_RE.match(line) and "-->" in lines[i + 1]
        for i, line in enumerate(lines[:-1])
    )
    labelled = sum(1 for turn in turns if turn["speaker"]) >= 2
    if numbered and labelled:
        return True
    return "zoom" in "\n".join(lines[:12]).lower()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_cues(text: str) -> list[dict]:
    """Parse WebVTT or SubRip cues into turns, keeping timestamps and speakers.

    A cue payload may run over several lines and may carry either a "Name:"
    prefix or a WebVTT voice span; both become the turn's speaker.
    """
    turns: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        times = _cue_times(lines[i])
        if times is None:
            i += 1
            continue
        start, end = times
        i += 1
        payload: list[str] = []
        while i < len(lines) and lines[i].strip():
            payload.append(lines[i])
            i += 1
        body = " ".join(payload).strip()

        speaker = None
        voice = _VOICE_RE.search(body)
        if voice:
            speaker = voice.group(1).strip()
            body = _TAG_RE.sub("", body).strip()
        else:
            speaker, body = _split_speaker(body)
        turns.append(_turn(start, end, speaker, body))
    return turns


def _parse_timestamped_speakers(text: str) -> list[dict]:
    """Parse an export whose utterances open with "Speaker Name  0:03".

    An utterance is the block of lines immediately under its header, and it ends
    at the first blank line. Accumulation deliberately stops there rather than
    running to the next header, because these exports put generated material
    after the last utterance: an "otter.ai" footer, or a whole Summary section.
    Folding that into the final turn would put a machine's sentences inside a
    speaker's quoted words and into the rung 0 archive, which is the exact
    records failure this module exists to prevent.
    """
    lines = text.splitlines()
    headers = _otter_headers(text)
    turns: list[dict] = []
    for position, (index, speaker, seconds) in enumerate(headers):
        stop = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body: list[str] = []
        for line in lines[index + 1 : stop]:
            if not line.strip():
                break
            body.append(line)
        turns.append(_turn(seconds, None, speaker, " ".join(body)))
    return [turn for turn in turns if turn["text"]]


def _parse_speaker_turns(text: str) -> list[dict]:
    """Parse "Therapist: ..." style turns, with continuation lines folded in."""
    turns: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        speaker, body = _split_speaker(line)
        if speaker:
            turns.append(_turn(None, None, speaker, body))
        elif turns:
            turns[-1]["text"] = (turns[-1]["text"] + " " + line.strip()).strip()
        else:
            turns.append(_turn(None, None, None, line))
    return turns


def parse_transcript(path_or_text) -> tuple[str, list[dict]]:
    """Read a transcript into (plain text, timed speaker turns).

    Accepts a Path or the text itself. Each turn is
    {"start", "end", "speaker", "text"} with start and end in seconds and any of
    the three set to None when the format does not carry them. Prose with no
    structure at all comes back as a single unlabelled turn, which is what a
    pasted debrief of the session looks like.

    The returned text is the turns without their timestamps, one per line, which
    is what later rungs read.
    """
    text = _text_of(path_or_text)
    if not text.strip():
        return "", []

    turns: list[dict] = []
    if _timed_cues(text):
        turns = _parse_cues(text)
    # An empty result means the cue shape was a false alarm, so the other readers
    # still get their turn rather than archiving nothing.
    if not turns:
        if len(_otter_headers(text)) >= 2:
            turns = _parse_timestamped_speakers(text)
        elif _looks_like_dialogue(text):
            turns = _parse_speaker_turns(text)
        else:
            turns = [_turn(None, None, None, text)]

    turns = [turn for turn in turns if turn["text"]]
    plain = "\n".join(
        f"{turn['speaker']}: {turn['text']}" if turn["speaker"] else turn["text"]
        for turn in turns
    )
    return plain, turns


def render_turns(turns: list[dict]) -> str:
    """Render turns as the rung 0 archive: "[MM:SS] Speaker: text" per line.

    Timestamps and speakers are dropped from the line when the source did not
    carry them, so pasted prose renders as plain readable text.
    """
    lines: list[str] = []
    for turn in turns:
        parts: list[str] = []
        if turn.get("start") is not None:
            parts.append(f"[{_clock(turn['start'])}]")
        if turn.get("speaker"):
            parts.append(f"{turn['speaker']}:")
        body = (turn.get("text") or "").strip()
        if not body:
            continue
        parts.append(body)
        lines.append(" ".join(parts))
    return "\n".join(lines) + ("\n" if lines else "")


def _duration_min(turns: list[dict]) -> int | None:
    """Session length in whole minutes, from the last timestamp. None if untimed."""
    latest = None
    for turn in turns:
        for value in (turn.get("start"), turn.get("end")):
            if value is not None and (latest is None or value > latest):
                latest = value
    if latest is None:
        return None
    return int(math.ceil(latest / 60))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(path: Path | None, text: str) -> tuple[str, bool, str, float]:
    """Guess (kind, verbatim, produced_by, confidence) for some session material.

    Layered and deterministic, never a model call. Timed subtitle cues are
    certain, export structures are strong, speaker turns are a decent guess, and
    prose is a coin toss that the clinician settles. Confidence below
    CONFIRM_BELOW means ask.

    Every layer fails in the same direction: when the evidence is mixed or thin,
    the answer is not verbatim and the clinician is asked. A confirmation tap
    costs a second. A generated sentence filed as a client quote is in the record
    for good.
    """
    text = text or ""

    # path is accepted for the module contract and deliberately not used to
    # decide anything. An extension is a claim made by whoever named the file,
    # and a clinician's formulation note saved as .srt is still a note. Only the
    # bytes settle it: a WebVTT file always opens with the WEBVTT header and a
    # SubRip file always pairs a cue index with a full timestamp line, so a file
    # carrying neither falls through to the heuristics below and gets asked
    # about, whatever it is called.
    if _timed_cues(text):
        if _looks_like_vtt(text):
            _, turns = parse_transcript(text)
            producer = "zoom" if _is_zoom_vtt(text, turns) else "unknown"
            return "transcript_file", True, producer, 1.0
        if _looks_like_srt(text):
            return "transcript_file", True, "unknown", 1.0

    dense = _dense_utterance_headers(text)
    headings = _summary_headings(text)

    if dense and headings:
        # A transcript block with any generated section anywhere in the same
        # file: an Otter export with the summary switched on, a Fathom export
        # with Key Takeaways above the transcript. One heading is enough. There
        # is no way to know from structure alone which sentences came from a
        # person, so the whole file is treated as a write up and the clinician
        # is asked.
        return "third_party_summary", False, "otter", 0.5
    if dense:
        return "transcript_file", True, "otter", 0.9
    if _looks_like_granola(text):
        return "third_party_summary", False, "granola", 0.85
    if headings >= 2:
        return "third_party_summary", False, "otter", 0.85
    if _looks_like_dialogue(text):
        # Speaker labels with no timestamps anywhere. A pasted transcript looks
        # exactly like a clinician typing up remembered dialogue afterwards, and
        # only one of those is word for word, so this one always asks.
        return "transcript_file", True, "unknown", 0.75
    return "clinician_notes", False, "clinician", 0.5


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    """Run one ffmpeg command. The single seam every media path goes through.

    Kept as its own function so a test can stand in for the encoder: the fast
    suite must never need real ffmpeg or real media. Callers put the output path
    last.
    """
    return subprocess.run(args, capture_output=True, text=True, timeout=1800)


def _ffmpeg_tail(proc: subprocess.CompletedProcess) -> str:
    """The last of ffmpeg's stderr, which is where it says what went wrong."""
    return (proc.stderr or "").strip()[-300:]


def _ffmpeg_to(src: Path, args: list[str], out_name: str, what: str) -> Path:
    """Run ffmpeg into a fresh temp directory and return the file it wrote."""
    src = Path(src)
    if not src.is_file():
        raise SourceError(f"there is no file at {src}")
    out = Path(tempfile.mkdtemp(prefix="debrief_source_")) / out_name
    try:
        proc = _run_ffmpeg(["ffmpeg", "-y", "-i", str(src), *args, str(out)])
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(out.parent, ignore_errors=True)
        raise SourceError(f"could not {what}: {exc}")
    if proc.returncode != 0 or not out.exists():
        tail = _ffmpeg_tail(proc)
        shutil.rmtree(out.parent, ignore_errors=True)
        raise SourceError(f"could not {what}: {tail}")
    return out


def extract_audio(media_path: Path) -> Path:
    """Extract 16k mono wav from any audio or video file, for speech to text.

    Returns a path in a temp directory that the caller owns and deletes. Raises
    SourceError carrying the tail of ffmpeg's own message when it fails.
    """
    return _ffmpeg_to(
        media_path,
        ["-vn", "-ar", "16000", "-ac", "1"],
        "audio.wav",
        "read the audio in that file",
    )


def _extract_audio_track(media_path: Path) -> Path:
    """Extract a video's audio track to m4a, for the archive.

    m4a rather than wav because this one is kept: a 50 minute wav is roughly
    twenty times the size for no clinical gain. The video itself is never copied
    anywhere.
    """
    return _ffmpeg_to(
        media_path,
        ["-vn", "-c:a", "aac", "-b:a", "96k"],
        "source.m4a",
        "extract the audio from that video",
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _now() -> _dt.datetime:
    """Wall clock, in one place so a test can pin it."""
    return _dt.datetime.now().replace(microsecond=0)


def _consent_ref(client_id: str) -> str:
    """The client's dated consent, or ConsentError. Every ingest starts here."""
    granted, date = vault.recording_consent(client_id)
    if not granted:
        raise ConsentError(CONSENT_MESSAGE)
    return f"recording_consent {date}"


def _validated_kind(kind) -> str:
    text = str(kind or "").strip()
    if text not in KIND_RULES:
        raise SourceError(
            f"{text or '(none)'} is not a kind of session material. "
            f"Expected one of: {', '.join(sorted(KIND_RULES))}."
        )
    return text


def _next_source_id(client_id: str, captured_at: _dt.datetime) -> str:
    """The capture minute as an id, with -2, -3 on collision."""
    base = captured_at.strftime("%Y-%m-%d-%H%M")
    if not recstore.source_dir(client_id, base).exists():
        return base
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if not recstore.source_dir(client_id, candidate).exists():
            return candidate
        n += 1


def _store(
    client_id: str,
    source_id: str,
    kind: str,
    produced_by: str,
    captured_at: _dt.datetime,
    consent_ref: str,
    audio_path: Path | None = None,
    turns: list[dict] | None = None,
    duration_min: int | None = None,
) -> Source:
    """Build the Source, write meta.json, and hand it back.

    Every ingest path ends here, so provenance is written exactly once and in
    exactly one shape.
    """
    verbatim, entry_rung = KIND_RULES[kind]
    turns = turns or []
    source = Source(
        id=source_id,
        client_id=client_id,
        kind=kind,
        verbatim=verbatim,
        produced_by=produced_by,
        entry_rung=entry_rung,
        captured_at=captured_at,
        duration_min=duration_min,
        diarized=any(turn.get("speaker") for turn in turns),
        partial=False,
        audio_path=audio_path,
        consent_ref=consent_ref,
    )
    recstore.write_meta(client_id, source_id, source.to_meta())
    return source


def _read_document(path: Path) -> str:
    """The text of a document, or SourceError explaining why there is none."""
    try:
        result = doctext.extract_document_text(path, path.name)
    except doctext.DocumentReadError as exc:
        raise SourceError(str(exc))
    except ValueError as exc:
        raise SourceError(str(exc))
    if result.get("pdf_unsupported"):
        raise SourceError(
            "PDF reading is not available on this machine. Save the file as .docx "
            "or .md, or paste the text in."
        )
    text = result.get("text") or ""
    if not text.strip():
        raise SourceError(f"there was no readable text in {path.name}.")
    return text


def ingest_file(path: Path, client_id: str, declared_kind: str | None = None) -> tuple[Source, bool]:
    """Bring a file in as a source. Returns (source, needs_confirmation).

    Consent is checked before anything is read. The extension decides how the
    bytes are handled; for documents and transcripts, classify() decides what the
    material claims to be, unless declared_kind settles it. The source is always
    stored with its best guess, and needs_confirmation says whether the clinician
    should be asked to confirm that guess with one tap.

    Video is the exception to "store what arrived": its audio track is extracted
    and kept, and the video is not retained.
    """
    src = Path(path)
    if not src.is_file():
        raise SourceError(f"there is no file at {src}")

    consent_ref = _consent_ref(client_id)
    declared = _validated_kind(declared_kind) if declared_kind else None

    ext = src.suffix.lower()
    captured_at = _now()
    source_id = _next_source_id(client_id, captured_at)

    if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
        natural = "uploaded_audio" if ext in AUDIO_EXTS else "uploaded_video"
        if declared is not None and declared != natural:
            raise SourceError(
                f"{src.name} is {natural.replace('_', ' ')}, so it cannot be filed "
                f"as {declared.replace('_', ' ')}."
            )
        if ext in AUDIO_EXTS:
            # The original container is kept so the archive stays byte faithful.
            # Transcription is the job queue's work, not this function's.
            stored = recstore.write_audio(client_id, source_id, src)
        else:
            track = _extract_audio_track(src)
            try:
                stored = recstore.write_audio(client_id, source_id, track)
            finally:
                shutil.rmtree(track.parent, ignore_errors=True)
        return (
            _store(
                client_id,
                source_id,
                natural,
                "unknown",
                captured_at,
                consent_ref,
                audio_path=stored,
            ),
            False,
        )

    if ext in TRANSCRIPT_EXTS:
        text = src.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise SourceError(f"there was no readable text in {src.name}.")
    elif ext in DOC_EXTS:
        text = _read_document(src)
    else:
        raise SourceError(
            f"Debrief cannot read {ext or 'that file'}. Audio, video, .vtt, .srt, "
            ".txt, .md, .docx, and .pdf all work."
        )

    kind, _, produced_by, confidence = classify(src, text)
    if declared is not None:
        if declared not in TEXT_KINDS:
            # Declaring a document to be a recording would file text at a rung
            # that expects media, so the claim is refused rather than absorbed.
            raise SourceError(
                f"{src.name} holds text, so it cannot be filed as "
                f"{declared.replace('_', ' ')}. Expected one of: "
                f"{', '.join(sorted(TEXT_KINDS))}."
            )
        kind = declared
        confidence = 1.0
        if kind == "clinician_notes":
            produced_by = "clinician"

    return _store_text(client_id, source_id, kind, produced_by, captured_at, consent_ref, text), (
        confidence < CONFIRM_BELOW
    )


def ingest_text(text: str, client_id: str, kind: str) -> tuple[Source, bool]:
    """Bring pasted text in as a source. Returns (source, needs_confirmation).

    kind is pasted_transcript, which is word for word and enters at rung 0, or
    clinician_notes, which is the clinician's own observation and enters at rung
    2. Nothing is guessed here, so confirmation is never needed.
    """
    kind = _validated_kind(kind)
    if kind not in PASTE_KINDS:
        raise SourceError(
            f"pasted text is either {' or '.join(sorted(PASTE_KINDS))}, not {kind}."
        )
    if not (text or "").strip():
        raise SourceError("there was nothing in that text.")

    consent_ref = _consent_ref(client_id)
    captured_at = _now()
    source_id = _next_source_id(client_id, captured_at)
    produced_by = "clinician" if kind == "clinician_notes" else "unknown"

    source = _store_text(client_id, source_id, kind, produced_by, captured_at, consent_ref, text)
    return source, False


def _store_text(
    client_id: str,
    source_id: str,
    kind: str,
    produced_by: str,
    captured_at: _dt.datetime,
    consent_ref: str,
    text: str,
) -> Source:
    """Write text material at the rung its kind enters on, then its provenance.

    Rung 0 material is parsed into turns and archived as the raw transcript.
    Rung 2 material is an observation about the session, so it is stored as
    source text and never as a transcript.
    """
    _, entry_rung = KIND_RULES[kind]
    if entry_rung == 0:
        _, turns = parse_transcript(text)
        recstore.write_rung(client_id, source_id, 0, render_turns(turns))
        return _store(
            client_id,
            source_id,
            kind,
            produced_by,
            captured_at,
            consent_ref,
            turns=turns,
            duration_min=_duration_min(turns),
        )

    recstore.write_source_text(client_id, source_id, text)
    return _store(client_id, source_id, kind, produced_by, captured_at, consent_ref)
