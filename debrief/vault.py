"""Obsidian vault: scaffold, read client context, write session notes.

Deterministic hands for the filesystem side of Debrief. All writes are atomic
(temp file + os.replace). Session notes are ALWAYS new files. Frontmatter uses
the exact schemas in IMPLEMENTATION_PLAN.md Appendix A. No em dashes anywhere.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import yaml

from .config import VAULT_DIR

# ---------------------------------------------------------------------------
# Low-level frontmatter + atomic write helpers
# ---------------------------------------------------------------------------


def _dump_frontmatter(data: dict) -> str:
    """Serialize a dict to a YAML frontmatter block (--- ... ---).

    Key order is preserved; leaf lists render inline (["a", "b"]) which reads
    cleanly as Obsidian properties.
    """
    body = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=None,
        allow_unicode=True,
    ).strip()
    return f"---\n{body}\n---\n"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body text) for a markdown file.

    If there is no frontmatter block, returns ({}, full text).
    """
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            fm = yaml.safe_load(parts[1]) or {}
            return fm, parts[2].lstrip("\n")
    return {}, text


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _iso_date(value) -> str:
    """Coerce a date/datetime/str to an ISO date string (YYYY-MM-DD)."""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def _iso_datetime(value) -> str:
    """Coerce a datetime/str to an ISO datetime string with a T separator."""
    if isinstance(value, _dt.datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

_FOLDERS = [
    "Clients",
    "Templates/Worksheets",
    "Interventions",
    "Themes",
    "Private",
]


def _thought_record_pdf(dest: Path) -> None:
    """Generate a clean one-page CBT thought-record worksheet PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TR_Title", parent=styles["Title"], fontSize=20, spaceAfter=6
    )
    sub_style = ParagraphStyle(
        "TR_Sub", parent=styles["Normal"], fontSize=10, textColor=colors.grey
    )
    cell_head = ParagraphStyle(
        "TR_Head", parent=styles["Normal"], fontSize=10, leading=12,
        textColor=colors.white, fontName="Helvetica-Bold",
    )
    cell_prompt = ParagraphStyle(
        "TR_Prompt", parent=styles["Normal"], fontSize=8.5, leading=11,
        textColor=colors.HexColor("#555555"),
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(dest), pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )

    rows_spec = [
        ("Situation", "What happened? Where, when, and who was involved?"),
        ("Automatic thoughts", "What went through your mind? Rate belief 0 to 100 percent."),
        ("Emotions", "What did you feel? Rate intensity 0 to 100 percent."),
        ("Evidence for the thought", "Facts that support the automatic thought."),
        ("Evidence against the thought", "Facts that do not support the automatic thought."),
        ("Balanced thought", "A fairer, more accurate way to see the situation."),
        ("Outcome", "Re-rate your emotion now. What will you do next?"),
    ]

    header = Paragraph("Thought Record", title_style)
    subtitle = Paragraph(
        "A CBT worksheet for noticing and re-examining automatic thoughts. "
        "Bring it to your next session.",
        sub_style,
    )

    data = [[Paragraph("Prompt", cell_head), Paragraph("Your notes", cell_head)]]
    for label, prompt in rows_spec:
        left = Paragraph(f"<b>{label}</b><br/><font size=8 color='#777777'>{prompt}</font>", cell_prompt)
        data.append([left, Paragraph("", cell_prompt)])

    table = Table(data, colWidths=[2.6 * inch, 4.7 * inch], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4f6f")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b8c4d0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f4f7fa")))
    table.setStyle(TableStyle(style))

    doc.build([header, subtitle, Spacer(1, 0.2 * inch), table])


def _seed_client(
    client_dir: Path,
    profile_fm: dict,
    summary: str,
    goals: list[dict],
) -> None:
    """Create a mock client's _Profile.md and Treatment-Plan.md if missing."""
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "Sessions").mkdir(exist_ok=True)

    profile_path = client_dir / "_Profile.md"
    if not profile_path.exists():
        body = (
            f"{summary}\n\n"
            "## Sessions\n\n"
            "Individual session notes live in the Sessions/ folder. "
            "Obsidian backlinks and the file list handle discovery.\n"
        )
        _atomic_write(profile_path, _dump_frontmatter(profile_fm) + "\n" + body)

    plan_path = client_dir / "Treatment-Plan.md"
    if not plan_path.exists():
        plan_fm = {
            "type": "treatment-plan",
            "client_id": profile_fm["client_id"],
            "framework": profile_fm["framework"],
            "review_date": _iso_date(profile_fm.get("intake_date")),
        }
        lines = [_dump_frontmatter(plan_fm), "\n# Treatment Plan\n"]
        for i, goal in enumerate(goals, start=1):
            lines.append(f"\n## Goal {i}: {goal['goal']}\n")
            lines.append(f"- Status: {goal['status']}\n")
            lines.append("- Objectives:\n")
            for obj in goal["objectives"]:
                lines.append(f"  - {obj}\n")
        _atomic_write(plan_path, "".join(lines))


