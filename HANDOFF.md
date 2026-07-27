# HANDOFF: Debrief

Session-to-session state of the project. Read this first. Update it whenever meaningful state changes.

## Source trust loop (built 2026-07-27, same day as Phase A, after Phil's first hands-on pass)

Phil's first contact with Phase A surfaced five gaps; all are closed. Plan: `docs/plans/2026-07-27-source-trust-loop.md`. Tests now **786 non-live** (Phase A ended at 695).

- **Word for word in app:** every source card opens a viewer (Corrected / Raw / Original text) reading the rung files through the existing notes route.
- **Session date:** a source carries `session_date` (editable, any direction) beside the never-editable `captured_at`; cards, summaries, and drafted notes all use it. A note drafted from a July 18 session uploaded July 27 files as `2026-07-18-session.md`.
- **Connect and draft:** a source can be connected to a filed note (dated amendment through the existing amend machinery, notes never rewritten) or become a draft note via the no-debrief path (zero actions, honest body copy). Unconfirmed sources can do neither.
- **Reference material:** two new kinds (`reference_recording`, `reference_document`, both shown as "Reference material"). Reference digests are reference-shaped (title, key points, practices; no client statements, no risk, enforced in code at four independent layers with their own map and reduce prompts that never name a clinician or client). Reference sources are never session evidence (backend gate, not just UI). Flip a source either way from its card.
- **Reveal in Finder everywhere:** `/api/reveal` learned directories; source cards, both dialogs, document cards, library items, the Settings vault card, and the developer panel all reveal real folders.
- **Developer panel:** Settings, "Under the hood": model + reachability, STT engine, vault path, doctor checks, live job list with errors.
- Review checkpoints again caught real defects each chunk (reference map calls running under the session prompt; a reference draft's filed note calling an article "session material"; same-second job ids sorting randomly so a card could permanently show a cancelled job).

Open items: Phil has not yet clicked through the trust-loop UI (a scratch server may be live at http://127.0.0.1:8378). The MCP-server idea and Phase B (live recording) remain the two candidate next fronts, in that discussion order; see the assistant conversation of 2026-07-27 for the pushback on MCP scoping (read-scoped, allowlisted, audited, Private/ and raw transcripts excluded).

## Session recording and ingestion, Phase A (built overnight 2026-07-27)

The whole of Phase A from `docs/specs/2026-07-25-session-recording-and-ingestion.md` shipped: a clinician can upload or paste a session recording, video, transcript, or document; it is classified with provenance (word for word vs AI summary, with a one-tap confirmation when unsure), distilled by a background job ladder (raw transcript, corrected transcript, session digest), and the digest rides into the debrief as labelled session evidence. Plan with per-chunk detail: `docs/plans/2026-07-26-session-recording-phase-a.md`.

- **Tests: 695 non-live passing** (was 413), 4 deselected, the one pre-existing Starlette warning. Run only with `.venv/bin/pytest -m "not live"`; a bare `pytest` runs the live suite, which writes its test client into the real vault (pre-existing behaviour, see open items).
- **New modules:** `debrief/doctext.py` (doc readers carved out of compiler), `recstore.py` (Recordings/ storage, atomic rungs, retention sweep), `sources.py` (ingestion, classification firewall, consent gate), `ladder.py` (map-reduce digest with code-owned timestamps, quote grounding, attribution checks), `jobs.py` (priority worker, interactive hold, resume). Changed: `stt.py` (windowed long correction, timed transcription), `extract.py` (digest as labelled evidence), `pipeline.py` (`source_id`, rule 3 strip, no-debrief rule), `records.py` (directory trash), `settings_store.py` (`recording_retention_days`), `app.py` (source/consent/jobs routes), `static/app.js` (Recordings tab, confirm banner, evidence checkbox, review callouts, 7-card wizard).
- **The seven spec rules each have a named test** in `tests/test_rules.py` (extract never sees rungs 0/1; non-verbatim never attributed; low confidence never files as a quote; raw transcript never in a note; Private/ untouched, asserted by snapshot; consent gates ingest; a recording without a debrief yields no actions). The no-source hero flow is pinned byte-identical by golden tests and was verified against the pre-change commit empirically.
- **Verified end to end on this machine** against the live model: consent 403, upload, confirm, ladder to done, digest served, debrief with `source_id` returning `digest_used: true` and a note whose data drew on session detail absent from the spoken debrief. Scratch vault only.
- **Build process:** eight chunks, one Opus implementer each, a senior review subagent per chunk. The checkpoint caught a real defect every single chunk, including three Criticals (a mixed AI export classifiable as verbatim; model-guessed speaker attribution; a retention sweep that could delete never-transcribed audio).

Open items from this pass:

1. **Phil has not seen the UI.** Visual pass pending: Recordings tab, confirm banner, review callouts, wizard card 6 (retention; permissions is now card 7). A scratch-vault server may still be running at `http://127.0.0.1:8378`.
2. **Any already-running `debrief` server predates this code.** Restart it to load Phase A (a stale instance was found listening on 8377 during verification and was left untouched).
3. **Pre-existing live-suite hygiene:** the live tests write `Clients/C-0008` (Bob Liveclient) and `_Activity` entries into the real vault; their git-checkout restore is a no-op because the vault is gitignored. Worth a scratch-vault fixture pass someday.
4. Deferred minors, all reviewer-noted and documented in code: delete-source racing a running rung write (narrow window), jobs folder never pruned, focus not restored after the confirm banner is answered, no window-level drag guard outside the upload zone, `_Trash` folder titles render as raw slugs.
5. **Phase B (live in-app long recording) is not built.** Spec section 12/14; the recorder-consolidation note at spec 12 still applies.

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

- **Branch:** `main`, pushed. See the overnight section above for the current state.
- **Tests:** 413 non-live passing, 4 deselected (`.venv/bin/pytest -m "not live"`). The live suite passes in full.
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
2. **Repo rename undecided:** still `fomoPhil/case-notes` while the product is Debrief. GitHub redirects old URLs if renamed.
3. **Phil's vault predates the new seeds.** `ensure_vault` never overwrites, so his Bob, Jane, and Maya keep their old hardcoded July 21 to 23 appointments and carry no `sample` flag. A fresh vault gets relative, always-future dates. Moving `DebriefVault/Clients/` aside gives clean demo data.
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
