# HANDOFF: Debrief (Gemma Hackathon, Track 2)

Read this first, then IMPLEMENTATION_PLAN.md. This file is the session-to-session state of the project. Update it whenever meaningful state changes.

## What this project is (3 sentences)

**Debrief** is a post-session admin agent for solo therapists, built for the "Build with Gemma: JustBuild" hackathon (July 17-18, 2026, in-person at Pattern, Lehi UT), **Track 2: Voice-to-Action Agents**. The therapist speaks one post-session debrief; the agent generates an audit-compliant DAP progress note into an Obsidian client vault, books the follow-up in Apple Calendar, drafts the client email in Mail, then screenshots the screen and uses Gemma 4 vision to verify each action actually happened. Everything runs locally on the Mac; PHI never leaves the device (the differentiator vs Upheal/Mentalyc/Blueprint, all cloud + BAA).

## Hard deadlines (Saturday July 18)

- **10:00 AM** team registration due
- **3:00 PM** Kaggle writeup due (mandatory; must link public GitHub repo)
- **3:00-4:30 PM** live 3-minute demo, in person
- Repo rule: public repo created AFTER Friday 5:30 PM kickoff only, no prior code

## Current state

| Item | Status |
|---|---|
| Research (6 parallel briefs: hackathon, Gemma 4, Hermes, voice, Obsidian, clinical) | DONE 2026-07-17 |
| Track decision | Track 2 (pivoted from Track 1 on 7/17; Track 2 requires screen understanding + real actions; our answer is deterministic actions + vision verification) |
| Implementation plan | DONE: IMPLEMENTATION_PLAN.md (LLM-ready, full architecture + timeline + demo script) |
| **SI refusal test (was the #1 risk)** | **PASSED 2026-07-17**: `gemma-4-12b-it-qat`, thinking OFF, produced a quality SOAP note from an SI-containing transcript, no refusal, spontaneously reminded clinician to document risk level. Keep this exact model + settings. |
| Code | NONE YET (repo rule; build starts after kickoff) |
| Models downloaded | Verify: Gemma 4 12B QAT confirmed present (it was tested); check parakeet-mlx installed |

## Architecture in one line

Voice → parakeet-mlx STT → Gemma 4 (glossary fix, then intent + DAP note as constrained JSON, thinking OFF) → approval checklist → deterministic actions (atomic vault write + `osascript` Calendar event + Mail draft, never auto-send) → `screencapture` → Gemma 4 VISION verifies on-screen results → agent reports what it saw. Principle: **deterministic hands, model brain, model eyes.**

## Key decisions already made (do not relitigate without new evidence)

1. Gemma CANNOT ingest dictation audio directly: all audio-capable variants cap at 30 seconds. Two-stage pipeline is mandatory.
2. Python + FastAPI + single-page web UI for the hackathon (fastest); SwiftUI is post-hackathon.
3. Dates resolved by Python (dateutil), never by the LLM; approval checklist shows absolute datetime before any action runs.
4. Email is ALWAYS a draft, never auto-sent. Calendar entries use client first name/initials only.
5. DAP note default; risk block is schema-mandatory when SI/HI appears; the audit trio (named intervention, client response, progress-toward-goal) required in every note; framework vocabulary injected per modality (Appendix B of plan).
6. Private notes folder is never LLM-touched (45 CFR 164.501 psychotherapy-notes separation).
7. Hermes (NousResearch/hermes-agent) is a 2-hour STRETCH only; zero core dependency.
8. Claims language: "no BAA needed," NEVER "HIPAA compliant." Suggestions are options; clinician decides.
9. Vault: no iCloud folder, Sync off, new session notes are always new files.

## Next actions (in order)

1. At/after kickoff: create public repo, register team, grant macOS TCC permissions on the demo Mac (Automation for Calendar+Mail, Screen Recording) — this is now the #1 demo risk.
2. Build the CLI spine end-to-end (wav → transcript → JSON → note file + calendar event + mail draft → screenshot verify). Milestone: ugly but working by Fri 10 PM.
3. Write 5 mock debriefs (reuse the Bob SI transcript as one of them).
4. Overnight: web UI + vault scaffolding + datetime resolver.
5. Saturday: eval harness (note quality + intent accuracy + a NEGATIVE verification case), polish, writeup, 3 rehearsals. Full timeline in IMPLEMENTATION_PLAN.md Section 11.

## Context for the assistant

- Phil is a non-coder (vibecoding); explain in plain terms, no walls of text, and NEVER use em dashes in anything written for this project (UI copy, README, writeup, chat).
- Demo Mac: Apple Silicon; confirm RAM before choosing 12B vs e4b fallback (if switching models, re-run the SI test first).
- Memory file exists at the Claude project memory dir: `casenotes-hackathon-project.md` (update it if decisions change).
- Demo theater moment: turn Wi-Fi off on stage before the live run.
