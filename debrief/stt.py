"""Speech-to-text and a glossary correction pass.

Two stages, both local:
  1. transcribe(): parakeet-mlx turns the recorded wav into raw text.
  2. correct_transcript(): a cheap Gemma pass fixes misheard clinical terms
     (drug names, diagnoses, techniques) without changing meaning.

The corrected transcript is always shown to the therapist before any action
runs, so a biased correction can be caught by a human.
"""

from __future__ import annotations

from pathlib import Path

from . import llm, settings_store, vocab

# The correction-prompt rules file (thinned to non-vocabulary guidance). Kept as
# a module symbol because the token-budget guard test reads it directly.
_GLOSSARY_PATH = Path(__file__).resolve().parent.parent / "prompts" / "glossary.md"

# parakeet model id (per the architecture contract).
_STT_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"

# Load the STT model once and reuse it across calls.
_model = None


def _get_model():
    global _model
    if _model is None:
        from parakeet_mlx import from_pretrained

        _model = from_pretrained(_STT_MODEL_ID)
    return _model


def transcribe(wav_path: str) -> str:
    """Transcribe a wav file to raw text with parakeet-mlx.

    Chunked so minutes-long dictation stays within memory.
    """
    if not Path(wav_path).exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    model = _get_model()
    result = model.transcribe(wav_path, chunk_duration=120.0)
    return result.text.strip()


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