def _seed_session_note(
    client_dir: Path,
    client_id: str,
    session_date: _dt.date,
    session_number: int,
    framework: str,
    data: str,
    assessment: str,
    plan: str,
    interventions: list[str],
    themes: list[str],
) -> None:
    """Seed a short past session note if missing, matching write_session_note
    frontmatter conventions exactly."""
    sessions = client_dir / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{session_date.isoformat()}-session.md"
    if path.exists():
        return
    frontmatter = {
        "type": "session-note",
        "client_id": client_id,
        "session_date": _iso_date(session_date),
        "session_number": session_number,
        "format": "DAP",
        "modality": "in-person",
        "duration_min": 50,
        "framework": framework,
        "interventions": interventions,
        "themes": themes,
        "risk_assessment": "none-discussed",
        "actions_taken": ["note-filed"],
        "tags": [f"client/{client_id}", "type/session"],
    }
    parts = [
        _dump_frontmatter(frontmatter),
        f"\n## Data\n\n{data}\n",
        f"\n## Assessment\n\n{assessment}\n",
        f"\n## Plan\n\n{plan}\n",
        "\n## Next Session Considerations\n\n",
        "\nClinical judgment and final session planning remain the "
        "therapist's responsibility.\n",
    ]
    _atomic_write(path, "".join(parts))


def _seed_stub(path: Path, fm: dict, heading: str) -> None:
    """Create a small stub note (theme or intervention) so backlinks resolve."""
    if not path.exists():
        _atomic_write(path, _dump_frontmatter(fm) + f"\n# {heading}\n")


