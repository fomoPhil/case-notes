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

from . import llm

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


def correct_transcript(text: str) -> str:
    """Fix misheard clinical terms via a Gemma glossary pass.

    Returns the corrected transcript. On an empty input, returns it unchanged.
    """
    if not text or not text.strip():
        return text
    system = _GLOSSARY_PATH.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text.strip()},
    ]
    corrected = llm.chat(messages, max_tokens=1500, temperature=0.0)
    return corrected.strip() if isinstance(corrected, str) else text
