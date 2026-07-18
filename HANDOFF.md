# HANDOFF: Debrief (Gemma Hackathon, Track 2)

Read this first, then IMPLEMENTATION_PLAN.md. This file is the session-to-session state of the project. Update it whenever meaningful state changes.

## What this project is (3 sentences)

**Debrief** is a clinician-controlled documentation agent for solo therapists, built for the "Build with Gemma: JustBuild" hackathon (July 17-18, 2026, in-person at Pattern, Lehi UT), **Track 2: Voice-to-Action Agents**. The therapist can speak one post-session debrief or use a between-session Voice Journal; the agent creates a review-required draft, can book a follow-up and draft an email only for an approved debrief, then uses Gemma 4 vision to verify the visible result. The demo uses fictional data and local processing; it makes no healthcare-compliance claim.

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
| Code | Runnable FastAPI MVP: Voice Journal and Debrief planning, atomic local draft writes, guarded macOS Calendar/Mail adapters, and local-model screen-verification adapter. Tests pass on Saturday; macOS live validation remains. |
| Models downloaded | Verify: Gemma 4 12B QAT confirmed present (it was tested); check parakeet-mlx installed |

## Architecture in one line

Voice → parakeet-mlx STT → Gemma 4 (glossary fix, then intent + DAP note as constrained JSON, thinking OFF) → approval checklist → deterministic actions (atomic vault write + `osascript` Calendar event + Mail draft, never auto-send) → `screencapture` → Gemma 4 VISION verifies on-screen results → agent reports what it saw. Principle: **deterministic hands, model brain, model eyes.**

## Key decisions already made (do not relitigate without new evidence)

1. Gemma CANNOT ingest dictation audio directly: all audio-capable variants cap at 30 seconds. Two-stage pipeline is mandatory.
2. Python + FastAPI + single-page web UI for the hackathon (fastest); SwiftUI is post-hackathon.
3. Dates resolved by Python (dateutil), never by the LLM; approval checklist shows absolute datetime before any action runs.
4. Email is ALWAYS a draft, never auto-sent. Calendar entries use client first name/initials only. Voice Journal never schedules or drafts email.
5. DAP note default; risk block is schema-mandatory when SI/HI appears; the audit trio (named intervention, client response, progress-toward-goal) required in every note; framework vocabulary injected per modality (Appendix B of plan).
6. Private notes folder is never LLM-touched (45 CFR 164.501 psychotherapy-notes separation).
7. Hermes (NousResearch/hermes-agent) is a 2-hour STRETCH only; zero core dependency.
8. Claims language: fictional demo and local processing only. Never claim "HIPAA compliant" or that a BAA is unnecessary. Suggestions are options; clinician decides.
9. Vault: no iCloud folder, Sync off, new session notes are always new files.

## Next actions (in order)

1. Provision the demo Mac with the local Gemma 4 E4B runtime, STT runtime, Obsidian, and permissions for Calendar, Mail, and Screen Recording.
2. Copy or clone the runnable app to the Mac and validate the spine: spoken request → Gemma JSON → local draft → Calendar/Mail draft → screenshot verification.
3. Write five fictional debriefs and five Voice Journal prompts, including a negative verification case.
4. Run the eval harness, record latency and failures, polish the screen flow, write the Kaggle submission, and rehearse three times.

## Context for the assistant

- Phil is a non-coder (vibecoding); explain in plain terms, no walls of text, and NEVER use em dashes in anything written for this project (UI copy, README, writeup, chat).
- Demo Mac: Apple Silicon; confirm RAM before choosing 12B vs e4b fallback (if switching models, re-run the SI test first).
- Memory file exists at the Claude project memory dir: `casenotes-hackathon-project.md` (update it if decisions change).
- Demo theater moment: turn Wi-Fi off on stage before the live run.
