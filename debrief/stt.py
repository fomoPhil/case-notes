"""Speech-to-text and a glossary correction pass.

Two stages, both local:
  1. transcribe(): a selectable local STT engine turns the recorded wav into raw
     text. Two engines ship: parakeet-mlx (the default, fast, already cached on
     this machine) and mlx-whisper (whisper-large-v3-turbo, downloaded on first
     use). get_engine() resolves which one to use from an explicit argument, the
     settings store, or the parakeet default.
  2. correct_transcript(): a cheap Gemma pass fixes misheard clinical terms
     (drug names, diagnoses, techniques) without changing meaning.

The corrected transcript is always shown to the therapist before any action
runs, so a biased correction can be caught by a human.

Offline mode is cache-aware. The old design set HF_HUB_OFFLINE at import so the
demo could run with Wi-Fi off, but that also blocks a deliberate first download
of a not-yet-cached engine (whisper). Instead each engine decides at load time:
if its model is already in the HuggingFace cache the offline flags are set
before the load (preserving the fully-offline guarantee); if the model is not
cached the load is allowed to reach the network for that one download, then the
offline flags are sealed so every subsequent load and run stays local. An
explicit HF_HUB_OFFLINE set by the user before startup always wins.

No em dashes anywhere.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from . import llm, settings_store, vocab

# The correction-prompt rules file (thinned to non-vocabulary guidance). Kept as
# a module symbol because the token-budget guard test reads it directly.
_GLOSSARY_PATH = Path(__file__).resolve().parent.parent / "prompts" / "glossary.md"

# Model ids per engine (per the architecture contract).
_PARAKEET_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
_WHISPER_MODEL_ID = "mlx-community/whisper-large-v3-turbo"

_DEFAULT_ENGINE = "parakeet"


# ---------------------------------------------------------------------------
# Cache-aware offline handling
# ---------------------------------------------------------------------------

# Snapshot the user's explicit offline choice at import, BEFORE any engine load
# mutates os.environ. An explicit env setting (fully air-gapped machine, or a
# deliberate online override) always wins over the cache-aware decision below.
_USER_HF_OFFLINE = os.environ.get("HF_HUB_OFFLINE")


def _hf_cache_dirs() -> list[Path]:
    """Candidate HuggingFace hub cache dirs, honoring the standard env overrides."""
    dirs: list[Path] = []
    for env in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        val = os.environ.get(env)
        if val:
            dirs.append(Path(val))
    home = os.environ.get("HF_HOME")
    if home:
        dirs.append(Path(home) / "hub")
    dirs.append(Path.home() / ".cache" / "huggingface" / "hub")
    return dirs


def _model_cached(repo_id: str) -> bool:
    """True when the HF repo already has a local snapshot with content.

    Checks huggingface_hub's cache layout: <hub>/models--<org>--<name>/snapshots
    must exist and be non-empty. A directory-scan (not an import of the heavy
    engine) so doctor and first-use hints stay cheap.
    """
    folder = "models--" + repo_id.replace("/", "--")
    for base in _hf_cache_dirs():
        snapshots = base / folder / "snapshots"
        try:
            if snapshots.is_dir() and any(snapshots.iterdir()):
                return True
        except OSError:
            continue
    return False


def _prepare_offline_for(repo_id: str) -> None:
    """Set the offline environment for the imminent load of repo_id.

    Cached model -> force offline so the load never touches the network. Not
    cached -> clear the offline flags so a deliberate first download can proceed
    (even if a prior engine load already sealed them). A user's explicit
    HF_HUB_OFFLINE always wins and is left untouched.
    """
    if _USER_HF_OFFLINE is not None:
        return
    if _model_cached(repo_id):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)


def _seal_offline() -> None:
    """After a (possibly first-download) load, re-seal offline so every later
    load and run stays fully local. Never overrides an explicit user choice."""
    if _USER_HF_OFFLINE is not None:
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


# ---------------------------------------------------------------------------
# Engine adapters
# ---------------------------------------------------------------------------


def _fallback_segments(text: str) -> list[dict]:
    """The whole transcript as one segment of unknown duration.

    The contract for a timed transcription when the engine cannot supply
    timestamps: one span starting at 0.0 with end None, so a caller can always
    iterate segments without branching on whether timing was available. Empty
    text yields no segments rather than one empty one.
    """
    text = (text or "").strip()
    return [{"start": 0.0, "end": None, "text": text}] if text else []


def _coerce_seconds(value) -> float | None:
    """A segment boundary as float seconds, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _Engine:
    """Common shape: a stable id, its HF repo, and transcribe(wav_path).

    transcribe_timed() is optional in spirit: the base implementation satisfies
    it by degrading to a single spanning segment, so a new engine that has no
    timestamps still works everywhere a timed transcript is expected.
    """

    id: str = ""
    repo_id: str = ""

    def transcribe(self, wav_path: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def transcribe_timed(self, wav_path: str) -> tuple[str, list[dict]]:
        """Default: no timestamps, so the full text is one spanning segment."""
        text = self.transcribe(wav_path)
        return text, _fallback_segments(text)


class ParakeetEngine(_Engine):
    """parakeet-mlx. Wraps the exact prior load-and-transcribe code."""

    id = "parakeet"
    repo_id = _PARAKEET_MODEL_ID

    def __init__(self) -> None:
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            # The model downloads inside from_pretrained, so decide offline mode
            # before the call and seal it after. The lazy import is also the
            # first huggingface_hub import in the process, so the offline
            # constants pick up the value we just set.
            _prepare_offline_for(self.repo_id)
            from parakeet_mlx import from_pretrained

            self._model = from_pretrained(self.repo_id)
            _seal_offline()
        return self._model

    def transcribe(self, wav_path: str) -> str:
        """Chunked so minutes-long dictation stays within memory."""
        model = self._ensure_loaded()
        result = model.transcribe(wav_path, chunk_duration=120.0)
        return result.text.strip()

    def transcribe_timed(self, wav_path: str) -> tuple[str, list[dict]]:
        """One transcription pass, returning text plus sentence timestamps.

        parakeet_mlx already aligns tokens into sentences carrying .start and
        .end in seconds, so the timings are free: this is the SAME single call
        transcribe() makes, not a second pass over the audio. Any sentence that
        cannot yield a usable start time is dropped, and a result with no usable
        sentences at all degrades to one spanning segment.
        """
        model = self._ensure_loaded()
        result = model.transcribe(wav_path, chunk_duration=120.0)
        text = (getattr(result, "text", "") or "").strip()

        segments: list[dict] = []
        for sentence in getattr(result, "sentences", None) or []:
            seg_text = (getattr(sentence, "text", "") or "").strip()
            start = _coerce_seconds(getattr(sentence, "start", None))
            if not seg_text or start is None:
                continue
            segments.append(
                {
                    "start": start,
                    "end": _coerce_seconds(getattr(sentence, "end", None)),
                    "text": seg_text,
                }
            )
        return text, segments or _fallback_segments(text)


class MlxWhisperEngine(_Engine):
    """mlx-whisper (whisper-large-v3-turbo).

    Unlike parakeet, mlx_whisper downloads the weights lazily inside its
    transcribe() call rather than at import, so the cache-aware offline decision
    has to be in force for the whole first transcription and the offline seal
    only happens after that first call succeeds.
    """

    id = "mlx-whisper"
    repo_id = _WHISPER_MODEL_ID

    def __init__(self) -> None:
        self._ready = False

    def _run(self, wav_path: str) -> dict:
        """One mlx_whisper pass, with the first-call offline dance around it.

        Shared by transcribe() and transcribe_timed() so asking for timestamps
        never costs a second transcription. A non-dict result becomes {}, which
        is what the old inline isinstance check produced.
        """
        first = not self._ready
        if first:
            _prepare_offline_for(self.repo_id)
        import mlx_whisper

        result = mlx_whisper.transcribe(wav_path, path_or_hf_repo=self.repo_id)
        if first:
            _seal_offline()
            self._ready = True
        return result if isinstance(result, dict) else {}

    def transcribe(self, wav_path: str) -> str:
        result = self._run(wav_path)
        return (result.get("text", "") or "").strip()

    def transcribe_timed(self, wav_path: str) -> tuple[str, list[dict]]:
        """Text plus whisper's own segment timings when the result carries them."""
        result = self._run(wav_path)
        text = (result.get("text", "") or "").strip()

        segments: list[dict] = []
        raw = result.get("segments")
        for seg in raw if isinstance(raw, list) else []:
            if not isinstance(seg, dict):
                continue
            seg_text = (seg.get("text") or "").strip()
            start = _coerce_seconds(seg.get("start"))
            if not seg_text or start is None:
                continue
            segments.append(
                {"start": start, "end": _coerce_seconds(seg.get("end")), "text": seg_text}
            )
        return text, segments or _fallback_segments(text)


_ENGINE_CLASSES: dict[str, type[_Engine]] = {
    "parakeet": ParakeetEngine,
    "mlx-whisper": MlxWhisperEngine,
}

# Per-engine lazy singletons: a loaded engine (and its model) is reused across
# calls, and switching engines never reloads a previously loaded one.
_engine_cache: dict[str, _Engine] = {}


def valid_engine_ids() -> list[str]:
    """The recognised STT engine ids, sorted for stable messages."""
    return sorted(_ENGINE_CLASSES)


def engine_repo_id(engine_id: str) -> str | None:
    """The HF repo id backing an engine, or None for an unknown id."""
    cls = _ENGINE_CLASSES.get(engine_id)
    return cls.repo_id if cls else None


def is_engine_model_cached(engine_id: str) -> bool:
    """True when the engine's model is already in the local HF cache."""
    repo = engine_repo_id(engine_id)
    return bool(repo) and _model_cached(repo)


def get_engine(engine_id: str | None = None) -> _Engine:
    """Resolve and return the STT engine to use.

    Precedence: explicit engine_id argument > settings_store stt_engine (which
    already folds in the DEBRIEF_STT_ENGINE env override) > the parakeet default.
    An unknown id raises ValueError listing the valid ids.
    """
    if engine_id is None:
        engine_id = settings_store.load().get("stt_engine") or _DEFAULT_ENGINE
    engine_id = (engine_id or "").strip() or _DEFAULT_ENGINE
    if engine_id not in _ENGINE_CLASSES:
        valid = ", ".join(valid_engine_ids())
        raise ValueError(f"unknown STT engine {engine_id!r}; valid engines: {valid}")
    if engine_id not in _engine_cache:
        _engine_cache[engine_id] = _ENGINE_CLASSES[engine_id]()
    return _engine_cache[engine_id]


def warm_engine_in_background(engine_id: str | None = None) -> None:
    """Load the selected STT engine on a daemon thread at launch.

    Without this, the very first debrief of a session stalls while the model
    loads (or downloads on a fresh machine) with nothing on screen explaining
    the wait. Warming at launch moves that cost to a moment when the clinician
    is not waiting on it. Best effort: any failure is swallowed, because a
    warmup problem must never stop the app from starting. The real transcribe
    call will surface the error properly if it persists.

    Only engines that can load without audio are warmed. mlx-whisper pulls its
    weights inside transcribe(), so there is nothing to warm ahead of time.
    """
    import threading

    def _warm() -> None:
        try:
            loader = getattr(get_engine(engine_id), "_ensure_loaded", None)
            if callable(loader):
                loader()
        except Exception:  # noqa: BLE001 - warmup is never fatal
            pass

    threading.Thread(target=_warm, name="debrief-stt-warmup", daemon=True).start()


def transcribe(wav_path: str, engine_id: str | None = None) -> str:
    """Transcribe a wav file to raw text with the selected local STT engine.

    engine_id defaults to the settings-store choice, so existing callers that
    pass only the path keep working.
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    return get_engine(engine_id).transcribe(wav_path)


def transcribe_timed(wav_path: str, engine_id: str | None = None) -> tuple[str, list[dict]]:
    """Transcribe a wav file to text plus timed segments.

    Returns (text, segments) where segments is
    [{"start": float, "end": float | None, "text": str}] in seconds. Engine
    resolution matches transcribe() exactly.

    Timestamps are best effort by contract: an engine that cannot produce them
    (or produces a shape this adapter does not understand) yields the whole
    transcript as one segment starting at 0.0 with end None. This never raises
    just because timings were unavailable, so a caller can always rely on
    getting at least the text.
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    engine = get_engine(engine_id)

    timed = getattr(engine, "transcribe_timed", None)
    if callable(timed):
        text, segments = timed(wav_path)
    else:
        # A duck-typed engine predating the timed contract.
        text, segments = engine.transcribe(wav_path), []
    # Normalised here as well as in the adapters, so the returned text and the
    # fallback segment's text can never disagree about leading whitespace.
    text = (text or "").strip()
    return text, segments or _fallback_segments(text)


# ---------------------------------------------------------------------------
# Glossary correction pass (unchanged contract)
# ---------------------------------------------------------------------------


def _framework_layer(framework: str | None, profession: str | None = None) -> str:
    """Elevate the active framework's term list. Delegates to the vocab registry;
    kept as a module symbol because tests call it directly."""
    return vocab.framework_layer(framework, profession)


def _build_correction_system(
    client_ctx: dict | None,
    framework: str | None,
    profession: str = "therapy",
    dictionary: str = "",
) -> str:
    """Assemble the layered correction prompt via the vocab registry.

    Layers: shared correction rules, the profession's reference vocabulary, this
    client's chart facts, the active framework's elevated term list, and the
    user's custom dictionary when it has content. Framework falls back to the one
    recorded on the client profile when not passed explicitly.
    """
    if not framework and client_ctx:
        framework = client_ctx.get("framework")
    return vocab.correction_layers(profession, framework, client_ctx, dictionary)


def correct_transcript(
    text: str,
    client_ctx: dict | None = None,
    framework: str | None = None,
    profession: str | None = None,
    dictionary: str | None = None,
) -> str:
    """Fix misheard clinical terms via a Gemma glossary pass.

    Builds a layered correction prompt: shared rules, the profession's reference
    vocabulary, this client's chart facts (name, diagnoses, medications), the
    active framework's elevated term list, and the user's custom dictionary. The
    correction contract stays strict: fix ONLY likely mis-transcriptions, change
    nothing else.

    profession and dictionary default from the settings store at call time, so
    existing callers that pass neither keep working. Returns the corrected
    transcript. On an empty input, returns it unchanged.
    """
    if not text or not text.strip():
        return text
    if profession is None:
        profession = settings_store.load().get("profession", "therapy")
    if dictionary is None:
        dictionary = settings_store.read_dictionary()
    system = _build_correction_system(client_ctx, framework, profession, dictionary)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text.strip()},
    ]
    corrected = llm.chat(messages, max_tokens=1500, temperature=0.0)
    return corrected.strip() if isinstance(corrected, str) else text


