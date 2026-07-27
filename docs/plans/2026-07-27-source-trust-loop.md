# Source Trust Loop: Implementation Plan

> **For agentic workers:** Executed chunk by chunk with a senior review after each, same protocol as `2026-07-26-session-recording-phase-a.md`. This plan extends Phase A; read that plan and HANDOFF.md first.

**Goal:** Close the trust gaps Phil hit on first contact with Phase A: see the word-for-word text in app, correct a source's session date, connect a source to a session note (or draft a note from it), mark material as reference rather than session, reach every real file via Reveal in Finder, and see what the machine is doing in a developer panel.

**Architecture:** No new modules. Additive changes to sources.py, recstore.py, ladder.py, pipeline.py, app.py, static/app.js. The Source invariant table grows two reference kinds; the ladder grows a reference-shaped digest; the API grows four small routes.

---

## Design decisions (locked)

1. **Two dates, both true.** `captured_at` stays exactly what it is (when the material entered Debrief, never editable). New meta field `session_date` (date only): when the session actually happened, defaults to `captured_at`'s date, editable any time. Every user-facing surface (source card, summary dialog header, draft note frontmatter) shows `session_date`; upload time becomes a secondary detail line. List ordering stays by source id.
2. **Reference material is two kinds, same label.** `reference_recording` (verbatim True, rung 0: a transcribed video or audio) and `reference_document` (verbatim False, rung 2: an article, PDF, notes). Both render in UI as "Reference material". This respects the existing kind-to-(verbatim, rung) invariant table without weakening it.
3. **Reference digests are reference-shaped.** For reference kinds, ladder's reduce uses a reference schema: {source_id, duration_min, kind: "reference", title, summary, segments (timeline), key_points, practices_or_suggestions, topics}. NO client_statements, NO interventions_observed, NO risk block, and code enforces those absences the same way rule 2 is enforced. Reference sources are never offered as session evidence in the debrief flow.
4. **Assigning evidence to a filed note is an amendment.** Filed notes are never rewritten (key decision 5). "Assign to a session" appends a dated amendment through the existing amend machinery: one line naming the source, its session date, kind, and vault path. The source's meta records the link too (`linked_notes: [note relative path]`) so the source card can show it.
5. **Draft from a source is the rule 7 path with a door.** New route runs extract with the digest and an empty transcript: plan comes back `no_debrief: true`, zero actions, and the existing review screen (which already handles that state) lets the clinician edit and file. The draft note's `session_date` is the source's session date. Reference sources can be drafted from too (their digest carries no session claims; the review screen copy stays honest).
6. **Reveal in Finder is a rule, not a button.** Everywhere the UI represents something that exists on disk, the overflow or dialog offers "Reveal in Finder" through the existing `/api/reveal` route: source cards (the source folder), the word-for-word viewer and summary dialog (the exact file), session note cards and the note view, document cards, library items, and the Settings vault card (the vault root). Audit all of them; add where missing.
7. **The word-for-word viewer shows the corrected transcript by default** (`transcript.md`), with links to the raw transcript (`transcript-raw.md`) and original text (`source-text.md`) when present, in the same dialog pattern as the session summary. Copy explains the difference in one line each.
8. **Developer panel is surfacing, not new machinery.** A "Under the hood" card in Settings opening a dialog that shows: active model and reachability, STT engine and cache state, vault path, job list with state/stage/error and the last worker error, and the doctor checks the wizard already runs. One consolidated `GET /api/devinfo` assembles what `/api/status`, `/api/jobs`, and doctor already know. Auto-refresh every 4s while open, same discipline as the sources poll (fingerprint, no repaint during interaction).

## Chunk A: session date + reference kinds (backend)

- sources.py: KIND_RULES gains the two reference kinds; `set_session_date(client_id, source_id, date)` validates and rewrites meta; ingest defaults session_date from captured_at; declared_kind accepts the reference kinds (a reference declaration always wins, confidence 1.0, no confirmation); reclassify can flip a source to and from reference (stale rung 2 deleted, job requeued, same rules as verbatim flips).
- ladder.py: reference digest schema + reduce prompt (prompts/digest_reference.md), code-enforced absence of session-claim fields, render_digest_md handles both shapes.
- Tests: kind invariants, session date validation (rejects future dates? NO: allow any date, clinicians correct backwards and forwards; reject only unparseable), reference digest shape enforced post-model, reference never carries client_statements even when the model returns them, render both shapes.

## Chunk B: draft-from-source + assign-to-note (backend + routes)

- pipeline.py: `plan_from_source(client_id, source_id)` builds the rule 7 plan (empty transcript, digest evidence, no actions) without audio; session_meta.session_date from the source.
- app.py: `POST /api/sources/{id}/draft` (json client_id) returning the plan; `POST /api/sources/{id}/assign` (json client_id, note_path) appending the amendment and updating meta.linked_notes; `PATCH /api/sources/{id}/session-date` (json client_id, session_date); GET enrichment gains session_date and linked_notes.
- Tests: draft plan is no_debrief with zero actions and correct session_date; assign appends an amendment and never rewrites note body; amendment wording has no em dashes; linked_notes round-trips; a second assign to the same note is refused politely (409) rather than duplicating.

## Chunk C: frontend

- Word-for-word viewer: "View word for word" on every source card with rung 1 present (falls back to rung 0 label when only raw exists); dialog with corrected/raw/original tabs per decision 7; Reveal in Finder inside.
- Session date: shown on the card as the headline date; an Edit control opening a date field; summary dialog header uses it ("the session on <date>, added <upload date>").
- Assign and draft: source card overflow gains "Connect to a session" (picker over the client's existing session notes, then confirmation showing the amendment line) and "Draft a note from this" (posts /draft, routes into the existing review screen). Linked sessions render as chips on the card, clickable to the note.
- Reference: upload zone gains a two-option row ("Session recording" preselected / "Reference material"); source overflow gains "Treat as reference material" / "Treat as a session recording" via reclassify; reference cards use the Reference chip and never show the evidence checkbox on the record screen.
- Reveal audit per decision 6.
- Developer panel per decision 8.
- jsdom harness extended over every new behavior; XSS discipline unchanged (every server string escaped).

## Chunk D: verify

- Full non-live suite green, no new warnings.
- Real run on the scratch vault against the live model: set a session date, view word for word, draft a note from the existing source, assign it to the drafted note, flip a source to reference and confirm the reference-shaped summary, open the developer panel.
- HANDOFF.md updated; this plan's checkboxes ticked.

## Hard rules (unchanged from Phase A)

Tests only via `.venv/bin/pytest -m "not live"`. Never write to the real DebriefVault/. No em dashes anywhere. One commit per chunk minimum, senior review after each chunk, fixes re-reviewed. Do not touch preview/home-redesign.html.
