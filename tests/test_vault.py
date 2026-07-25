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


# ---------------------------------------------------------------------------
# Seeded sample clients: labelled, and scheduled relative to the seeding date
# ---------------------------------------------------------------------------


def test_seeded_clients_are_marked_as_samples(vault):
    for client in vault.list_clients():
        assert client["sample"] is True, f"{client['client_id']} should be a sample"


def test_seeded_next_sessions_are_in_the_future(vault):
    today = dt.date.today()
    for client in vault.list_clients():
        raw = str(client["next_session"])
        booked = dt.datetime.fromisoformat(raw)
        assert booked.date() > today, (
            f"{client['client_id']} advertises a next session on {raw}, "
            "which is not in the future"
        )


def test_seeded_next_sessions_keep_their_times_of_day(vault):
    today = dt.date.today()
    by_id = {c["client_id"]: c for c in vault.list_clients()}
    expected = {
        "C-0001": (3, dt.time(15, 0)),
        "C-0002": (4, dt.time(14, 0)),
        "C-0003": (5, dt.time(16, 0)),
    }
    for cid, (offset, time_of_day) in expected.items():
        booked = dt.datetime.fromisoformat(str(by_id[cid]["next_session"]))
        assert booked == dt.datetime.combine(today + dt.timedelta(days=offset), time_of_day)


def test_seeded_last_sessions_are_in_the_past(vault):
    today = dt.date.today()
    for client in vault.list_clients():
        last = client["last_session"]
        assert isinstance(last, dt.date), "last_session should parse as a date"
        assert last < today, f"{client['client_id']} last session {last} is not in the past"


def test_seeded_risk_flag_matches_the_relative_last_session(vault):
    bob = next(c for c in vault.list_clients() if c["client_id"] == "C-0001")
    assert bob["risk_flags"] == [f"SI-passive-{bob['last_session'].isoformat()}"]
    # The prose summary quotes the same assessment date, so the record stays
    # internally consistent rather than citing a hardcoded July date.
    ctx = vault.client_context("C-0001")
    assert f"assessed {bob['last_session'].isoformat()}" in ctx["summary"]


def test_seeded_session_notes_match_the_relative_last_session(vault):
    maya = next(c for c in vault.list_clients() if c["client_id"] == "C-0003")
    sessions = vault.VAULT_DIR / "Clients" / "C-0003" / "Sessions"
    stems = sorted(p.stem for p in sessions.glob("*.md"))
    assert len(stems) == 2
    assert stems[-1] == f"{maya['last_session'].isoformat()}-session"


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


def test_create_client_writes_a_full_record(vault):
    created = vault.create_client(
        "  Alex   Rivera  ",
        email="alex@example.com",
        framework="CBT",
        presenting_concerns=["workplace stress", "low mood"],
    )
    assert created["client_id"] == "C-0004"
    # Whitespace is collapsed, not just stripped.
    assert created["name"] == "Alex Rivera"

    client_dir = vault.VAULT_DIR / "Clients" / "C-0004"
    assert (client_dir / "Sessions").is_dir()
    assert (client_dir / "Treatment-Plan.md").is_file()
    fm = _read_frontmatter(client_dir / "_Profile.md")
    assert fm["type"] == "client-profile"
    assert fm["email"] == "alex@example.com"
    assert fm["framework"] == "CBT"
    assert fm["presenting_concerns"] == ["workplace stress", "low mood"]
    assert fm["status"] == "active"
    assert fm["intake_date"] == dt.date.today()
    assert fm["last_session"] is None and fm["next_session"] is None
    assert fm["diagnosis"] == [] and fm["themes"] == [] and fm["risk_flags"] == []

    plan_fm = _read_frontmatter(client_dir / "Treatment-Plan.md")
    assert plan_fm["type"] == "treatment-plan"
    assert plan_fm["client_id"] == "C-0004"


