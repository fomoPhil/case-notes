# HANDOFF: Debrief

Session-to-session state of the project. Read this first. Update it whenever meaningful state changes.

## Overnight design and hardening pass (2026-07-25)

Three parallel audits (empty and edge states, copy and emotional design, accessibility) followed by roughly forty commits of fixes. Start at `SHIP_BLOCKERS.md`, which lists what only Phil can do and every decision made on his behalf.

Behaviour changes worth knowing about:

- **Failures were silent.** `go()` cleared `App.error` one line after every caller set it, so a failed debrief, failed assistant request, rejected upload, and denied microphone all failed with no message, and the recording was discarded. Errors now survive navigation, the audio is kept and offered back, and the app warns before recording when the model is unreachable.
- **Escape during a rename committed the rename** instead of abandoning it (Escape triggered a re-render, which blurred the input, which saved).
- **Filed notes no longer carry app-injected liability boilerplate.** A clinical record should contain only what the clinician wrote.
- **Clients can be created in the app** (`POST /api/clients`). Before this, the only way was hand-authoring `_Profile.md` in Finder.
- **Accessibility**: document and library cards are real buttons (their actions were mouse-only and absent from the accessibility tree), all four sheets are proper dialogs with focus traps and Escape, every text colour clears AA, and live regions announce async changes.
- **The live pipeline test is vault-independent** and passes twice through on a scratch vault. It no longer asserts the vision pass *confirmed* what it saw, since that depends on which window is frontmost.

Test counts: 413 non-live. Live extract, agent, and full pipeline all pass; `eval/run_eval.py` OVERALL PASS.

## Where things stand (2026-07-24)

The hackathon is over (2nd place, Build with Gemma: JustBuild, Track 2). That build is frozen at the `v0.1-hackathon` tag with a GitHub Release. Everything since is the public open source phase, and it is complete: two full plans shipped (open source release, then launch readiness), plus a design polish pass.

- **HEAD:** `87890ba`. Branch `main`, pushed.
- **Tests:** 413 non-live passing, 4 deselected (`.venv/bin/pytest -m "not live"`). Live suite passes except one known-stale test (see open items).
- **Launch command:** `uv run debrief`, then open http://127.0.0.1:8377. First run downloads Parakeet (a few hundred MB) and opens the setup wizard.
- **Diagnostics:** `uv run debrief-doctor` prints the same environment checks the wizard shows.

## What exists now

| Area | State |
|---|---|
| Core loop | Voice debrief to filed note, calendar booking, Mail draft, on-screen vision verification |
| In-app agent | `debrief/agent.py` plus `/api/assistant/*`: local tool-calling loop, stages proposals for approval. The external Hermes harness is retired. |
| Records UI | Client records, session timeline, documents, library, trash with 30-day retention, global search, PDF export, Reveal in Finder |
| Editable review | Every note section is click-to-edit before filing; edits are what gets written |
| Note formats | DAP, SOAP, GROW, meeting memo, plus custom formats derived from an imported sample document |
| Onboarding | Six-card first-run wizard: model check, vault, profession, note format, feature toggles, Mac permissions |
| Settings | `_Settings` store in the vault: profession, active format, STT engine, personal dictionary, feature toggles |
| STT | Parakeet default; mlx-whisper (large-v3-turbo) selectable. Cache-aware offline handling. |
| Template import | Local Gemma compiler by default; optional one-time consented Gemini boost with the user's own key, never stored or logged |
| Frontend | `static/index.html` plus `static/app.js` plus `static/style.css`. Vanilla JS, no build step. |
| Package | `debrief/` has 22 modules; entry points `debrief` and `debrief-doctor` |

## Open items

1. **Phil has not hands-on tested** the records UI, settings, or wizard on his real vault yet. This is the highest-value next step before the launch video.
2. **Activity log junk:** `DebriefVault/_Activity/2026-07-24.md` has 52 entries, 50 of which are test pollution from the (now fixed) audit path bug. Worth cleaning if the Activity log appears in the video.
3. **Repo rename undecided:** still `fomoPhil/case-notes` while the product is Debrief. GitHub redirects old URLs if renamed.
4. **`test_pipeline_live_runs_twice`** is coupled to the real vault's seeded C-0001 (expects an email on file), so it fails against a fresh scratch vault. The pipeline itself is verified working. Fix: have the test seed its own client fixture.
5. **PDF export is an optional extra** (`uv sync --extra pdf`, plus `brew install pango`). Absent, it is a soft doctor warning, not a failure.
6. **Ollama is detect-only.** The agent runs against LM Studio; Ollama tool-call support was deliberately deferred.
7. **Packaged Mac app** (signed DMG, embedded model, no terminal) is the next big build if pursued. Business thinking lives in the gitignored `docs/business/`.

## Architecture in one line

Voice to parakeet or whisper STT, to Gemma 4 (glossary correction, then intent and note as constrained JSON, thinking off), to an approval checklist, to deterministic actions (atomic vault write, `osascript` Calendar event, Mail draft, never auto-send), to `screencapture`, to Gemma 4 vision verifying the on-screen result. Principle: **deterministic hands, model brain, model eyes.**

## Key decisions (do not relitigate without new evidence)

1. Gemma cannot ingest dictation audio directly (audio-capable variants cap at 30 seconds). The two-stage pipeline is mandatory.
2. Dates are resolved by Python, never by the model. The approval checklist shows the absolute datetime before anything runs.
3. Email is always a draft, never auto-sent. Calendar entries use client first name or initials only.
4. Risk block is schema-mandatory for clinical formats when SI or HI appears. The audit trio (named intervention, client response, progress toward goal) is required in every note.
5. Filed notes are never rewritten. Edits append a dated amendment.
6. The private notes folder is never LLM-touched (45 CFR 164.501 psychotherapy-notes separation).
7. Claims language: fictional demo data and local processing only. Never claim "HIPAA compliant" and never claim a BAA is unnecessary. Suggestions are options; the clinician decides.
8. Obsidian is optional. Deep links fire only when Obsidian has this vault registered, so unregistered vaults never trigger error dialogs.
9. The vault is plain markdown, scaffolded on first run, and gitignored. It is not committed to the public repo.
10. Model settings that must not drift: `gemma-4-12b-it-qat` at 64k context, `reasoning_effort` none. If the model changes, re-run the suicidal-ideation refusal test first (it passed on this exact model and is the project's top historical risk).

## Context for the assistant

- Phil is a non-coder (vibecoding). Explain in plain terms, no walls of text, and **never use em dashes** in anything written for this project (UI copy, README, docs, chat).
- Project memory lives in the Claude project memory dir: `casenotes-hackathon-project.md`, `debrief-open-source-release.md`, `debrief-monetization-ideas.md`.
- Build process that worked: Opus subagents implement each phase, a reviewer subagent audits the diff afterward. The review checkpoints caught real defects every time (XSS, cache poisoning, a trash-restore path that could destroy a filed note).
- Verify with tests plus a real run, and never write to `DebriefVault/` during test or verification work; point `DEBRIEF_VAULT_DIR` at a scratch dir instead.
