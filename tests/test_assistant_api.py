"""Tests for the assistant endpoints (TestClient, tmp vault, mocked model)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
from debrief import render


def _healthy_checks():
    return [
        {"name": "Model server reachable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Gemma model loaded", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "Vault writable", "ok": True, "detail": "ok", "fix": "", "hard": True},
        {"name": "ffmpeg on PATH", "ok": True, "detail": "ok", "fix": "", "hard": True},
    ]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    vault_dir = tmp_path / "vault"
    # Point both the app config and the vault module at the temp vault, then
    # scaffold it so profiles (client emails) and Documents dirs exist.
    monkeypatch.setattr(app_module.config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(app_module.vault, "VAULT_DIR", vault_dir)
    app_module.vault.ensure_vault()
    # Healthy model so the readiness guard passes.
    monkeypatch.setattr(app_module.doctor, "run_checks", _healthy_checks)
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    return TestClient(app_module.app), vault_dir


def test_plan_assistant_route(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        app_module.classify, "classify",
        lambda transcript, has_selected_client: {"route": "assistant", "client_hint": ""},
    )
    monkeypatch.setattr(
        app_module.agent, "run_agent",
        lambda text, now, client_hint=None: {
            "final_text": "Prepared a worksheet.",
            "proposals": [{"type": "worksheet", "title": "Box Breathing", "markdown_body": "Breathe.", "client_id": None}],
            "transcript": [{"step": "final", "text": "Prepared a worksheet."}],
        },
    )
    resp = tc.post("/api/assistant/plan", json={"text": "make a box breathing worksheet"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "assistant"
    assert body["final_text"] == "Prepared a worksheet."
    assert len(body["proposals"]) == 1
    assert body["raw_transcript"] == "make a box breathing worksheet"


def test_plan_session_debrief_route(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        app_module.classify, "classify",
        lambda transcript, has_selected_client: {"route": "session_debrief", "client_hint": "Bob"},
    )
    resp = tc.post(
        "/api/assistant/plan",
        json={"text": "Today Bob reported a hard week.", "client_id": "C-0001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "session_debrief"
    assert "transcript" in body


def test_plan_503_when_not_ready(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(
        app_module.doctor, "run_checks",
        lambda: [
            {"name": "Model server reachable", "ok": False, "detail": "no server", "fix": "Start LM Studio.", "hard": True},
            {"name": "Gemma model loaded", "ok": False, "detail": "no gemma", "fix": "Load gemma.", "hard": True},
        ],
    )
    app_module._doctor_cache["checks"] = None
    app_module._doctor_cache["at"] = 0.0
    resp = tc.post("/api/assistant/plan", json={"text": "hi"})
    assert resp.status_code == 503


def test_execute_files_worksheet_pdf(client, monkeypatch):
    tc, vault_dir = client

    def _fake_render_pdf(md, title, dest):
        from pathlib import Path
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.7 fake")
        return dest

    monkeypatch.setattr(app_module.render, "render_pdf", _fake_render_pdf)

    resp = tc.post(
        "/api/assistant/execute",
        json={
            "proposals": [
                {"type": "worksheet", "title": "Box Breathing", "markdown_body": "Breathe in.", "client_id": "C-0001"}
            ],
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "ok"
    pdf_path = results[0]["path"]
    assert pdf_path.endswith(".pdf")
    from pathlib import Path
    assert Path(pdf_path).exists()
    assert Path(pdf_path).read_bytes()[:4] == b"%PDF"
    # Filed under the client's Documents folder, inside the vault.
    assert str(vault_dir / "Clients" / "C-0001" / "Documents") in pdf_path
    # Markdown source written beside it.
    assert Path(pdf_path).with_suffix(".md").exists()
    # Audit entry written.
    activity = list((vault_dir / "_Activity").glob("*.md"))
    assert activity, "assistant run should append an activity log entry"
    assert "Assistant" in activity[0].read_text(encoding="utf-8")


def test_execute_falls_back_to_md_when_pdf_unavailable(client, monkeypatch):
    tc, vault_dir = client

    def _raise(md, title, dest):
        raise render.PdfUnavailable("no weasyprint")

    monkeypatch.setattr(app_module.render, "render_pdf", _raise)

    resp = tc.post(
        "/api/assistant/execute",
        json={"proposals": [{"type": "worksheet", "title": "Sleep Hygiene", "markdown_body": "Wind down."}]},
    )
    results = resp.json()["results"]
    assert results[0]["status"] == "ok"
    assert results[0]["path"].endswith(".md")
    # Shared library location when no client id.
    assert str(vault_dir / "Templates" / "Worksheets") in results[0]["path"]


def test_execute_email_resolves_client_email(client, monkeypatch):
    tc, _ = client
    sent = {}

    def _draft(to, subject, body, attachment):
        sent["to"] = to
        sent["subject"] = subject
        return True

    monkeypatch.setattr(app_module.actions, "create_mail_draft", _draft)

    resp = tc.post(
        "/api/assistant/execute",
        json={"proposals": [{"type": "email", "client_id": "C-0001", "subject": "Hi", "body": "Hello"}]},
    )
    results = resp.json()["results"]
    assert results[0]["status"] == "ok"
    assert sent["to"] == "bob@example.com"


def test_execute_path_safety_rejects_escape(client, monkeypatch):
    tc, vault_dir = client
    monkeypatch.setattr(app_module.render, "render_pdf", lambda md, title, dest: (_ for _ in ()).throw(render.PdfUnavailable()))

    resp = tc.post(
        "/api/assistant/execute",
        json={"proposals": [{"type": "worksheet", "title": "X", "markdown_body": "Y", "client_id": "../../etc"}]},
    )
    results = resp.json()["results"]
    # A malicious client id is rejected as an id and the file lands in the shared
    # library inside the vault, never outside it.
    assert results[0]["status"] == "ok"
    from pathlib import Path
    filed = Path(results[0]["path"]).resolve()
    assert str(vault_dir.resolve()) in str(filed)
