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


# Compact per-framework term hints for the "this clinician practices X" layer.
# The full framework term lists live in prompts/glossary.md (the static layer);
# these short lists are what we elevate when the active framework is known, so
# ambiguous words get biased toward the right modality's vocabulary.
_FRAMEWORK_TERMS = {
    "CBT": [
        "cognitive restructuring", "automatic thoughts", "cognitive distortions",
        "thought record", "behavioral activation", "graded exposure",
        "Socratic questioning", "CBT-I",
    ],
    "ACT": [
        "cognitive defusion", "willingness", "values clarification",
        "committed action", "self-as-context", "acceptance", "mindfulness",
    ],
    "DBT": [
        "diary card", "chain analysis", "distress tolerance",
        "emotion regulation", "interpersonal effectiveness", "wise mind",
        "validation",
    ],
    "EMDR": [
        "SUDs", "VOC", "bilateral stimulation", "negative cognition (NC)",
        "positive cognition (PC)", "target memory", "body scan",
    ],
    "FAMILY SYSTEMS": [
        "genogram", "triangulation", "differentiation", "enmeshment",
        "boundaries", "subsystems", "enactment",
    ],
    "PSYCHODYNAMIC": [
        "transference", "countertransference", "defenses", "interpretation",
        "insight", "working through",
    ],
}

# Human-readable expansions for the ICD-10 codes carried on the mock profiles,
# so a diagnosis code in the context also biases the spelled-out term.
_DIAGNOSIS_TERMS = {
    "F41.1": "generalized anxiety disorder (GAD)",
    "F41.9": "anxiety disorder",
    "F32": "major depressive disorder (MDD)",
    "F33": "recurrent major depressive disorder (MDD)",
    "F43.1": "post-traumatic stress disorder (PTSD)",
    "F43.10": "post-traumatic stress disorder (PTSD)",
    "F42": "obsessive-compulsive disorder (OCD)",
    "F90": "attention-deficit/hyperactivity disorder (ADHD)",
    "F90.0": "attention-deficit/hyperactivity disorder (ADHD)",
    "F31": "bipolar disorder",
    "F34.1": "dysthymia (persistent depressive disorder)",
}


def _framework_key(framework: str | None) -> str:
    return (framework or "").strip().upper()


def _client_layer(client_ctx: dict | None) -> str:
    """Layer (b): a client-specific block so misheard names, diagnoses, and
    medications from THIS client's chart are corrected toward what is on file.

    Returns an empty string when there is nothing useful on the profile.
    """
    if not client_ctx:
        return ""
    lines: list[str] = []

    name = (client_ctx.get("name") or "").strip()
    if name:
        lines.append(
            f"- Client name (fix misheard spellings toward this exactly): {name}"
        )

    diagnosis = client_ctx.get("diagnosis") or []
    if isinstance(diagnosis, (list, tuple)):
        dx_items = [str(d).strip() for d in diagnosis if str(d).strip()]
    else:
        dx_items = [str(diagnosis).strip()] if str(diagnosis).strip() else []
    if dx_items:
        rendered = []
        for code in dx_items:
            term = _DIAGNOSIS_TERMS.get(code.upper())
            rendered.append(f"{code} ({term})" if term else code)
        lines.append(f"- Diagnoses on file: {', '.join(rendered)}")

    meds = client_ctx.get("medications") or client_ctx.get("current_medications") or []
    if isinstance(meds, str):
        meds = [meds]
    med_items = [str(m).strip() for m in meds if str(m).strip()]
    if med_items:
        lines.append(f"- Current medications on file: {', '.join(med_items)}")

    fw = (client_ctx.get("framework") or "").strip()
    if fw:
        lines.append(f"- Treatment framework on file: {fw}")

    if not lines:
        return ""
    return (
        "\nCLIENT CHART CONTEXT (bias ambiguous words toward these known facts, "
        "but still change ONLY clear mis-transcriptions):\n" + "\n".join(lines) + "\n"
    )


def _framework_layer(framework: str | None) -> str:
    """Layer (c): elevate the active framework's term list so ambiguous words
    are read as that modality's vocabulary."""
    key = _framework_key(framework)
    terms = _FRAMEWORK_TERMS.get(key)
    if not terms:
        return ""
    return (
        f"\nThis clinician practices {framework}, so a GARBLED word is more likely "
        f"to be one of these terms: {', '.join(terms)}. This only raises the odds "
        f"for words that are actually mis-transcribed. It is NOT a licence to "
        f"reword fluent, correct English. A plain phrase that already reads "
        f"correctly (for example a 'chain of events', a personal 'diary', dish "
        f"'suds') is not garbled, so leave it exactly as written.\n"
    )


def _build_correction_system(client_ctx: dict | None, framework: str | None) -> str:
    """Assemble the three-layer correction prompt.

    (a) the static clinical glossary file, (b) this client's chart facts,
    (c) the active framework's elevated term list. Framework falls back to the
    one recorded on the client profile when not passed explicitly.
    """
    static = _GLOSSARY_PATH.read_text(encoding="utf-8").rstrip()
    if not framework and client_ctx:
        framework = client_ctx.get("framework")
    return static + "\n" + _client_layer(client_ctx) + _framework_layer(framework)


def correct_transcript(
    text: str,
    client_ctx: dict | None = None,
    framework: str | None = None,
) -> str:
    """Fix misheard clinical terms via a Gemma glossary pass.

    Builds a three-layer correction prompt: the static clinical glossary, plus
    (when available) this client's chart facts (name, diagnoses, medications)
    and the active framework's elevated term list. The correction contract stays
    strict: fix ONLY likely mis-transcriptions, change nothing else.

    Returns the corrected transcript. On an empty input, returns it unchanged.
    """
    if not text or not text.strip():
        return text
    system = _build_correction_system(client_ctx, framework)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text.strip()},
    ]
    corrected = llm.chat(messages, max_tokens=1500, temperature=0.0)
    return corrected.strip() if isinstance(corrected, str) else text