# ---------------------------------------------------------------------------
# Windowed correction for full-session transcripts
# ---------------------------------------------------------------------------

# A 50-minute session is 6,000 to 9,000 words. correct_transcript cannot carry
# that: max_tokens=1500 truncates it, and one call cannot hold the transcript
# plus the layered correction prompt. So the long path splits into windows that
# each comfortably fit, and stitches the results back together.
_WINDOW_WORDS = 1200

# A run of words with no sentence-ending punctuation (a wall-of-text paste, or
# an engine that emitted none) is hard-split at this length, so one unbroken
# run can never become a monster window. Split pieces are _WINDOW_WORDS long,
# so the largest possible window is one sentence of _MAX_SENTENCE_WORDS.
_MAX_SENTENCE_WORDS = 1500

# Output budget per window. Roughly 2 tokens per word (generous: English runs
# nearer 1.33) plus slack, because a window truncated mid-sentence is exactly
# the bug this function exists to fix.
_TOKENS_PER_WORD = 2
_TOKEN_SLACK = 200
_MIN_WINDOW_TOKENS = 256
_MAX_WINDOW_TOKENS = 2600

# The read-only tail handed to the next window. Two sentences, but capped in
# words so a hard-split wall of text cannot hand over a context block larger
# than the window it is meant to orient.
_CONTEXT_SENTENCES = 2
_CONTEXT_MAX_WORDS = 250

