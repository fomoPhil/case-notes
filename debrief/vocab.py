"""Single source of truth for profession vocabulary.

Before this module the same clinical vocabulary lived in four places at once:
stt.py (_FRAMEWORK_TERMS, _DIAGNOSIS_TERMS), extract.py (_FRAMEWORK_VOCAB),
prompts/glossary.md, and prompts/extract_system.md. That is impossible to keep
in sync. Everything vocabulary-shaped now lives here, keyed by profession, and
the correction pass and the extractor both read from this registry.

Each profession is a TERM_PACK:

    name                display name
    clinical            whether risk/clinical framing applies
    frameworks          {FRAMEWORK: [compact elevated correction terms]}
    diagnosis_terms     {CODE: spelled-out term} for chart-context expansion
    correction_glossary reference vocabulary block for the correction prompt
    extract_vocab_table {FRAMEWORK: comma-joined authentic vocabulary string}

The therapy pack is migrated VERBATIM from the pre-existing constants and files
(byte-parity tests guard the CBT framework list and the diagnosis-term map). The
other packs are curated stubs for launch and will deepen with real usage.

No em dashes anywhere.
"""

from __future__ import annotations

from pathlib import Path

# The correction-prompt rules (no vocabulary) live in this thinned file. The
# vocabulary that used to sit below the rules now lives in each pack's
# correction_glossary.
_CORRECTION_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "glossary.md"
)


# ---------------------------------------------------------------------------
# Therapy vocabulary (migrated verbatim from the four historical sources)
# ---------------------------------------------------------------------------

