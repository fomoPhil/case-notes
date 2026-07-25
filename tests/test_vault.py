"""Tests for debrief.vault. Temp-dir based: never touches the real vault."""

from __future__ import annotations

import datetime as dt
import importlib
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Point the vault at a temp dir and return the freshly imported module."""
    import debrief.config as config

    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "vault")

    import debrief.vault as vault_mod

    # Rebind the module-level VAULT_DIR that vault.py imported by value.
    monkeypatch.setattr(vault_mod, "VAULT_DIR", tmp_path / "vault")
    vault_mod.ensure_vault()
    return vault_mod


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "file missing frontmatter"
    block = text.split("---", 2)[1]
    return yaml.safe_load(block)


def test_scaffold_folders_exist(vault):
    root = vault.VAULT_DIR
    for folder in ("Clients", "Templates/Worksheets", "Interventions", "Themes", "Private"):
        assert (root / folder).is_dir(), f"missing folder {folder}"


def test_worksheet_generated(vault):
    pdf = vault.VAULT_DIR / "Templates" / "Worksheets" / "thought-record.pdf"
    md = pdf.with_suffix(".md")
    assert pdf.exists() or md.exists(), "no worksheet attachment produced"
    if pdf.exists():
        assert pdf.read_bytes()[:4] == b"%PDF", "worksheet is not a valid PDF"


def test_list_clients_returns_all_mocks(vault):
    clients = vault.list_clients()
    ids = sorted(c["client_id"] for c in clients)
    assert ids == ["C-0001", "C-0002", "C-0003"]
    by_id = {c["client_id"]: c for c in clients}
    assert by_id["C-0001"]["name"] == "Bob Smith"
    assert by_id["C-0001"]["framework"] == "CBT"
    assert by_id["C-0001"]["risk_flags"], "Bob should carry an SI history flag"
    assert by_id["C-0002"]["framework"] == "ACT"
    assert by_id["C-0002"]["email"] == "jane@example.com"
    assert by_id["C-0003"]["name"] == "Maya Chen"
    assert by_id["C-0003"]["framework"] == "DBT"
    assert by_id["C-0003"]["email"].endswith("@example.com")


def test_third_client_has_seeded_sessions(vault):
    sessions = vault.VAULT_DIR / "Clients" / "C-0003" / "Sessions"
    notes = sorted(sessions.glob("*.md"))
    assert len(notes) == 2, "C-0003 should seed two past session notes"
    for note in notes:
        fm = _read_frontmatter(note)
        assert fm["type"] == "session-note"
        assert fm["client_id"] == "C-0003"
        assert fm["framework"] == "DBT"


def test_treatment_plans_have_two_goals(vault):
    for cid in ("C-0001", "C-0002", "C-0003"):
        plan = vault.VAULT_DIR / "Clients" / cid / "Treatment-Plan.md"
        text = plan.read_text(encoding="utf-8")
        assert text.count("## Goal ") == 2, f"{cid} plan should have 2 goals"


def test_ensure_vault_idempotent(vault):
    profile = vault.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md"
    before = profile.read_text(encoding="utf-8")
    vault.ensure_vault()
    after = profile.read_text(encoding="utf-8")
    assert before == after, "ensure_vault must not overwrite existing files"


def test_client_context_shape(vault):
    ctx = vault.client_context("C-0001")
    assert ctx["client_id"] == "C-0001"
    assert ctx["name"] == "Bob Smith"
    assert "Bob" in ctx["summary"]
    assert "## Sessions" not in ctx["summary"], "summary should stop before Sessions"
    assert "last_session_note" in ctx


def test_no_em_dashes_anywhere(vault):
    for md in vault.VAULT_DIR.rglob("*.md"):
        assert "—" not in md.read_text(encoding="utf-8"), f"em dash in {md}"


def _sample_note(risk: bool) -> dict:
    return {
        "data": "Client reported a difficult week at work with a critical performance review.",
        "assessment": "Consistent with recurrent self-critical automatic thoughts.",
        "plan": "Continue cognitive restructuring and assign a thought record.",
        "risk_present": risk,
        "risk": (
            {
                "assessed": True,
                "ideation": "Passive ideation, no active wish to die.",
                "plan_intent_means": "No plan, no intent, no access to means.",
                "protective_factors": "Strong therapeutic alliance and family support.",
                "interventions_taken": "Safety plan reviewed and updated.",
            }
            if risk
            else None
        ),
        "interventions": ["cognitive-restructuring"],
        "themes": ["Work-Undermining"],
        "client_quotes": ["I feel like I can never do enough."],
    }


def _sample_meta() -> dict:
    return {
        "session_date": dt.date(2026, 7, 18),
        "session_number": 15,
        "format": "DAP",
        "modality": "in-person",
        "duration_min": 50,
        "framework": "CBT",
        "actions_taken": ["note-filed", "followup-booked-2026-07-22T15:00", "email-drafted"],
        "next_session_suggestions": [
            "Consider reviewing the completed thought record together.",
            "Possible focus on values-based activity scheduling.",
        ],
    }


def test_write_session_note_valid_yaml_and_headings(vault):
    note = _sample_note(risk=True)
    path = vault.write_session_note("C-0001", note, "This is the transcript.", _sample_meta())
    assert path.exists()

    fm = _read_frontmatter(path)  # re-parse proves valid YAML
    assert fm["type"] == "session-note"
    assert fm["client_id"] == "C-0001"
    assert fm["format"] == "DAP"
    assert fm["risk_assessment"] == "present-see-note"
    assert fm["interventions"] == ["cognitive-restructuring"]
    assert "client/C-0001" in fm["tags"]

    text = path.read_text(encoding="utf-8")
    for heading in ("## Data", "## Assessment", "## Plan", "## Risk", "## Next Session Considerations"):
        assert heading in text, f"missing {heading}"
    # A filed note is a clinical record. Debrief must never inject liability
    # boilerplate the clinician did not write into it; that framing lives in
    # the UI only.
    assert "Clinical judgment" not in text, "no disclaimer boilerplate in the record"
    assert "responsibility" not in text, "no disclaimer boilerplate in the record"
    assert "This is the transcript." in text
    assert "—" not in text, "no em dashes allowed"


def test_risk_section_omitted_when_no_risk(vault):
    note = _sample_note(risk=False)
    path = vault.write_session_note("C-0002", note, "transcript", {
        "session_date": dt.date(2026, 7, 18),
        "framework": "ACT",
    })
    text = path.read_text(encoding="utf-8")
    assert "## Risk\n" not in text
    fm = _read_frontmatter(path)
    assert fm["risk_assessment"] == "none-discussed"


def test_audio_field_and_section_when_requested(vault):
    note = _sample_note(risk=False)
    meta = _sample_meta()
    meta["audio_filename"] = True
    path = vault.write_session_note("C-0001", note, "transcript", meta)

    fm = _read_frontmatter(path)
    assert fm["audio"] == f"audio/{path.stem}.m4a", "frontmatter must reference audio/<stem>.m4a"
    text = path.read_text(encoding="utf-8")
    assert "## Dictation Audio" in text
    assert f"![[{path.stem}.m4a]]" in text, "embed must use the note's own stem"

    # Without the meta key, no audio artifacts appear.
    path2 = vault.write_session_note("C-0002", note, "transcript", {"session_date": dt.date(2026, 7, 18)})
    fm2 = _read_frontmatter(path2)
    assert "audio" not in fm2
    assert "## Dictation Audio" not in path2.read_text(encoding="utf-8")


def test_write_session_note_prefers_meta_sections(vault):
    # Finding 1 belt-and-braces: the note must render the section list the plan
    # carried (meta["sections"]), not re-resolve the on-disk spec. Here meta
    # names Alpha/Beta while the "DAP" spec on disk would give Data/Assessment/
    # Plan; the filed note must match what was extracted and approved.
    note = {
        "alpha": "first body",
        "beta": "second body",
        "interventions": [],
        "themes": [],
        "client_quotes": [],
    }
    meta = {
        "session_date": dt.date(2026, 7, 18),
        "format": "DAP",
        "sections": [
            {"key": "alpha", "heading": "Alpha"},
            {"key": "beta", "heading": "Beta"},
        ],
    }
    path = vault.write_session_note("C-0001", note, "transcript", meta)
    text = path.read_text(encoding="utf-8")
    assert "## Alpha" in text and "first body" in text
    assert "## Beta" in text and "second body" in text
    # The on-disk DAP headings must NOT leak in over the plan's sections.
    assert "## Data" not in text and "## Assessment" not in text


def test_second_note_same_day_gets_suffix(vault):
    note = _sample_note(risk=False)
    meta = _sample_meta()
    p1 = vault.write_session_note("C-0001", note, "t1", meta)
    p2 = vault.write_session_note("C-0001", note, "t2", meta)
    assert p1 != p2
    assert p1.name == "2026-07-18-session.md"
    assert p2.name == "2026-07-18-session-2.md"


def test_update_profile_merges_and_replaces_summary(vault):
    vault.update_profile(
        "C-0001",
        "Updated running summary after the latest session.",
        {
            "last_session": dt.date(2026, 7, 18),
            "next_session": dt.datetime(2026, 7, 22, 15, 0, 0),
            "summary_updated": dt.datetime(2026, 7, 18, 18, 0, 0),
        },
    )
    profile = vault.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md"
    fm = _read_frontmatter(profile)
    assert str(fm["next_session"]).startswith("2026-07-22T15:00")
    text = profile.read_text(encoding="utf-8")
    assert "Updated running summary after the latest session." in text
    assert "## Sessions" in text, "Sessions tail must be preserved"


def test_read_client_file_reads_profile(vault):
    text = vault.read_client_file("C-0001", "_Profile.md")
    assert "Bob Smith" in text


def test_read_client_file_rejects_traversal(vault):
    for bad in ("../C-0002/_Profile.md", "/etc/passwd", "..", "Sessions/../../secret"):
        with pytest.raises((vault.VaultPathError, FileNotFoundError)):
            vault.read_client_file("C-0001", bad)


def test_read_client_file_rejects_bad_client_id(vault):
    with pytest.raises(vault.VaultPathError):
        vault.read_client_file("../Templates", "x.md")


def test_search_vault_finds_and_snippets(vault):
    hits = vault.search_vault("DBT")
    assert hits, "expected at least one DBT hit"
    assert all(set(h) == {"path", "title", "snippet"} for h in hits)
    assert any("C-0003" in h["path"] for h in hits)


def test_search_vault_empty_query(vault):
    assert vault.search_vault("") == []


def test_search_vault_skips_private(vault):
    hits = vault.search_vault("psychotherapy")
    assert all("Private" not in h["path"] for h in hits)


def test_obsidian_uri_format(vault, monkeypatch):
    # Do not actually launch Obsidian during the test.
    monkeypatch.setattr(vault.subprocess, "run", lambda *a, **k: None)
    path = vault.VAULT_DIR / "Clients" / "C-0001" / "Sessions" / "2026-07-18-session.md"
    uri = vault.obsidian_open_uri(path)
    assert uri.startswith("obsidian://open?vault=")
    assert "C-0001" in uri
    assert ".md" not in uri, "file param should drop the extension"