_CONTEXT_HEADER = "CONTEXT (already corrected, do not repeat it in your output):"
_WINDOW_HEADER = "CORRECT ONLY THE FOLLOWING TEXT:"

# Split after sentence-ending punctuation followed by whitespace. Deliberately
# simple: an abbreviation like "Dr. Smith" splits early, which only makes a
# window boundary land one clause sooner. It never loses or duplicates text.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split a transcript into sentences, hard-splitting unbroken runs.

    Any piece longer than _MAX_SENTENCE_WORDS is chopped into _WINDOW_WORDS
    chunks so a punctuation-free paste still windows. Whitespace inside a
    hard-split chunk is normalised; ordinary sentences keep their own spacing.
    """
    pieces: list[str] = []
    for raw in _SENTENCE_END.split((text or "").strip()):
        piece = raw.strip()
        if not piece:
            continue
        words = piece.split()
        if len(words) <= _MAX_SENTENCE_WORDS:
            pieces.append(piece)
            continue
        for start in range(0, len(words), _WINDOW_WORDS):
            pieces.append(" ".join(words[start : start + _WINDOW_WORDS]))
    return pieces


def _pack_windows(sentences: list[str]) -> list[tuple[int, int]]:
    """Pack sentences into disjoint windows of roughly _WINDOW_WORDS words.

    Returns half-open index ranges into `sentences`. A window closes when the
    next sentence would push it past the budget, so every boundary lands on a
    sentence boundary. Every window holds at least one sentence (an oversized
    single sentence gets its own window rather than an empty one), and the
    ranges tile the whole list: disjoint, contiguous, nothing dropped.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    words_in_window = 0
    for index, sentence in enumerate(sentences):
        length = len(sentence.split())
        if words_in_window and words_in_window + length > _WINDOW_WORDS:
            windows.append((start, index))
            start = index
            words_in_window = 0
        words_in_window += length
    if start < len(sentences):
        windows.append((start, len(sentences)))
    return windows