# From stt.py _FRAMEWORK_TERMS. The compact per-framework term hints elevated
# when the active framework is known.
_THERAPY_FRAMEWORKS = {
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

# From stt.py _DIAGNOSIS_TERMS. ICD-10 code to spelled-out term.
_THERAPY_DIAGNOSIS_TERMS = {
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

# From extract.py _FRAMEWORK_VOCAB. Framework-authentic vocabulary injected into
# the extraction user message.
_THERAPY_EXTRACT_VOCAB = {
    "CBT": "cognitive restructuring, automatic thoughts, cognitive distortions, thought records, behavioral activation, graded exposure, Socratic questioning",
    "ACT": "cognitive defusion, willingness, values clarification, committed action, self-as-context, acceptance, mindfulness",
    "DBT": "diary card, chain analysis, target behaviors, the four skills modules, validation",
    "FAMILY SYSTEMS": "subsystems, boundaries, enmeshment, enactment, differentiation, triangulation, genogram",
    "EMDR": "target memory, negative and positive cognitions (NC/PC), SUDs 0 to 10, VOC 1 to 7, bilateral stimulation, body scan",
    "PSYCHODYNAMIC": "transference, countertransference, defenses, interpretation, insight, working through",
}

# The reference vocabulary block that used to sit at the bottom of glossary.md,
# migrated verbatim. Handed to the correction pass after the rules.
_THERAPY_CORRECTION_GLOSSARY = """Reference clinical vocabulary (terms STT tends to mangle):

- Diagnoses/abbreviations: GAD, MDD, PTSD, OCD, ADHD, bipolar, dysthymia, SI (suicidal ideation), HI (homicidal ideation).
- Medications: sertraline, escitalopram, fluoxetine, bupropion, venlafaxine, duloxetine, lamotrigine, buspirone, hydroxyzine, trazodone.
- Instruments/scales: PHQ-9, GAD-7, SUDs, VOC, Columbia (C-SSRS).

Framework-specific term lists:

- CBT: cognitive restructuring, automatic thoughts, cognitive distortions, thought record, behavioral activation, graded exposure, Socratic questioning, CBT-I.
- ACT: cognitive defusion, willingness, values clarification, committed action, self-as-context, acceptance, mindfulness.
- DBT: diary card, chain analysis, distress tolerance, emotion regulation, interpersonal effectiveness, wise mind, validation.
- EMDR: SUDs (subjective units of distress, 0 to 10), VOC (validity of cognition, 1 to 7), bilateral stimulation, negative cognition (NC), positive cognition (PC), target memory, body scan.
- Family systems: genogram, triangulation, differentiation, enmeshment, boundaries, subsystems, enactment.
- Psychodynamic: transference, countertransference, defenses, interpretation, insight, working through."""


# ---------------------------------------------------------------------------
# Stub packs (curated launch starters, clinical framing per profession)
# ---------------------------------------------------------------------------

# Speech-language pathology: clinical, code-bearing.
_SLP_FRAMEWORKS = {
    "ARTICULATION": [
        "phoneme", "phonological process", "minimal pairs", "cycles approach",
        "stimulability", "elicitation", "carryover",
    ],
    "LANGUAGE": [
        "expressive language", "receptive language", "mean length of utterance (MLU)",
        "morphology", "syntax", "pragmatics",
    ],
    "FLUENCY": [
        "disfluency", "stuttering", "prolongation", "block", "easy onset",
        "light articulatory contact",
    ],
    "DYSPHAGIA": [
        "penetration", "aspiration", "bolus", "pharyngeal phase", "residue",
        "thickened liquids",
    ],
}
_SLP_DIAGNOSIS_TERMS = {
    "F80.0": "phonological disorder",
    "F80.1": "expressive language disorder",
    "F80.2": "mixed receptive-expressive language disorder",
    "F80.81": "childhood-onset fluency disorder (stuttering)",
    "R13.1": "dysphagia",
}
_SLP_EXTRACT_VOCAB = {
    "ARTICULATION": "phoneme accuracy, phonological processes, minimal pairs, cycles approach, stimulability, carryover",
    "LANGUAGE": "expressive and receptive language, mean length of utterance, morphology, syntax, pragmatics",
    "FLUENCY": "disfluency count, stuttering-like disfluencies, easy onset, light articulatory contact",
    "DYSPHAGIA": "bolus control, penetration, aspiration, pharyngeal phase, diet consistency",
}
_SLP_CORRECTION_GLOSSARY = """Reference speech-language pathology vocabulary (terms STT tends to mangle):

- Areas: articulation, phonology, expressive language, receptive language, fluency, voice, dysphagia.
- Measures: mean length of utterance (MLU), percent consonants correct (PCC), disfluency count, stimulability.
- Dysphagia: bolus, penetration, aspiration, pharyngeal phase, residue, thickened liquids."""

# Coaching: non-clinical.
_COACHING_FRAMEWORKS = {
    "GROW": [
        "goal", "reality", "options", "will", "way forward", "accountability",
    ],
    "SOLUTION FOCUSED": [
        "scaling question", "miracle question", "exception", "preferred future",
        "small steps",
    ],
    "CO-ACTIVE": [
        "fulfillment", "balance", "process", "values", "saboteur", "designed alliance",
    ],
}
_COACHING_DIAGNOSIS_TERMS: dict = {}
_COACHING_EXTRACT_VOCAB = {
    "GROW": "goal, reality, options, will and way forward, accountability actions",
    "SOLUTION FOCUSED": "scaling questions, miracle question, exceptions, preferred future, small next steps",
    "CO-ACTIVE": "values, fulfillment, balance, process, saboteur, designed alliance",
}
_COACHING_CORRECTION_GLOSSARY = """Reference coaching vocabulary (terms STT tends to mangle):

- Models: GROW (goal, reality, options, will), solution-focused, co-active coaching.
- Common terms: accountability, scaling question, miracle question, values, limiting beliefs, action steps."""

# Legal / professional meeting: non-clinical.
_LEGAL_FRAMEWORKS = {
    "MATTER": [
        "matter", "client intake", "conflict check", "engagement letter",
        "scope of work", "retainer",
    ],
    "LITIGATION": [
        "pleadings", "discovery", "deposition", "motion", "settlement", "disclosure",
    ],
    "TRANSACTIONAL": [
        "term sheet", "due diligence", "closing", "counterparty", "indemnity",
        "covenant",
    ],
}
_LEGAL_DIAGNOSIS_TERMS: dict = {}
_LEGAL_EXTRACT_VOCAB = {
    "MATTER": "matter reference, conflict check, engagement scope, retainer, deadlines",
    "LITIGATION": "pleadings, discovery, deposition, motion, settlement posture, disclosure obligations",
    "TRANSACTIONAL": "term sheet, due diligence, closing conditions, counterparty, indemnity, covenants",
}
_LEGAL_CORRECTION_GLOSSARY = """Reference professional meeting vocabulary (terms STT tends to mangle):

- Matter handling: intake, conflict check, engagement letter, scope, retainer, deadlines.
- Litigation: pleadings, discovery, deposition, motion, disclosure, settlement.
- Transactional: term sheet, due diligence, closing, counterparty, indemnity, covenant."""


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

TERM_PACKS: dict = {
    "therapy": {
        "name": "Therapy",
        "clinical": True,
        "frameworks": _THERAPY_FRAMEWORKS,
        "diagnosis_terms": _THERAPY_DIAGNOSIS_TERMS,
        "correction_glossary": _THERAPY_CORRECTION_GLOSSARY,
        "extract_vocab_table": _THERAPY_EXTRACT_VOCAB,
    },
    "slp": {
        "name": "Speech-Language Pathology",
        "clinical": True,
        "frameworks": _SLP_FRAMEWORKS,
        "diagnosis_terms": _SLP_DIAGNOSIS_TERMS,
        "correction_glossary": _SLP_CORRECTION_GLOSSARY,
        "extract_vocab_table": _SLP_EXTRACT_VOCAB,
    },
    "coaching": {
        "name": "Coaching",
        "clinical": False,
        "frameworks": _COACHING_FRAMEWORKS,
        "diagnosis_terms": _COACHING_DIAGNOSIS_TERMS,
        "correction_glossary": _COACHING_CORRECTION_GLOSSARY,
        "extract_vocab_table": _COACHING_EXTRACT_VOCAB,
    },
    "legal_meeting": {
        "name": "Legal Meeting",
        "clinical": False,
        "frameworks": _LEGAL_FRAMEWORKS,
        "diagnosis_terms": _LEGAL_DIAGNOSIS_TERMS,
        "correction_glossary": _LEGAL_CORRECTION_GLOSSARY,
        "extract_vocab_table": _LEGAL_EXTRACT_VOCAB,
    },
}

# Display order for the professions list.
_PROFESSION_ORDER = ["therapy", "slp", "coaching", "legal_meeting"]

_DEFAULT_PROFESSION = "therapy"


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def get_pack(profession: str | None) -> dict:
    """Return the term pack for a profession, falling back to therapy."""
    return TERM_PACKS.get((profession or "").strip().lower(), TERM_PACKS[_DEFAULT_PROFESSION])


def list_professions() -> list[dict]:
    """Return [{id, name, clinical}] in display order for the settings UI."""
    out: list[dict] = []
    for pid in _PROFESSION_ORDER:
        pack = TERM_PACKS[pid]
        out.append({"id": pid, "name": pack["name"], "clinical": pack["clinical"]})
    return out


def extract_framework_vocab(framework: str | None, profession: str | None = None) -> str:
    """Return the extraction vocabulary string for a framework, or ""."""
    pack = get_pack(profession)
    key = (framework or "").strip().upper()
    return pack["extract_vocab_table"].get(key, "")


# ---------------------------------------------------------------------------
# Correction prompt assembly
# ---------------------------------------------------------------------------


def _framework_key(framework: str | None) -> str:
    return (framework or "").strip().upper()


def client_layer(client_ctx: dict | None, profession: str | None = None) -> str:
    """Layer (b): a client-specific block so misheard names, diagnoses, and
    medications from THIS client's chart are corrected toward what is on file.

    Returns an empty string when there is nothing useful on the profile.
    """
    if not client_ctx:
        return ""
    pack = get_pack(profession)
    diagnosis_terms = pack["diagnosis_terms"]
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
            term = diagnosis_terms.get(code.upper())
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


def framework_layer(framework: str | None, profession: str | None = None) -> str:
    """Layer (c): elevate the active framework's term list so ambiguous words
    are read as that modality's vocabulary."""
    pack = get_pack(profession)
    key = _framework_key(framework)
    terms = pack["frameworks"].get(key)
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


def _dictionary_layer(dictionary_text: str | None) -> str:
    """Final layer: the user's own custom correction dictionary, verbatim."""
    text = (dictionary_text or "").strip()
    if not text:
        return ""
    return (
        "\nUSER DICTIONARY (the therapist's own preferred spellings and terms; "
        "bias ambiguous words toward these, but still change ONLY clear "
        "mis-transcriptions):\n" + text + "\n"
    )


def correction_layers(
    profession: str | None,
    framework: str | None,
    client_ctx: dict | None,
    dictionary_text: str | None = None,
) -> str:
    """Assemble the full correction system prompt for the given profession.

    Order: the shared correction rules, the profession's reference vocabulary,
    this client's chart facts, the active framework's elevated term list, and
    finally the user's custom dictionary when it has content.
    """
    pack = get_pack(profession)
    rules = _CORRECTION_RULES_PATH.read_text(encoding="utf-8").rstrip()
    parts = [
        rules,
        pack["correction_glossary"].rstrip(),
        client_layer(client_ctx, profession),
        framework_layer(framework, profession),
        _dictionary_layer(dictionary_text),
    ]
    return "\n".join(p for p in parts if p)
