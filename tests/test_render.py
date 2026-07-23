"""Tests for debrief.render: markdown to HTML, and PDF when available."""

from __future__ import annotations

import pytest

from debrief import render


def test_markdown_to_html_structure():
    html = render.markdown_to_html(
        "# Box Breathing\n\nBreathe in for **four** counts.\n\n- Inhale\n- Hold",
        "Box Breathing",
    )
    assert "<!DOCTYPE html>" in html
    assert "<title>Box Breathing</title>" in html
    assert "<h1>Box Breathing</h1>" in html
    assert "<strong>four</strong>" in html
    assert "<li>Inhale</li>" in html
    assert 'class="document"' in html
    # The stylesheet is inlined, not linked.
    assert "@font-face" in html
    assert "<link" not in html


def test_markdown_to_html_tables_extension():
    html = render.markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |", "Table")
    assert "<table>" in html
    assert "<th>A</th>" in html


def test_markdown_to_html_strips_em_dash():
    html = render.markdown_to_html("A sentence — with a dash.", "T")
    assert "—" not in html


def test_sanitize_strips_event_handlers_and_scripts():
    frag = render.markdown_to_fragment(
        "<img src=x onerror=alert(1)>\n\n<script>alert(2)</script>\n\nHello world"
    )
    assert "onerror" not in frag, "on* event handlers must be stripped"
    assert "<script" not in frag and "alert(2)" not in frag, "script dropped with content"
    assert "Hello world" in frag, "surrounding text survives"


def test_sanitize_preserves_normal_note_markdown():
    md = (
        "## Data\n\nClient reported progress.\n\n- item one\n- item two\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |"
    )
    frag = render.markdown_to_fragment(md)
    assert "<h2>Data</h2>" in frag
    assert "<li>item one</li>" in frag
    assert "<table>" in frag and "<td>1</td>" in frag


def test_sanitize_keeps_details_summary_for_transcripts():
    frag = render.markdown_to_fragment(
        "<details><summary>Show transcript</summary>\n\nThe words.\n\n</details>"
    )
    assert "<details>" in frag and "<summary>" in frag


def test_pdf_bytes_or_unavailable():
    """When PDF is available the bytes start with %PDF; otherwise it raises."""
    if render.pdf_available():
        data = render.render_pdf_bytes("# Hello\n\nWorld", "Hello")
        assert data[:4] == b"%PDF"
    else:
        with pytest.raises(render.PdfUnavailable) as exc:
            render.render_pdf_bytes("# Hello\n\nWorld", "Hello")
        assert exc.value.fix  # carries the doctor fix text


def test_render_pdf_writes_file_when_available(tmp_path):
    if not render.pdf_available():
        pytest.skip("weasyprint native stack not available on this machine")
    dest = tmp_path / "out.pdf"
    out = render.render_pdf("# Title\n\nBody text.", "Title", dest)
    assert out == dest
    assert dest.exists()
    assert dest.read_bytes()[:4] == b"%PDF"
