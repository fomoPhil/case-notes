# Debrief: Post-Session Admin Agent for Therapists
## Implementation Plan for "Build with Gemma: JustBuild" Hackathon, Track 2 (July 17-18, 2026)

Working title: **Debrief** (fallback: CaseNotes). This document is LLM-ready: an agent given this file plus an empty repo should be able to build the project end to end.

**PIVOT NOTE (2026-07-17):** This plan supersedes the earlier Track 1 scribe-only concept. The product is now a voice-to-action agent for Track 2. The clinical documentation core (Sections 6-7, Appendices A-B) carries over unchanged from the prior plan.

---

## 1. Executive Summary

After every session, a therapist does 15-30 minutes of admin: write the progress note, book the follow-up, send the client their homework/confirmation. **Debrief collapses all of it into one spoken debrief.** Between sessions, its Clinician Voice Journal lets the therapist dictate observations and session-prep thoughts while context is fresh. The therapist talks for 60-90 seconds; the agent:

1. Generates an audit-compliant DAP progress note (local Gemma 4) and files it in the Obsidian client vault, opening it on screen.
2. Books the follow-up appointment in Apple Calendar (visible on screen).
3. Drafts the client email in Apple Mail (confirmation + attached worksheet), left open for the therapist to review and send.
4. **Closes the loop with screen understanding:** screenshots the results and has Gemma 4 (vision) read the actual screen to verify the appointment and note exist as requested, then reports what it saw.

The intended deployment keeps transcription, model inference, vault data, and screen captures on clinician-controlled hardware. The demo uses fictional clients only. This is not a HIPAA-compliance claim and a production deployment needs a formal privacy, security, retention, and legal review.

**Status of the #1 pre-build risk: RESOLVED.** Tested 2026-07-17 on `gemma-4-12b-it-qat` with thinking toggled OFF: a mock transcript containing suicidal ideation produced a high-quality SOAP note with no refusal, and the model spontaneously appended a clinician reminder to document risk level and safety plan. Structured generation on SI content is confirmed viable.

---

## 2. Track 2 Requirements and How We Hit Each One

**Track 2, "Voice-to-Action Agents":** "Build an agent you can talk to that gets things done on a real computer: it sees the screen, takes the actions, closes the loop. Any model stack is eligible; meaningful Gemma use may be viewed favorably." Ineligible: voice-only chatbots, scripted demonstrations, assistants that merely explain where to click.

| Judge checkbox | How Debrief satisfies it |
|---|---|
| A natural spoken request | One conversational debrief: note content + "book her for Tuesday at 3" + "send her the thought-record worksheet," all extracted from natural speech, no command syntax. |
| Live screen understanding | The verification pass: agent screenshots Calendar/Obsidian/Mail and Gemma 4 vision reads the live screen to confirm each action landed ("I can see Jane's appointment on Tuesday July 21 at 3:00 PM"). Plus stretch: voice correction loop ("actually make it 4") re-reads the screen state first. |
| A real computer action | Three: a calendar event created, an email drafted with attachment, a note filed and opened in Obsidian. Real apps, real artifacts. |
| Visible confirmation | Apps visibly change on screen during the demo, and the agent narrates its screen-verified confirmation. |

**Honest weak point to engineer around:** our actions execute via AppleScript/file-writes (reliable), not via clicking the UI. The screen-understanding checkbox is satisfied by the verification loop, so make that loop PROMINENT in the demo, not an afterthought. Stretch goal (timeboxed): one genuinely UI-driven action via Hermes `computer_use` for extra credit. If judges probe "is AppleScript a real action?", the answer: the event exists in the real Calendar, the email is a real draft; we are not explaining where to click, we are doing the work.

**Judging rubric (100 pts, same for both tracks):**

| Category | Pts | Our play |
|---|---|---|
| Value | 25 | 15-30 min of post-session admin → one sentence. Cloud scribes only do the note; nobody closes the whole loop, and nobody does it without uploading trauma disclosures. |
| Inputs & Data | 15 | Natural speech in; vault context (profile, treatment plan, prior sessions) feeds generation; screenshots feed verification. |
| Enablement & Ease of Use | 20 | Therapist workflow is literally "talk, then review." Notes land in Obsidian which they can use standalone. |
| Underlying Model | 20 | Gemma 4 is essential three ways: note generation must be local (privacy), intent extraction runs on it, and its VISION reads the screen for verification. One local model, three roles. |
| Evidence & Evaluation | 20 | Eval harness (Section 10): note quality checks + intent-extraction accuracy + action success rate + latency. Most teams skip this. |