def test_created_profile_keys_match_the_seeded_ones(vault):
    """Downstream readers must not have to special-case a hand-added client."""
    vault.create_client("Alex Rivera")
    seeded = list(_read_frontmatter(vault.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md"))
    created = list(_read_frontmatter(vault.VAULT_DIR / "Clients" / "C-0004" / "_Profile.md"))
    # The seeds carry one extra key: the sample marker.
    assert seeded == created + ["sample"]


def test_created_client_is_not_marked_as_a_sample(vault):
    vault.create_client("Alex Rivera")
    by_id = {c["client_id"]: c for c in vault.list_clients()}
    assert "sample" not in by_id["C-0004"]
    assert by_id["C-0001"]["sample"] is True


def test_created_client_is_readable_by_list_and_context(vault):
    vault.create_client("Alex Rivera", framework="ACT", presenting_concerns="anxiety")
    ids = sorted(c["client_id"] for c in vault.list_clients())
    assert ids == ["C-0001", "C-0002", "C-0003", "C-0004"]

    ctx = vault.client_context("C-0004")
    assert ctx["name"] == "Alex Rivera"
    assert ctx["framework"] == "ACT"
    assert ctx["summary"].startswith("Alex Rivera was added to the caseload on")
    assert "anxiety" in ctx["summary"]
    assert ctx["last_session_note"] == ""


def test_create_client_id_allocation_skips_gaps(vault):
    """Ids come from max+1, so deleting an earlier client never causes a clash."""
    import shutil

    vault.create_client("Alex Rivera")  # C-0004
    vault.create_client("Sam Patel")  # C-0005
    shutil.rmtree(vault.VAULT_DIR / "Clients" / "C-0002")
    shutil.rmtree(vault.VAULT_DIR / "Clients" / "C-0004")
    created = vault.create_client("Robin Okafor")
    assert created["client_id"] == "C-0006"
    assert (vault.VAULT_DIR / "Clients" / "C-0005" / "_Profile.md").is_file()


def test_create_client_ignores_non_client_folders(vault):
    (vault.VAULT_DIR / "Clients" / "Archive").mkdir()
    (vault.VAULT_DIR / "Clients" / "C-not-a-number").mkdir()
    assert vault.create_client("Alex Rivera")["client_id"] == "C-0004"


def test_create_client_leaves_no_staging_folder_in_clients(vault):
    vault.create_client("Alex Rivera")
    names = sorted(p.name for p in (vault.VAULT_DIR / "Clients").iterdir())
    assert names == ["C-0001", "C-0002", "C-0003", "C-0004"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "   "},
        {"name": None},
        {"name": "\n\t "},
        {"name": "A" * (200)},
        {"name": "Alex Rivera", "email": "not-an-email"},
        {"name": "Alex Rivera", "email": "alex@example"},
        {"name": "Alex Rivera", "email": "alex @example.com"},
        {"name": "Alex Rivera", "framework": "F" * 100},
        {"name": "Alex Rivera", "presenting_concerns": ["x" * 200]},
        {"name": "Alex Rivera", "presenting_concerns": [f"c{i}" for i in range(30)]},
        {"name": "Alex Rivera", "presenting_concerns": 42},
        {"name": "Alex Rivera", "presenting_concerns": [["nested"]]},
    ],
)
def test_create_client_rejects_bad_input(vault, kwargs):
    with pytest.raises(vault.ClientInputError):
        vault.create_client(**kwargs)


def test_create_client_rejects_leave_nothing_behind(vault):
    with pytest.raises(vault.ClientInputError):
        vault.create_client("")
    assert sorted(p.name for p in (vault.VAULT_DIR / "Clients").iterdir()) == [
        "C-0001",
        "C-0002",
        "C-0003",
    ]


def test_create_client_optional_fields_default_empty(vault):
    created = vault.create_client("Alex Rivera")
    assert created["email"] == ""
    assert created["framework"] == ""
    assert created["presenting_concerns"] == []


def test_create_client_splits_and_dedupes_a_concerns_string(vault):
    created = vault.create_client(
        "Alex Rivera", presenting_concerns=" anxiety , sleep ,, anxiety ,  "
    )
    assert created["presenting_concerns"] == ["anxiety", "sleep"]


def test_search_snippets_never_leak_frontmatter(vault):
    """Search results show prose, not raw YAML.

    Snippets used to be cut from the whole file, so a hit near the top produced
    results like "d] tags: [cli" in the sidebar.
    """
    vault.ensure_vault()
    hits = vault.search_vault("worthlessness")
    assert hits, "expected at least one hit"
    for h in hits:
        s = h["snippet"]
        for marker in ("---", "tags:", "client_id:", "type:", "session_date:"):
            assert marker not in s, f"frontmatter leaked into snippet: {s!r}"


def test_search_still_finds_a_client_by_name(vault):
    """Names often live only in frontmatter, so the title must stay searchable."""
    vault.ensure_vault()
    hits = vault.search_vault("Maya Chen")
    assert any("C-0003" in h["path"] for h in hits), "client name search regressed"


def test_search_matches_frontmatter_but_shows_prose(vault):
    """A frontmatter-only match still finds the record, and still shows prose."""
    vault.ensure_vault()
    hits = vault.search_vault("DBT")
    assert any("C-0003" in h["path"] for h in hits), "framework search must still work"
    for h in hits:
        assert "framework:" not in h["snippet"]
        assert not h["snippet"].startswith("---")