def ensure_vault() -> None:
    """Create the full vault scaffold and mock clients if they do not exist.

    Idempotent: existing files are never overwritten.
    """
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    for folder in _FOLDERS:
        (VAULT_DIR / folder).mkdir(parents=True, exist_ok=True)

    # Private folder guidance (never LLM-touched; psychotherapy-notes separation).
    private_readme = VAULT_DIR / "Private" / "README.md"
    if not private_readme.exists():
        _atomic_write(
            private_readme,
            "# Private\n\n"
            "Process and psychotherapy notes kept separate from the record "
            "(45 CFR 164.501). Debrief never reads or writes anything in this "
            "folder.\n",
        )

    # Worksheet PDF for the mail-draft action.
    pdf_path = VAULT_DIR / "Templates" / "Worksheets" / "thought-record.pdf"
    if not pdf_path.exists():
        try:
            _thought_record_pdf(pdf_path)
        except Exception:
            # PDF generation is best-effort; a markdown fallback still attaches.
            md_path = pdf_path.with_suffix(".md")
            if not md_path.exists():
                _atomic_write(
                    md_path,
                    "# Thought Record\n\nSituation, Automatic thoughts, Emotions, "
                    "Evidence for, Evidence against, Balanced thought, Outcome.\n",
                )

    # Theme + intervention stubs referenced by the mock clients.
    _seed_stub(
        VAULT_DIR / "Themes" / "Work-Undermining.md",
        {"type": "theme", "tags": ["theme/work-undermining"]},
        "Work-Undermining",
    )
    _seed_stub(
        VAULT_DIR / "Themes" / "Anxiety.md",
        {"type": "theme", "tags": ["theme/anxiety"]},
        "Anxiety",
    )
    _seed_stub(
        VAULT_DIR / "Themes" / "Sleep.md",
        {"type": "theme", "tags": ["theme/sleep"]},
        "Sleep",
    )
    _seed_stub(
        VAULT_DIR / "Interventions" / "cognitive-restructuring.md",
        {"type": "intervention", "framework": "CBT"},
        "Cognitive Restructuring",
    )
    _seed_stub(
        VAULT_DIR / "Interventions" / "values-clarification.md",
        {"type": "intervention", "framework": "ACT"},
        "Values Clarification",
    )
    _seed_stub(
        VAULT_DIR / "Themes" / "Emotion-Dysregulation.md",
        {"type": "theme", "tags": ["theme/emotion-dysregulation"]},
        "Emotion-Dysregulation",
    )
    _seed_stub(
        VAULT_DIR / "Interventions" / "dbt-skills-training.md",
        {"type": "intervention", "framework": "DBT"},
        "DBT Skills Training",
    )

    # Mock client C-0001: Bob Smith, CBT, SI history flag.
    _seed_client(
        VAULT_DIR / "Clients" / "C-0001",
        {
            "type": "client-profile",
            "client_id": "C-0001",
            "name": "Bob Smith",
            "email": "bob@example.com",
            "status": "active",
            "intake_date": _dt.date(2026, 1, 15),
            "last_session": _dt.date(2026, 7, 17),
            "next_session": "2026-07-21T15:00:00",
            "diagnosis": ["F41.1"],
            "presenting_concerns": ["workplace stress", "worthlessness"],
            "framework": "CBT",
            "themes": ["Work-Undermining"],
            "risk_flags": ["SI-passive-2026-07-17"],
            "summary_updated": "2026-07-17T18:30:00",
        },
        summary=(
            "Bob is a mid-career professional presenting with workplace stress and "
            "recurrent feelings of worthlessness tied to his performance at work. "
            "Treatment uses cognitive behavioral therapy with a focus on identifying "
            "and restructuring self-critical automatic thoughts. He has a documented "
            "history of passive suicidal ideation (assessed 2026-07-17, no plan or "
            "intent, protective factors in place) which is monitored each session. "
            "Engagement is consistent and he completes between-session worksheets."
        ),
        goals=[
            {
                "goal": "Reduce frequency and intensity of self-critical automatic thoughts.",
                "status": "in-progress",
                "objectives": [
                    "Complete a thought record after each significant work stressor.",
                    "Identify at least two cognitive distortions per week and generate balanced alternatives.",
                ],
            },
            {
                "goal": "Rebuild a sense of worth independent of work performance.",
                "status": "in-progress",
                "objectives": [
                    "Schedule two value-based activities each week unrelated to work.",
                    "Track mood before and after activities to test predictions.",
                ],
            },
        ],
    )

    # Mock client C-0002: Jane Doe, ACT.
    _seed_client(
        VAULT_DIR / "Clients" / "C-0002",
        {
            "type": "client-profile",
            "client_id": "C-0002",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "status": "active",
            "intake_date": _dt.date(2026, 2, 3),
            "last_session": _dt.date(2026, 7, 15),
            "next_session": "2026-07-22T14:00:00",
            "diagnosis": ["F41.9"],
            "presenting_concerns": ["anxiety", "sleep difficulties"],
            "framework": "ACT",
            "themes": ["Anxiety", "Sleep"],
            "risk_flags": [],
            "summary_updated": "2026-07-15T17:00:00",
        },
        summary=(
            "Jane presents with generalized anxiety and disrupted sleep, often driven "
            "by anticipatory worry about the following day. Treatment uses acceptance "
            "and commitment therapy, building willingness to experience anxious "
            "sensations without avoidance and clarifying values to guide committed "
            "action. No risk concerns have been identified. She responds well to "
            "defusion exercises and is working on a consistent wind-down routine."
        ),
        goals=[
            {
                "goal": "Increase willingness to experience anxiety without avoidance.",
                "status": "in-progress",
                "objectives": [
                    "Practice a defusion exercise daily when worry appears.",
                    "Approach one avoided situation each week and record the outcome.",
                ],
            },
            {
                "goal": "Establish a consistent, restorative sleep routine.",
                "status": "in-progress",
                "objectives": [
                    "Keep a fixed wind-down window free of screens.",
                    "Log sleep onset and quality to review patterns together.",
                ],
            },
        ],
    )

    # Mock client C-0003: Maya Chen, DBT.
    _seed_client(
        VAULT_DIR / "Clients" / "C-0003",
        {
            "type": "client-profile",
            "client_id": "C-0003",
            "name": "Maya Chen",
            "email": "maya@example.com",
            "status": "active",
            "intake_date": _dt.date(2026, 3, 10),
            "last_session": _dt.date(2026, 7, 14),
            "next_session": "2026-07-23T16:00:00",
            "diagnosis": ["F60.3"],
            "presenting_concerns": ["emotion dysregulation", "relationship conflict"],
            "framework": "DBT",
            "themes": ["Emotion-Dysregulation"],
            "risk_flags": [],
            "summary_updated": "2026-07-14T17:30:00",
        },
        summary=(
            "Maya is a graduate student presenting with intense emotional swings and "
            "recurring conflict in close relationships, often followed by shame and "
            "withdrawal. Treatment uses dialectical behavior therapy: weekly diary "
            "cards, chain analysis of target behaviors, and skills training across "
            "distress tolerance, emotion regulation, and interpersonal effectiveness. "
            "No current risk concerns. She engages well with chain analysis and has "
            "started using TIPP skills during escalation instead of sending messages "
            "she later regrets."
        ),
        goals=[
            {
                "goal": "Reduce the frequency and intensity of emotional escalation episodes.",
                "status": "in-progress",
                "objectives": [
                    "Complete a diary card every day and review it in session.",
                    "Use a distress tolerance skill before responding during conflict.",
                ],
            },
            {
                "goal": "Build stable, effective communication in close relationships.",
                "status": "in-progress",
                "objectives": [
                    "Practice one DEAR MAN request each week and record the outcome.",
                    "Identify early warning signs of escalation using chain analysis.",
                ],
            },
        ],
    )
    _seed_session_note(
        VAULT_DIR / "Clients" / "C-0003",
        "C-0003",
        _dt.date(2026, 6, 30),
        16,
        "DBT",
        data=(
            "Maya reviewed her diary card, which showed two escalation episodes this "
            "week, both triggered by delayed text replies from her partner. She used "
            "paced breathing once before responding and rated the urge to send an "
            "angry message as reduced from 8 to 4."
        ),
        assessment=(
            "Skill use is generalizing to in-the-moment triggers. Interpretations of "
            "delayed replies as rejection remain the main vulnerability."
        ),
        plan=(
            "Continue daily diary cards. Chain analysis of the second episode next "
            "session. Assign one DEAR MAN practice with her partner this week."
        ),
        interventions=["dbt-skills-training"],
        themes=["Emotion-Dysregulation"],
    )
    _seed_session_note(
        VAULT_DIR / "Clients" / "C-0003",
        "C-0003",
        _dt.date(2026, 7, 14),
        17,
        "DBT",
        data=(
            "Maya completed the DEAR MAN practice and reported it went better than "
            "expected: her partner agreed to a check-in routine. Diary card showed "
            "one escalation episode, resolved with TIPP skills within twenty minutes."
        ),
        assessment=(
            "Clear progress on both treatment goals. Interpersonal effectiveness "
            "skills are moving from rehearsal into real interactions."
        ),
        plan=(
            "Reinforce the check-in routine. Begin emotion regulation module work on "
            "opposite action for shame following conflict."
        ),
        interventions=["dbt-skills-training"],
        themes=["Emotion-Dysregulation"],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _client_dir(client_id: str) -> Path:
    return VAULT_DIR / "Clients" / client_id


def list_clients() -> list[dict]:
    """Return one dict per client, parsed from Clients/*/_Profile.md frontmatter."""
    clients: list[dict] = []
    clients_root = VAULT_DIR / "Clients"
    if not clients_root.exists():
        return clients
    for profile in sorted(clients_root.glob("*/_Profile.md")):
        text = profile.read_text(encoding="utf-8")
        fm, _ = _split_frontmatter(text)
        if fm:
            clients.append(fm)
    return clients


def _latest_session_note(client_id: str) -> Path | None:
    """Return the most recent session note path, or None."""
    sessions = _client_dir(client_id) / "Sessions"
    if not sessions.exists():
        return None
    notes = sorted(sessions.glob("*.md"))
    return notes[-1] if notes else None


def client_context(client_id: str) -> dict:
    """Return profile frontmatter, summary body, and the last session note text.

    Shape:
      {
        "client_id", "name", "email", "framework", ...profile fields...,
        "profile": <full frontmatter dict>,
        "summary": <running summary paragraph from the profile body>,
        "last_session_note": <text of most recent session note, or "">,
      }
    """
    profile_path = _client_dir(client_id) / "_Profile.md"
    if not profile_path.exists():
        raise FileNotFoundError(f"No profile for client {client_id!r}")

    fm, body = _split_frontmatter(profile_path.read_text(encoding="utf-8"))

    # Summary = everything before the "## Sessions" heading.
    summary = body.split("## Sessions", 1)[0].strip()

    last_note = _latest_session_note(client_id)
    last_text = last_note.read_text(encoding="utf-8") if last_note else ""

    ctx = dict(fm)
    ctx["profile"] = fm
    ctx["summary"] = summary
    ctx["last_session_note"] = last_text
    return ctx


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _session_note_path(client_id: str, session_date) -> Path:
    """Compute a new, non-colliding session note path (suffix -2, -3, ...)."""
    sessions = _client_dir(client_id) / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    date_str = _iso_date(session_date)
    base = sessions / f"{date_str}-session.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = sessions / f"{date_str}-session-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def write_session_note(
    client_id: str, note: dict, transcript: str, meta: dict
) -> Path:
    """Write a DAP session note as a NEW file. Returns the path.

    note: EXTRACT_SCHEMA "note" dict (data/assessment/plan/risk_present/risk/
          interventions/themes/client_quotes).
    meta: session-level metadata. Recognized keys (all optional except noted):
          session_date (date/str, defaults today), session_number (int),
          format ("DAP"|"SOAP"), modality, duration_min, framework,
          actions_taken (list[str]), risk_assessment (override),
          next_session_suggestions (list[str]),
          audio_filename (truthy -> the note references audio/<stem>.m4a and
          renders a ## Dictation Audio embed; the caller then places the
          converted m4a at Sessions/audio/<returned stem>.m4a).
    """
    session_date = meta.get("session_date") or _dt.date.today()
    fmt = meta.get("format", "DAP")

    # Reserve the note path up front so the archived dictation audio can share
    # the exact stem (including any -2 collision suffix). The caller moves the
    # converted m4a to Sessions/audio/<stem>.m4a after this returns.
    path = _session_note_path(client_id, session_date)
    audio_name = f"{path.stem}.m4a" if meta.get("audio_filename") else None

    risk_present = bool(note.get("risk_present"))
    if meta.get("risk_assessment"):
        risk_assessment = meta["risk_assessment"]
    elif risk_present:
        risk_assessment = "present-see-note"
    else:
        risk_assessment = "none-discussed"

    frontmatter = {
        "type": "session-note",
        "client_id": client_id,
        "session_date": _iso_date(session_date),
        "session_number": meta.get("session_number"),
        "format": fmt,
        "modality": meta.get("modality", "in-person"),
        "duration_min": meta.get("duration_min", 50),
        "framework": meta.get("framework"),
        "interventions": note.get("interventions", []),
        "themes": note.get("themes", []),
        "risk_assessment": risk_assessment,
        "actions_taken": meta.get("actions_taken", []),
        "tags": [f"client/{client_id}", "type/session"],
    }
    if audio_name:
        frontmatter["audio"] = f"audio/{audio_name}"

    # Body sections.
    parts: list[str] = [_dump_frontmatter(frontmatter)]
    parts.append(f"\n## Data\n\n{note.get('data', '').strip()}\n")
    parts.append(f"\n## Assessment\n\n{note.get('assessment', '').strip()}\n")
    parts.append(f"\n## Plan\n\n{note.get('plan', '').strip()}\n")

    if risk_present:
        risk = note.get("risk") or {}
        parts.append("\n## Risk\n\n")
        risk_lines = [
            ("Assessed", "Yes" if risk.get("assessed") else "Not documented"),
            ("Ideation", risk.get("ideation", "")),
            ("Plan, intent, means", risk.get("plan_intent_means", "")),
            ("Protective factors", risk.get("protective_factors", "")),
            ("Interventions taken", risk.get("interventions_taken", "")),
        ]
        for label, value in risk_lines:
            value = (value or "").strip()
            if value:
                parts.append(f"- {label}: {value}\n")

    # Next Session Considerations: suggestions (options only) + required line.
    suggestions = (
        meta.get("next_session_suggestions")
        or note.get("next_session_suggestions")
        or []
    )
    parts.append("\n## Next Session Considerations\n\n")
    for s in suggestions:
        parts.append(f"- {s.strip()}\n")
    parts.append(
        "\nClinical judgment and final session planning remain the "
        "therapist's responsibility.\n"
    )

    # Grounding evidence: the raw transcript, folded so it does not clutter.
    if transcript and transcript.strip():
        parts.append("\n## Session Transcript\n\n")
        parts.append("<details><summary>Show transcript</summary>\n\n")
        parts.append(f"{transcript.strip()}\n\n")
        parts.append("</details>\n")

    # Archived dictation audio: Obsidian embed renders an inline player.
    if audio_name:
        parts.append("\n## Dictation Audio\n\n")
        parts.append(f"![[{audio_name}]]\n")

    _atomic_write(path, "".join(parts))
    return path


def update_profile(client_id: str, new_summary: str, updates: dict) -> None:
    """Rewrite a client profile: replace the summary paragraph and merge
    frontmatter updates (e.g. last_session, next_session, summary_updated,
    risk_flags). Atomic.
    """
    profile_path = _client_dir(client_id) / "_Profile.md"
    if not profile_path.exists():
        raise FileNotFoundError(f"No profile for client {client_id!r}")

    fm, body = _split_frontmatter(profile_path.read_text(encoding="utf-8"))

    # Merge frontmatter updates, coercing datetime types to ISO strings.
    for key, value in (updates or {}).items():
        if key in ("next_session", "summary_updated") and isinstance(
            value, _dt.datetime
        ):
            fm[key] = _iso_datetime(value)
        elif key in ("last_session", "intake_date") and isinstance(
            value, (_dt.date, _dt.datetime)
        ):
            fm[key] = _iso_date(value)
        else:
            fm[key] = value

    # Preserve the "## Sessions" tail; replace only the leading summary.
    tail = ""
    if "## Sessions" in body:
        tail = "## Sessions" + body.split("## Sessions", 1)[1]
    else:
        tail = (
            "## Sessions\n\n"
            "Individual session notes live in the Sessions/ folder. "
            "Obsidian backlinks and the file list handle discovery.\n"
        )

    new_body = f"{new_summary.strip()}\n\n{tail.strip()}\n"
    _atomic_write(profile_path, _dump_frontmatter(fm) + "\n" + new_body)


def obsidian_open_uri(path: Path) -> str:
    """Build an obsidian:// URI for a vault file and open it. Returns the URI."""
    path = Path(path)
    try:
        rel = path.relative_to(VAULT_DIR)
    except ValueError:
        rel = path
    vault_name = VAULT_DIR.name
    file_param = str(rel.with_suffix(""))
    uri = (
        f"obsidian://open?vault={quote(vault_name)}"
        f"&file={quote(file_param)}"
    )
    try:
        subprocess.run(["open", uri], check=False)
    except Exception:
        pass
    return uri