**Event logistics:** In-person at Pattern, Lehi UT. Build: Fri 5:30-10 PM + overnight + Sat 8 AM-3 PM. Team registration Sat 10 AM. Kaggle writeup due Sat 3 PM (must link public repo). 3-min live demo Sat 3-4:30 PM. **Repo must be created AFTER Friday 5:30 PM kickoff; no prior code.** Model downloads and this plan are fine to prepare beforehand.

---

## 3. Product Scope (24-hour MVP)

**Persona:** Solo private-practice therapist, Mac user.

**Core loop (must ship):**
1. Pick client (or create new) → choose Post-session Debrief or Clinician Voice Journal → tap Record → speak → Stop.
2. On-device STT → transcript shown briefly.
3. Gemma 4 extracts: (a) note content, (b) requested actions with parameters (follow-up datetime, email intent + attachment reference).
4. Action plan shown as checklist ("File DAP note · Book Tue Jul 21 3:00 PM · Draft email with thought-record worksheet") → therapist taps Approve (one confirmation gate, then the agent runs unattended).
5. Agent executes: writes note to vault + opens it in Obsidian; creates Calendar event; creates Mail draft with attachment.
6. Verification pass: screenshot each surface → Gemma 4 vision confirms → agent reports results with what it actually saw.
7. Next-session suggestions panel (from prior plan, unchanged): options only, therapist decides.

**Clinician Voice Journal mode:** writes a dated, review-required observation or session-prep draft into `Clients/<id>/Voice-Journal/`. It never schedules, drafts email, signs documentation, or sends anything. This is the lowest-risk first demonstration of the voice-to-action loop outside a live session.

**Should ship:** voice correction turn ("change it to 4pm"): agent re-reads current screen/calendar state, amends the event, re-verifies.

**Stretch (timeboxed 2h):** one Hermes `computer_use` UI-driven action, e.g. visibly clicking "Send" is NOT it (never auto-send client email); instead e.g. dragging the session note into an Obsidian Bases dashboard view or navigating Obsidian UI.

**OUT of scope:** auto-sending email (always leave as draft: safety + liability), full-session two-speaker transcription, TTS, fine-tuning, iPhone app, billing, EHR integration (post-hackathon: SimplePractice browser driving).

---

## 4. Architecture

```
┌───────────────────────────── Mac (everything local) ─────────────────────────────┐
│  Web UI (FastAPI + single page): record • action-plan approval • results          │
│        │ audio                                                                    │
│        ▼                                                                          │
│  Orchestrator (Python)                                                            │
│   1. STT: parakeet-mlx  (fallback: mlx-whisper large-v3-turbo + clinical prompt)  │
│   2. Glossary correction pass ──► Gemma 4                                         │
│   3. Intent + note extraction ──► Gemma 4 (JSON-schema constrained, thinking OFF) │
│   4. ACTIONS (deterministic executors):                                           │
│        • vault writer: atomic write → Clients/<id>/Sessions/YYYY-MM-DD.md         │
│          then `open obsidian://open?...` to show it                               │
│        • calendar: osascript / EventKit → create event                            │
│        • mail: osascript → draft w/ attachment, window left open                  │
│   5. VERIFY (screen understanding):                                               │
│        `screencapture` → Gemma 4 VISION: "read this screen; is there an event     │
│        for <client alias> Tue 3pm?" → structured confirm/deny → report            │
│        │                                                                          │
│        ▼                                                                          │
│  Obsidian Vault (Clients/, Interventions/, Themes/, Dashboard.base)               │
│  Ollama serving gemma-4-12b QAT @ http://localhost:11434/v1  (thinking off)       │
│                                                                                   │
│  [Stretch] Hermes Agent + computer_use / Obsidian MCP for one UI-driven action    │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**Design principle: deterministic hands, model brain, model eyes.** The LLM decides WHAT to do (intent extraction) and verifies THAT it happened (vision), but actions execute through reliable deterministic code (AppleScript/file ops), not model-driven clicking. This is why the demo won't die on stage, and it's a defensible architecture statement for the writeup: reliability is what makes agents usable by non-technical professionals.

**Why Python + web UI:** fastest to demo (parakeet-mlx is Python/MLX; Ollama is HTTP; osascript is a subprocess call). Post-hackathon path: native SwiftUI + EventKit + WhisperKit.

---

## 5. Voice Pipeline

