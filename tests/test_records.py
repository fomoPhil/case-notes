"""Tests for debrief.records: documents, amendments, rename, trash, uploads."""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture()
def records(tmp_path, monkeypatch):
    """Point config + vault + records at a temp vault and scaffold it."""
    import debrief.config as config

    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)

    import debrief.vault as vault_mod
    import debrief.records as records_mod

    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(records_mod, "VAULT_DIR", vault_dir)
    vault_mod.ensure_vault()
    return records_mod


def _session_path(records) -> str:
    sessions = records.VAULT_DIR / "Clients" / "C-0003" / "Sessions"
    note = sorted(sessions.glob("*.md"))[0]
    return str(note.relative_to(records.VAULT_DIR))


# ---------------------------------------------------------------------------
# Listing + metadata
# ---------------------------------------------------------------------------


def test_list_documents_creates_folder_and_includes_sessions(records):
    docs = records.list_documents("C-0003")
    # Documents folder created lazily.
    assert (records.VAULT_DIR / "Clients" / "C-0003" / "Documents").is_dir()
    sessions = [d for d in docs if d["kind"] == "session-note"]
    assert len(sessions) == 2
    assert all(d["filed"] for d in sessions)
    assert all("Session" in d["title"] for d in sessions)


def test_agent_worksheet_pair_collapses_to_one_card(records):
    docs_dir = records.VAULT_DIR / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "box-breathing.md").write_text("# Box Breathing\n\nBreathe.\n", encoding="utf-8")
    (docs_dir / "box-breathing.pdf").write_bytes(b"%PDF-1.7 fake")
    docs = [d for d in records.list_documents("C-0001") if d["kind"] != "session-note"]
    assert len(docs) == 1, "the .md + .pdf worksheet pair should collapse to one card"
    card = docs[0]
    assert card["kind"] == "worksheet-pdf"
    assert card["agent_made"] is True
    assert card["path"].endswith("box-breathing.md"), "keeps the editable markdown as canonical"


def test_list_sessions_sorted_desc(records):
    sessions = records.list_sessions("C-0003")
    dates = [s["date"] for s in sessions]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Amendments (append-only)
# ---------------------------------------------------------------------------


def test_amendment_appends_and_preserves_history(records):
    rel = _session_path(records)
    before = (records.VAULT_DIR / rel).read_text(encoding="utf-8")
    records.append_amendment(rel, "Client clarified the timeline.", dt.datetime(2026, 7, 20))
    after = (records.VAULT_DIR / rel).read_text(encoding="utf-8")
    assert before in after, "original content must be preserved"
    assert "## Amendment (2026-07-20)" in after
    assert "Client clarified the timeline." in after


def test_amendment_rejected_on_non_markdown(records):
    meta = records.save_upload("C-0001", "scan.pdf", b"%PDF-1.7 data")
    with pytest.raises(records.VaultPathError):
        records.append_amendment(meta["path"], "no", dt.datetime.now())


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_session_keeps_filename_changes_title(records):
    rel = _session_path(records)
    new_rel = records.rename_title(rel, "Crisis follow-up")
    assert new_rel == rel, "session note filename must not change"
    view = records.read_note(rel)
    assert view["frontmatter"]["title"] == "Crisis follow-up"


def test_rename_document_renames_file_and_rewrites_links(records):
    # Write a markdown document and a wikilink to it in the client folder.
    docs_dir = records.VAULT_DIR / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "old-note.md").write_text("# Old Note\n\nBody.\n", encoding="utf-8")
    profile = records.VAULT_DIR / "Clients" / "C-0001" / "_Profile.md"
    profile.write_text(profile.read_text(encoding="utf-8") + "\nSee [[old-note|the note]].\n", encoding="utf-8")

    new_rel = records.rename_title("Clients/C-0001/Documents/old-note.md", "Shiny New Title")
    assert new_rel.endswith("shiny-new-title.md")
    assert (records.VAULT_DIR / new_rel).is_file()
    assert not (docs_dir / "old-note.md").exists()
    # Wikilink rewritten to the new stem.
    assert "[[shiny-new-title|the note]]" in profile.read_text(encoding="utf-8")


