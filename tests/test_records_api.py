"""Tests for the records API endpoints (TestClient, tmp vault, mocked shell)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module


def _healthy_checks():
    return [
        {"name": "Model server reachable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Gemma model loaded", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
    ]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.records, "VAULT_DIR", vault_dir)
    import debrief.audit as audit_mod

    monkeypatch.setattr(audit_mod, "VAULT_DIR", vault_dir)
    app_module.vault.ensure_vault()
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    return TestClient(app_module.app), vault_dir


def _first_session_path(vault_dir):
    sessions = vault_dir / "Clients" / "C-0003" / "Sessions"
    return str(sorted(sessions.glob("*.md"))[0].relative_to(vault_dir))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_client_detail_shape(client):
    tc, _ = client
    resp = tc.get("/api/clients/C-0003")
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "C-0003"
    assert body["profile"]["name"] == "Maya Chen"
    assert len(body["sessions"]) == 2
    assert isinstance(body["documents"], list)


def test_client_detail_404(client):
    tc, _ = client
    assert tc.get("/api/clients/C-9999").status_code == 404


def test_note_view_returns_html_fragment(client):
    tc, vault_dir = client
    rel = _first_session_path(vault_dir)
    resp = tc.get("/api/notes", params={"path": rel})
    assert resp.status_code == 200
    body = resp.json()
    assert "<h2" in body["html"] or "<p>" in body["html"]
    assert body["kind"] == "session-note"
    assert body["frontmatter"]["client_id"] == "C-0003"


def test_note_view_path_guard(client):
    tc, _ = client
    assert tc.get("/api/notes", params={"path": "../secret.md"}).status_code == 400


def test_malformed_markdown_documents_still_list(client):
    tc, vault_dir = client
    docs_dir = vault_dir / "Clients" / "C-0001" / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    # Broken YAML frontmatter and scalar (non-dict) frontmatter must not 500.
    (docs_dir / "broken.md").write_text(
        "---\ntitle: [unclosed\n---\n\n# Broken Doc\n\nBody.\n", encoding="utf-8"
    )
    (docs_dir / "scalar.md").write_text(
        "---\nhello\n---\n\n# Scalar Doc\n", encoding="utf-8"
    )
    resp = tc.get("/api/clients/C-0001")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    by_title = {d["title"]: d for d in docs}
    assert "Broken Doc" in by_title and "Scalar Doc" in by_title
    assert by_title["Broken Doc"]["kind"] == "markdown"
    assert by_title["Scalar Doc"]["kind"] == "markdown"
    # /api/notes on the malformed file stays usable (200, renders a fragment).
    note = tc.get("/api/notes", params={"path": "Clients/C-0001/Documents/broken.md"})
    assert note.status_code == 200
    assert "Broken Doc" in note.json()["html"]


# ---------------------------------------------------------------------------
# Amend + save
# ---------------------------------------------------------------------------


def test_amend_appends(client):
    tc, vault_dir = client
    rel = _first_session_path(vault_dir)
    resp = tc.post("/api/notes/amend", json={"path": rel, "text": "Extra context."})
    assert resp.status_code == 200
    text = (vault_dir / rel).read_text(encoding="utf-8")
    assert "## Amendment" in text and "Extra context." in text


def test_document_save_rejected_for_session_note(client):
    tc, vault_dir = client
    rel = _first_session_path(vault_dir)
    resp = tc.post("/api/documents/save", json={"path": rel, "markdown": "# Hacked"})
    assert resp.status_code == 400


def test_document_save_allows_library_markdown(client):
    tc, vault_dir = client
    resp = tc.post(
        "/api/documents/save",
        json={"path": "Interventions/cognitive-restructuring.md", "markdown": "# CR\n\nNew body.\n"},
    )
    assert resp.status_code == 200
    assert "New body." in (vault_dir / "Interventions" / "cognitive-restructuring.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Upload + rename
# ---------------------------------------------------------------------------


def test_upload_and_rename(client):
    tc, vault_dir = client
    resp = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("Intake Form.pdf", b"%PDF-1.7 body", "application/pdf")},
    )
    assert resp.status_code == 200
    path = resp.json()["path"]
    assert path.endswith("intake-form.pdf")

    r2 = tc.post("/api/documents/rename", json={"path": path, "new_title": "Signed Intake"})
    assert r2.status_code == 200
    assert r2.json()["path"].endswith("signed-intake.pdf")


def test_upload_rejects_bad_type(client):
    tc, _ = client
    resp = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PDF endpoint: passthrough vs rendered vs 400
# ---------------------------------------------------------------------------


def test_pdf_passthrough_for_uploaded_pdf(client):
    tc, _ = client
    up = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("scan.pdf", b"%PDF-1.7 passthrough", "application/pdf")},
    )
    path = up.json()["path"]
    resp = tc.get("/api/documents/pdf", params={"path": path})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_pdf_rendered_for_markdown(client, monkeypatch):
    tc, vault_dir = client

    def _fake_render_pdf(md, title, dest):
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.7 rendered")
        return dest

    monkeypatch.setattr(app_module.render, "render_pdf", _fake_render_pdf)
    rel = _first_session_path(vault_dir)
    resp = tc.get("/api/documents/pdf", params={"path": rel})
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")


def test_pdf_400_for_image(client):
    tc, _ = client
    up = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("pic.png", b"\x89PNG body", "image/png")},
    )
    path = up.json()["path"]
    resp = tc.get("/api/documents/pdf", params={"path": path})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Email (mocked mail draft), reveal/open (mocked subprocess)
# ---------------------------------------------------------------------------


def test_email_document_drafts(client, monkeypatch):
    tc, _ = client
    sent = {}

    def _draft(to, subject, body, attachment):
        sent["to"] = to
        return True

    monkeypatch.setattr(app_module.actions, "create_mail_draft", _draft)
    up = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("resource.pdf", b"%PDF body", "application/pdf")},
    )
    path = up.json()["path"]
    resp = tc.post("/api/documents/email", json={"client_id": "C-0001", "path": path})
    assert resp.status_code == 200
    assert sent["to"] == "bob@example.com"


def test_reveal_and_open_mocked(client, monkeypatch):
    tc, _ = client
    calls = []
    monkeypatch.setattr(app_module.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    up = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("r.pdf", b"%PDF body", "application/pdf")},
    )
    path = up.json()["path"]
    assert tc.post("/api/reveal", json={"path": path}).status_code == 200
    assert tc.post("/api/open", json={"path": path}).status_code == 200
    assert any("-R" in c for c in calls), "reveal should use open -R"


# ---------------------------------------------------------------------------
# Trash round-trip + search + library
# ---------------------------------------------------------------------------


def test_trash_restore_endpoints(client):
    tc, vault_dir = client
    up = tc.post(
        "/api/documents/upload",
        data={"client_id": "C-0001"},
        files={"file": ("gone.pdf", b"%PDF body", "application/pdf")},
    )
    path = up.json()["path"]
    token = tc.post("/api/trash", json={"path": path}).json()["token"]
    assert not (vault_dir / path).exists()
    restored = tc.post("/api/trash/restore", json={"token": token})
    assert restored.status_code == 200
    assert (vault_dir / path).exists()


def test_search_grouped(client):
    tc, _ = client
    body = tc.get("/api/search", params={"q": "DBT"}).json()
    assert set(body) == {"clients", "notes", "library"}
    assert body["clients"] or body["notes"] or body["library"]


def test_library_shape(client):
    tc, _ = client
    body = tc.get("/api/library").json()
    assert "worksheets" in body and "reference" in body
    titles = [w["title"] for w in body["worksheets"]]
    assert any("Thought Record" in t or "thought" in t.lower() for t in titles)