Unchanged from prior research:
- **Primary: parakeet-mlx** (`pip install parakeet-mlx`), ~2GB RAM, minutes-long dictation transcribed in seconds on M-series, 6.32% WER.
- **Fallback: mlx-whisper / whisper.cpp large-v3-turbo** with `initial_prompt` clinical glossary (GAD, MDD, SSRI names, EMDR, SUDs...) if parakeet fumbles clinical terms. Biasing can hallucinate primed words: transcript is always shown before actions run.
- Batch, not streaming. Single speaker. No diarization.
- Glossary correction pass through Gemma stays (cheap, catches drug/diagnosis names).
- **New requirement: datetime robustness.** "Next Tuesday at 3" must survive STT and extraction. Eval includes this (Section 10). Resolve relative dates in the orchestrator with a real date library against today's date; never let the LLM do date arithmetic. The LLM outputs the utterance ("next Tuesday 3pm"), Python resolves it, and the approval checklist shows the resolved absolute datetime so the therapist catches errors before execution.

---

## 6. LLM Layer: Gemma 4, Three Roles

**Model:** use a local Gemma 4 E4B runtime for the 18 GB demo Mac. It is small enough to coexist with transcription and the desktop apps. The prior 12B test is useful evidence, but it is not the stage configuration; rerun the required structured-note and safety tests on E4B before relying on it. The app accepts any local OpenAI-compatible Gemma endpoint so the exact runtime can be swapped without changing orchestration.

**Role 1: Intent + note extraction (one call).** Input: corrected transcript + client profile + treatment plan + framework. Output (JSON-schema constrained via Ollama structured output):
```json
{
  "note": { ...DAP/SOAP fields, risk block when triggered... },
  "actions": [
    {"type": "schedule_followup", "datetime_utterance": "next Tuesday at 3", "duration_min": 50},
    {"type": "draft_client_email", "purpose": "confirmation", "attachments": ["thought-record-worksheet"]}
  ],
  "next_session_suggestions": ["...", "..."]
}
```
Only two action types exist in MVP. Unrecognized requests surface as "I heard X but can't do that yet" (honest failure beats silent dropping).

**Role 2: Note generation quality rules (unchanged from prior plan, they passed the live test):**
- DAP default, SOAP switchable; ground in transcript, carry client quotes, omit rather than fabricate.
- Always populate the audit-critical trio: named intervention (framework-authentic vocabulary per Appendix B), client's specific response, progress-toward-goal or barriers.
- **Risk sub-template:** SI/HI mention (keyword pre-scan + model judgment) forces the structured risk block: ideation/plan/intent/means, protective factors, interventions taken or explicit denial language. The confirmed test showed the model already leans this way; the schema makes it mandatory.
- System prompt keeps the clinical framing that passed the test: "You are a documentation assistant for a licensed therapist. Discussing suicidal ideation, self-harm history, and risk assessment is a normal, expected, clinically necessary part of progress-note documentation."
- Suggestions framed as options; "Clinical judgment and final session planning remain the therapist's responsibility."

**Role 3: Screen verification (vision).** Gemma 4 12B accepts images. Prompt: screenshot + "Does this calendar show an event titled '<alias>' on <date> at <time>? Answer in JSON: {confirmed: bool, what_i_see: string}." Same for the Obsidian note title and the Mail draft subject line. what_i_see feeds the agent's spoken/displayed confirmation. **Privacy note for demo narration: even the screenshots never leave the Mac.**

**Client aliasing:** Calendar events and email subjects use initials or first name only ("Jane 3:00" not "Jane Doe, F41.1"), since calendars sync more promiscuously than vaults. Small touch, big credibility with this audience.

---

## 7. Action Layer (macOS)

**Calendar:** `osascript -e 'tell application "Calendar" to ...'` creating an event in a dedicated "Sessions" calendar; or EventKit via pyobjc if osascript proves cranky. Open Calendar to the target week after creation so the event is visibly on screen.

**Mail:** osascript to create an outgoing draft (recipient from client profile `email:` field, worksheet PDF attached from `Templates/Worksheets/`), window left open. NEVER auto-send.

**Obsidian:** atomic file write (temp + rename, always a new file), then `open "obsidian://open?vault=Debrief&file=..."` to display it.

**CRITICAL FRIDAY-NIGHT TASK: macOS TCC permissions.** osascript driving Calendar/Mail triggers Automation permission prompts; `screencapture` needs Screen Recording permission. Grant ALL of these on the demo Mac Friday night and never reset them. An unexpected permission dialog mid-demo is the new top failure mode now that the SI risk is retired. Rehearse on the exact Mac + same terminal/app context that will run on stage.

---

## 8. Obsidian Vault