def _seed_worksheet_pair(records, client_id="C-0001", stem="box-breathing"):
    docs_dir = records.VAULT_DIR / "Clients" / client_id / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / f"{stem}.md").write_text(f"# {stem.title()}\n\nBreathe.\n", encoding="utf-8")
    (docs_dir / f"{stem}.pdf").write_bytes(b"%PDF-1.7 fake")
    return docs_dir


def test_rename_worksheet_by_md_moves_pdf_sibling(records):
    docs_dir = _seed_worksheet_pair(records)
    new_rel = records.rename_title("Clients/C-0001/Documents/box-breathing.md", "Calm Breathing")
    assert new_rel.endswith("calm-breathing.md")
    assert (docs_dir / "calm-breathing.md").is_file()
    assert (docs_dir / "calm-breathing.pdf").is_file(), "pdf sibling rides along"
    assert not (docs_dir / "box-breathing.md").exists()
    assert not (docs_dir / "box-breathing.pdf").exists()
    cards = [d for d in records.list_documents("C-0001") if d["kind"] != "session-note"]
    assert len(cards) == 1 and cards[0]["kind"] == "worksheet-pdf"


def test_rename_worksheet_by_pdf_still_moves_md_sibling(records):
    docs_dir = _seed_worksheet_pair(records)
    new_rel = records.rename_title("Clients/C-0001/Documents/box-breathing.pdf", "Calm Breathing")
    assert new_rel.endswith("calm-breathing.pdf")
    assert (docs_dir / "calm-breathing.pdf").is_file()
    assert (docs_dir / "calm-breathing.md").is_file(), "md source rides along"


def test_rename_library_file_rewrites_client_wikilinks(records):
    docs_dir = records.VAULT_DIR / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "homework.md").write_text(
        "# Homework\n\nComplete the [[thought-record]] this week.\n", encoding="utf-8"
    )
    assert (records.VAULT_DIR / "Templates" / "Worksheets" / "thought-record.md").is_file()
    records.rename_title("Templates/Worksheets/thought-record.md", "CBT Thought Log")
    text = (docs_dir / "homework.md").read_text(encoding="utf-8")
    assert "[[cbt-thought-log]]" in text
    assert "[[thought-record]]" not in text


# ---------------------------------------------------------------------------
# Trash / restore / sweep
# ---------------------------------------------------------------------------


def test_trash_and_restore_worksheet_by_md(records):
    docs_dir = _seed_worksheet_pair(records)
    token = records.trash("Clients/C-0001/Documents/box-breathing.md")
    assert not (docs_dir / "box-breathing.md").exists()
    assert not (docs_dir / "box-breathing.pdf").exists(), "pdf goes to trash too"
    restored = records.restore(token)
    assert restored.endswith("box-breathing.md")
    assert (docs_dir / "box-breathing.md").is_file()
    assert (docs_dir / "box-breathing.pdf").is_file(), "pdf comes back too"


def test_restore_with_name_collision_does_not_clobber(records):
    docs_dir = records.VAULT_DIR / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    orig = docs_dir / "daily.md"
    orig.write_text("# Old\n", encoding="utf-8")
    token = records.trash("Clients/C-0001/Documents/daily.md")
    # The freed name is reused before restore (mirrors _session_note_path reuse).
    orig.write_text("# Brand New\n", encoding="utf-8")
    restored = records.restore(token)
    assert restored != "Clients/C-0001/Documents/daily.md"
    assert restored.endswith("daily-restored.md")
    assert orig.read_text(encoding="utf-8") == "# Brand New\n", "newer file untouched"
    assert (records.VAULT_DIR / restored).read_text(encoding="utf-8") == "# Old\n"


def test_trash_and_restore_round_trip(records):
    meta = records.save_upload("C-0001", "photo.png", b"\x89PNG fake")
    rel = meta["path"]
    token = records.trash(rel)
    assert not (records.VAULT_DIR / rel).exists()
    restored = records.restore(token)
    assert restored == rel
    assert (records.VAULT_DIR / rel).exists()


