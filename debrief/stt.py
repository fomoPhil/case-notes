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


class _Engine:
    """Common shape: a stable id, its HF repo, and transcribe(wav_path)."""

    id: str = ""
    repo_id: str = ""

    def transcribe(self, wav_path: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


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

    def transcribe(self, wav_path: str) -> str:
        first = not self._ready
        if first:
            _prepare_offline_for(self.repo_id)
        import mlx_whisper

        result = mlx_whisper.transcribe(wav_path, path_or_hf_repo=self.repo_id)
        if first:
            _seal_offline()
            self._ready = True
        text = result.get("text", "") if isinstance(result, dict) else ""
        return (text or "").strip()


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


def transcribe(wav_path: str, engine_id: str | None = None) -> str:
    """Transcribe a wav file to raw text with the selected local STT engine.

    engine_id defaults to the settings-store choice, so existing callers that
    pass only the path keep working.
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    return get_engine(engine_id).transcribe(wav_path)


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