Unchanged from prior plan (see Appendix A for schemas): `Clients/<id>/{_Profile.md, Treatment-Plan.md, Sessions/}`, `Interventions/`, `Themes/`, Bases dashboard. Native property types; wikilinks + nested tags. New session notes always new files. Profile `email:` field added for the mail action. Private-notes separation (45 CFR 164.501) stays: `Private/` folder, never LLM-touched, one line of UI copy about it. Setup guardrails: no iCloud folder, Sync off, FileVault on.

## 9. Hermes (Stretch Only, 2h Timebox)

Hermes (NousResearch/hermes-agent) is no longer load-bearing. If core + eval are done by Sat 1 PM: point Hermes at local Ollama, add Obsidian MCP (Local REST API plugin at `https://127.0.0.1:27124/mcp/`), demo one `computer_use` UI-driven flourish or the "prep me for my 3pm" chat. If anything fights back, cut it without regret; every judge checkbox is already covered without Hermes.

## 10. Evidence & Evaluation

`eval/` with two suites:
1. **Note quality (5 mock debriefs, written Friday):** one with SI content (reuse the confirmed Bob transcript), one medication-heavy, one ACT, one family-systems, one rambling. Checks: required fields present, risk block when triggered, quotes grounded in transcript (fuzzy substring), framework vocabulary present.
2. **Agent accuracy (10 spoken action phrasings):** "next Tuesday at 3," "same time next week," "two weeks from Friday morning," etc. → correct resolved datetime and action list. Plus action success rate and screen-verification accuracy (does vision correctly confirm AND correctly reject a deliberately wrong screenshot? Include one negative case: verification that catches a planted wrong-time event proves the loop is real, not theater).
3. **Latency:** end-of-speech → all actions verified, per run, on the demo Mac.

Results table in README + one demo slide.

## 11. Build Timeline

**Before kickoff (today, allowed: not code):** pull models at home (`ollama pull` Gemma 4 12B QAT + e4b; pip wheels for parakeet-mlx). SI test: DONE ✓.

**Friday 5:30-10 PM:** repo creation + team registration. TCC permissions on demo Mac. Spike the spine as CLI: wav → parakeet → correction → intent+note JSON → vault write + osascript Calendar event + Mail draft → screencapture → vision verify. Write the 5 mock debriefs. The spine end-to-end (however ugly) by 10 PM is the milestone.

**Overnight:** FastAPI + web UI (record, approval checklist, results panel). Vault scaffolding + Bases dashboard. Datetime resolver + tests.