def test_trash_rejects_protected_files(records):
    for name in ("_Profile.md", "Treatment-Plan.md"):
        with pytest.raises(records.VaultPathError):
            records.trash(f"Clients/C-0001/{name}")


def test_sweep_respects_30_days(records):
    meta = records.save_upload("C-0001", "temp.pdf", b"%PDF-1.7 x")
    # Trash 40 days ago -> swept; trash today -> kept.
    old = dt.datetime.now() - dt.timedelta(days=40)
    token_old = records.trash(meta["path"], now=old)
    meta2 = records.save_upload("C-0001", "temp2.pdf", b"%PDF-1.7 y")
    token_new = records.trash(meta2["path"], now=dt.datetime.now())

    removed = records.sweep_trash(dt.datetime.now(), days=30)
    assert removed == 1
    assert not (records.VAULT_DIR / "_Trash" / token_old).exists()
    assert (records.VAULT_DIR / "_Trash" / token_new).exists()


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def test_upload_allowlist_rejects_bad_type(records):
    with pytest.raises(records.VaultPathError):
        records.save_upload("C-0001", "malware.exe", b"MZ")


def test_upload_rejection_message_covers_iphone_photos(records):
    """HEIC is the iPhone camera default, so this is a likely first attempt."""
    with pytest.raises(records.VaultPathError) as excinfo:
        records.save_upload("C-0001", "consent-form.heic", b"ftypheic")
    message = str(excinfo.value)
    assert message == records.UPLOAD_REJECTED_MESSAGE
    assert "PDF, PNG, JPG, DOCX, and Markdown" in message
    assert "HEIC" in message and "export it as JPG" in message
    assert "Most Compatible" in message
    # No developer-speak, and no leaking of the raw extension.
    assert "file type not allowed" not in message
    assert ".heic" not in message


def test_heic_stays_off_the_allowlist(records):
    assert ".heic" not in records._UPLOAD_ALLOWLIST
    assert ".heif" not in records._UPLOAD_ALLOWLIST


def test_upload_sanitizes_and_collides(records):
    m1 = records.save_upload("C-0001", "My Weird File!.PDF", b"%PDF a")
    assert m1["path"].endswith("my-weird-file.pdf")
    m2 = records.save_upload("C-0001", "My Weird File!.PDF", b"%PDF b")
    assert m2["path"].endswith("my-weird-file-2.pdf")


def test_upload_rejects_bad_client_id(records):
    with pytest.raises(records.VaultPathError):
        records.save_upload("../etc", "x.pdf", b"%PDF")


# ---------------------------------------------------------------------------
# Render cache freshness
# ---------------------------------------------------------------------------


def test_render_cache_freshness(records, monkeypatch):
    calls = {"n": 0}

    def _fake_render_pdf(md, title, dest):
        calls["n"] += 1
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.7 fake")
        return dest

    monkeypatch.setattr(records.render, "render_pdf", _fake_render_pdf)

    docs_dir = records.VAULT_DIR / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    note = docs_dir / "hello.md"
    note.write_text("# Hello\n\nWorld.\n", encoding="utf-8")
    rel = "Clients/C-0001/Documents/hello.md"

    pdf1 = records.render_cache_pdf(rel)
    assert pdf1.read_bytes()[:4] == b"%PDF"
    assert calls["n"] == 1
    # Second call with unchanged source uses the cache.
    records.render_cache_pdf(rel)
    assert calls["n"] == 1
    # Touch the source newer than the cache -> re-render.
    import os
    import time

    future = time.time() + 10
    os.utime(note, (future, future))
    records.render_cache_pdf(rel)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------


def test_guard_rejects_traversal_and_absolute(records):
    for bad in ("../secret.md", "/etc/passwd", "Clients/../../oops.md"):
        with pytest.raises(records.VaultPathError):
            records._guard(bad)


def test_read_note_rejects_non_markdown(records):
    meta = records.save_upload("C-0001", "doc.pdf", b"%PDF a")
    with pytest.raises(records.VaultPathError):
        records.read_note(meta["path"])
