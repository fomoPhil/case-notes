"""Tests for debrief.vocab: the single source of profession vocabulary.

The byte-parity tests embed the historical literals (previously in stt.py) so a
future edit that silently changes the migrated therapy vocabulary trips a red
test. These are deterministic and do not call the model.
"""

from __future__ import annotations

from debrief import vocab

# Historical stt.py _FRAMEWORK_TERMS, verbatim. The migration must preserve this.
_HISTORICAL_FRAMEWORK_TERMS = {
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

# Historical stt.py _DIAGNOSIS_TERMS, verbatim.
_HISTORICAL_DIAGNOSIS_TERMS = {
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


def test_therapy_cbt_framework_byte_parity():
    pack = vocab.get_pack("therapy")
    assert pack["frameworks"]["CBT"] == _HISTORICAL_FRAMEWORK_TERMS["CBT"]
    # Guard every framework, not just CBT.
    assert pack["frameworks"] == _HISTORICAL_FRAMEWORK_TERMS


def test_therapy_diagnosis_terms_byte_parity():
    pack = vocab.get_pack("therapy")
    assert pack["diagnosis_terms"] == _HISTORICAL_DIAGNOSIS_TERMS


def test_list_professions_order_and_clinical_flags():
    profs = vocab.list_professions()
    assert [p["id"] for p in profs] == ["therapy", "slp", "coaching", "legal_meeting"]
    by_id = {p["id"]: p for p in profs}
    assert by_id["therapy"]["clinical"] is True
    assert by_id["slp"]["clinical"] is True
    assert by_id["coaching"]["clinical"] is False
    assert by_id["legal_meeting"]["clinical"] is False


def test_get_pack_falls_back_to_therapy():
    assert vocab.get_pack("nonsense") is vocab.TERM_PACKS["therapy"]
    assert vocab.get_pack(None) is vocab.TERM_PACKS["therapy"]


def test_stub_packs_have_full_shape():
    required = {"name", "clinical", "frameworks", "diagnosis_terms", "correction_glossary", "extract_vocab_table"}
    for pid in ("slp", "coaching", "legal_meeting"):
        pack = vocab.TERM_PACKS[pid]
        assert required <= set(pack)
        assert pack["frameworks"], f"{pid} should ship at least one framework"


def test_extract_framework_vocab_matches_therapy():
    # The extract vocabulary string is the migrated extract.py _FRAMEWORK_VOCAB.
    assert vocab.extract_framework_vocab("CBT", "therapy").startswith("cognitive restructuring")
    assert vocab.extract_framework_vocab("nonsense", "therapy") == ""


def test_correction_layers_appends_dictionary_last():
    system = vocab.correction_layers(
        "therapy", "CBT", {"name": "Bob Smith"}, dictionary_text="Zoloft is sertraline"
    )
    assert "transcription correction pass" in system  # shared rules present
    assert "Reference clinical vocabulary" in system  # pack glossary present
    assert "Bob Smith" in system  # client layer
    assert "This clinician practices CBT" in system  # framework layer
    # Dictionary is the final layer.
    assert "USER DICTIONARY" in system
    assert system.index("USER DICTIONARY") > system.index("This clinician practices CBT")


def test_correction_layers_no_dictionary_when_empty():
    system = vocab.correction_layers("therapy", "CBT", None, dictionary_text="")
    assert "USER DICTIONARY" not in system