**Saturday 8-10 AM:** end-to-end polish, correction turn ("make it 4pm"), confirm registration by 10 AM.
**10 AM-12:30 PM:** eval harness, fix what it exposes, negative verification case.
**12:30-1:30 PM:** Hermes stretch OR skip straight to polish.
**1:30-3 PM:** Kaggle writeup (problem → architecture "deterministic hands, model brain, model eyes" → Gemma's three roles → eval table → repo link → limitations/roadmap), README, 3x demo rehearsal. Submit before 3:00.

**Demo script (3 min):**
1. (20s) "After every session, therapists lose up to 30 minutes to admin: the note, the booking, the follow-up email. And the AI scribes that help all require uploading trauma disclosures to the cloud."
2. (15s) **Wi-Fi off on stage.** "Everything runs on this laptop, including the model that's about to read my screen."
3. (90s) Live: pick mock client Bob → speak a rehearsed 45s debrief (CBT content + "book him next Tuesday at 3 and send him the thought-record worksheet") → approval checklist appears with resolved date → Approve → note opens in Obsidian, Calendar flips to next week showing the event, Mail draft pops up → agent reports: "Verified on screen: DAP note filed, appointment Tuesday 3:00, draft ready for your review."
4. (20s) Correction turn: "Actually, make it 4." Event amends, re-verifies.
5. (15s) Eval slide: extraction accuracy, verification catches planted errors, latency. "One local Gemma 4: writes the note, plans the actions, and reads the screen to prove they happened. PHI never left this Mac."

Fallback assets ready: pre-recorded debrief wav (if mic/room noise fails), pre-warmed Ollama (first call after idle is slow: send a warmup request before walking on stage).

## 12. Risks

| Risk | Status/Likelihood | Mitigation |
|---|---|---|
| Gemma 4 refuses SI content | **RETIRED: tested, passed** (12B QAT, thinking off) | Keep exact model+settings; re-test if model changes. |
| macOS permission dialog mid-demo | NEW #1, medium | Grant all TCC perms Friday; rehearse on exact Mac/terminal; never reset. |
| Datetime extraction error books wrong slot | Medium | Python resolves dates, not the LLM; approval checklist shows absolute datetime; eval suite 2. |
| Judges question AppleScript as "real action" | Low-medium | Lead with the screen-verification loop; artifacts are real; stretch UI-driven action if time. |
| parakeet-mlx rough edges | Medium | mlx-whisper fallback with clinical prompt. |
| RAM contention (12B + STT + browser) | Medium | e4b fallback (re-run SI test on it); close apps; test Friday. |
| Ollama cold start on stage | Medium | Warmup call before demo slot. |
| Hermes time sink | Medium | Stretch-only, 2h box, zero core dependency. |

**Claims discipline (writeup + demo):** describe the fictional-data demo and local processing plainly. Never claim "HIPAA compliant," "no BAA needed," or that local deployment alone satisfies legal obligations. Email always remains a draft. Suggestions are options; the signed note and the sent email are the clinician's decisions.

---

## Appendix A: Frontmatter Schemas

```yaml
# _Profile.md
---
type: client-profile
client_id: C-0001
name: Bob Smith           # demo uses fictional clients only
email: bob@example.com    # for mail-draft action; demo uses a dummy address
status: active
intake_date: 2026-01-15
last_session: 2026-07-17
next_session: 2026-07-21T15:00:00
diagnosis: ["F41.1"]
presenting_concerns: ["workplace stress", "worthlessness"]
framework: CBT
themes: ["Work-Undermining"]
risk_flags: ["SI-passive-2026-07-17"]
summary_updated: 2026-07-17T18:30:00
---
```

```yaml
# Sessions/2026-07-17-session.md
---
type: session-note
client_id: C-0001
session_date: 2026-07-17
session_number: 14
format: DAP
modality: in-person
duration_min: 50
framework: CBT
interventions: ["cognitive-restructuring"]
themes: ["Work-Undermining"]
risk_assessment: present-see-note   # none-discussed | denied | present-see-note
actions_taken: ["note-filed", "followup-booked-2026-07-21T15:00", "email-drafted"]
tags: [client/C-0001, type/session]
---
## Data
## Assessment
## Plan
## Risk            (mandatory when triggered)
## Next Session Considerations   (suggestions; therapist decides)
```

Treatment-Plan.md schema unchanged from prior plan (goals with objectives, status, review_date).

## Appendix B: Framework Vocabulary Injection (condensed; full table in prompts/)

CBT: cognitive restructuring, automatic thoughts, cognitive distortions, thought records, behavioral activation, graded exposure, Socratic questioning. ACT: cognitive defusion, willingness, values clarification, committed action, self-as-context. DBT: diary card, chain analysis, target behaviors, four skills modules, validation. Family systems: (structural) subsystems, boundaries, enmeshment, enactment; (Bowenian) differentiation, triangulation, genogram. EMDR: target memory, NC/PC, SUDs 0-10, VOC 1-7, bilateral stimulation, body scan. Psychodynamic: transference/countertransference, defenses, interpretation, insight, working through.

Audit-critical trio in every note: named intervention + client's specific response + progress-toward-goal or barriers.

## Appendix C: Key Commands & Snippets

```bash
ollama pull gemma4:12b-qat     # match the exact model that passed the SI test
pip install parakeet-mlx fastapi uvicorn sounddevice python-dateutil

# Calendar event (shape; refine Friday):
osascript -e 'tell application "Calendar" to tell calendar "Sessions" to make new event with properties {summary:"Bob 3:00", start date:date "Tuesday, July 21, 2026 3:00:00 PM", duration:3000}'

# Mail draft with attachment (shape):
osascript -e 'tell application "Mail"
  set d to make new outgoing message with properties {subject:"Your next appointment", content:"...", visible:true}
  tell d to make new to recipient with properties {address:"bob@example.com"}
  tell d to make new attachment with properties {file name:POSIX file "/path/worksheet.pdf"}
end tell'

# Screen verify:
screencapture -x /tmp/verify.png   # then POST to Ollama chat with image + JSON schema
open "obsidian://open?vault=Debrief&file=Clients%2FC-0001%2FSessions%2F2026-07-17-session"
```

## Appendix D: Post-Hackathon Path (for writeup)

Native SwiftUI app with EventKit/MailKit-proper integrations; WhisperKit STT; EHR browser-driving (SimplePractice) as the "real computer action" for practices that don't use Obsidian; informed-consent template; per-state retention settings; pricing that undercuts $19-119/mo cloud scribes with "your clients' words never leave your Mac."