def _window_max_tokens(window_text: str) -> int:
    """Output budget for one window: generous per word, hard-capped."""
    words = len(window_text.split())
    budget = words * _TOKENS_PER_WORD + _TOKEN_SLACK
    return min(_MAX_WINDOW_TOKENS, max(_MIN_WINDOW_TOKENS, budget))


def _context_tail(text: str) -> str:
    """The last couple of sentences of a window, for the next window's context."""
    tail = " ".join(_split_sentences(text)[-_CONTEXT_SENTENCES:])
    words = tail.split()
    if len(words) > _CONTEXT_MAX_WORDS:
        tail = " ".join(words[-_CONTEXT_MAX_WORDS:])
    return tail


def correct_long_transcript(
    text: str,
    client_ctx: dict | None = None,
    framework: str | None = None,
    profession: str | None = None,
    dictionary: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Fix misheard clinical terms across a full-session transcript.

    Same contract and same layered prompt as correct_transcript, applied over
    windows of roughly 1,200 words split on sentence boundaries. The system
    prompt does not vary per window, so it is built once. Each window is one
    call; windows after the first are handed the previous window's last two
    sentences as read-only context so terminology stays consistent across a
    boundary, and that context is never part of the window's own output.

    Windows are independent on purpose: a window whose call raises, or answers
    with nothing, degrades to its own uncorrected text. A long session never
    fails wholesale because one call went wrong, and no apology note is ever
    injected into the clinical text.

    progress, when given, is called as progress(done, total) after each window.
    profession and dictionary default from the settings store at call time.
    Empty input is returned unchanged.
    """
    if not text or not text.strip():
        return text
    if profession is None:
        profession = settings_store.load().get("profession", "therapy")
    if dictionary is None:
        dictionary = settings_store.read_dictionary()
    system = _build_correction_system(client_ctx, framework, profession, dictionary)

    sentences = _split_sentences(text)
    windows = _pack_windows(sentences)
    total = len(windows)

    corrected_windows: list[str] = []
    previous_tail = ""
    for done, (start, end) in enumerate(windows, start=1):
        window_text = " ".join(sentences[start:end])
        if previous_tail:
            user = (
                f"{_CONTEXT_HEADER}\n{previous_tail}\n\n{_WINDOW_HEADER}\n{window_text}"
            )
        else:
            user = window_text

        try:
            answer = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=_window_max_tokens(window_text),
                temperature=0.0,
            )
        except Exception:  # noqa: BLE001 - one bad window must not fail the run
            answer = None

        if isinstance(answer, str) and answer.strip():
            corrected = answer.strip()
        else:
            corrected = window_text

        corrected_windows.append(corrected)
        # Context comes from the corrected output when the window succeeded, and
        # from its raw text when it did not (corrected is the raw text then).
        previous_tail = _context_tail(corrected)
        if progress is not None:
            progress(done, total)

    return " ".join(corrected_windows)
