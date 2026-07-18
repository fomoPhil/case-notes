---
date: 2026-07-18
topic: debrief-product-features
focus: product feature improvements, ranked for quick-payoff high-value hackathon demo
mode: repo-grounded
---

# Ideation: Debrief Product Features (demo-first ranking)

## Grounding Context

**Codebase context.** Python 3.13 FastAPI + vanilla-JS SPA, local-only macOS app. Flow: pick client, record 60-90s debrief, parakeet STT, client-aware glossary correction, one JSON-schema Gemma call extracts DAP note + actions, Python resolves dates, approval screen with gap nudges, then execution (Apple Calendar event, Mail draft never auto-sent, atomic vault write, audio archive, running summary condensation) verified by Gemma vision reading screenshots. Principles: deterministic hands, model brain, model eyes; two-phase human-gated approval. Known gaps: no correction turn, no in-app client creation, hardcoded worksheet, email hard-fails without address, 74-190s extract latency, Hermes second-brain (session-prep, caseload-risk-review, week-review) is CLI-only.

**External context (2026).** Mentalyc/Upheal/Autonotes dominate cloud AI therapy notes; none are local or on-device (genuine white space). Eleos sells golden-thread compliance flagging to 35k+ providers. Abridge's "linked evidence" (click a claim, jump to its transcript moment) is the portable trust pattern; Upheal has a documented note-fabrication incident. Benchmarks: ~2h admin per 1h care; 15% of clinicians never adopt AI tools over trust. Human-in-the-loop approval is industry consensus. Claim "on-device, nothing leaves your Mac," never "HIPAA-compliant."

## Topic Axes

1. Capture and correction
2. The note and clinical quality
3. Admin actions and execution
4. Caseload intelligence over time
5. Practice operations and trust

## Ranked Ideas

### 1. Correction turn on the approval screen ("actually make it 4pm")
**Description:** A voice-or-text field on the approval screen. A constrained Gemma pass patches only the affected field (time, name, intervention, dropped action) and re-renders for approval. Date words resolve through the existing deterministic resolver.
**Axis:** Capture and correction
**Basis:** direct: "no voice/text correction turn on approval screen" is the top named gap in the repo docs; 4 of 6 ideation frames converged on it independently.
**Rationale:** Re-recording 90 seconds to fix one digit is the most enraging failure of a voice-first tool. Also the canonical demo moment: a wrong calendar time fixed by one sentence.
**Downsides:** Patch pass must be constrained so it cannot rewrite or hallucinate the rest of the note.
**Confidence:** 85%
**Complexity:** Low-Medium (~1-2 hours to working v1)
**Status:** Unexplored

### 2. Linked evidence: click a note claim, see the words that produced it
**Description:** Post-hoc fuzzy alignment maps each DAP sentence to its transcript span. Clicking a claim highlights the span; unsupported lines get a "no transcript basis" flag before approval.
**Axis:** The note and clinical quality
**Basis:** external: Abridge ships linked evidence; Upheal's documented fabrication incident shows the cost of its absence. Repo already stores transcript + corrected transcript + archived audio.
**Rationale:** The strongest possible trust answer for a local model, and no competitor does it on-device. Clicking the risk line and seeing your own words is the demo's trust beat.
**Downsides:** Fuzzy alignment misses paraphrased claims; must flag rather than fail.
**Confidence:** 70%
**Complexity:** Medium (~2-3 hours)
**Status:** Unexplored

### 3. Session-prep card at client pick (Hermes surfaced in-app)
**Description:** Picking a client shows a brief assembled from the vault: last session summary, open homework, active goals, risk flags. Deterministic assembly from existing files, no new model call.
**Axis:** Caseload intelligence over time
**Basis:** direct: Hermes session-prep skill and the running client summary already exist but are CLI-only; 5 of 6 frames converged here.
**Rationale:** Makes the compounding vault visible in the first ten seconds of any demo and pays the therapist back before the session, not just after.
**Downsides:** Must stay fast; resist the temptation to make it another slow LLM call.
**Confidence:** 85%
**Complexity:** Low-Medium (~1-2 hours if deterministic)
**Status:** Unexplored

### 4. Client-facing recap card (one debrief, two artifacts)
**Description:** Second render of the same extracted JSON as a plain-language "what we agreed / your practice this week" card, flowing into the existing never-auto-sent Mail draft, separately approved.
**Axis:** Admin actions and execution
**Basis:** external: Mentalyc ships client-facing summaries as a headline feature. direct: zero new capture; the extraction already contains homework, agreed actions, worksheet.
**Rationale:** Doubles the visible output of one dictation for near-zero build cost. Cheapest wow on the list.
**Downsides:** Client-facing tone needs a careful template; must not leak clinical content beyond what the therapist approves.
**Confidence:** 85%
**Complexity:** Low (under an hour)
**Status:** Unexplored

### 5. New client by voice
**Description:** "New client, Sam Rivera, ACT, health anxiety" scaffolds `_Profile.md` and a starter `Treatment-Plan.md` via the same dictate-extract-approve spine, human-approved before write.
**Axis:** Practice operations and trust
**Basis:** direct: onboarding currently requires hand-editing markdown, named as the biggest usability blocker; reuses the existing pipeline against a profile schema.
**Rationale:** Proves end-to-end usability by a real therapist, not just pre-seeded demo data.
**Downsides:** A second extraction schema to prompt-tune on deadline day.
**Confidence:** 65%
**Complexity:** Medium (~2 hours)
**Status:** Unexplored

### 6. Golden-thread nudge
**Description:** One amber approval-screen nudge from a deterministic check against Treatment-Plan.md and recent sessions: "Goal 2 (sleep) has not been addressed in 3 sessions."
**Axis:** The note and clinical quality
**Basis:** external: Eleos sells golden-thread compliance flagging to 35k+ providers. direct: the deterministic gap-nudge machinery and the vault structure already exist. 6 of 6 frames touched this cluster.
**Rationale:** The sellable clinical-compliance moment, and the vault's longitudinal structure is a moat stateless cloud scribes cannot match.
**Downsides:** Weakest demo legibility; needs one sentence of narration for non-clinical judges.
**Confidence:** 70%
**Complexity:** Medium (~1-2 hours)
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Staged/streaming extract progress | Optimistic staged checklist shipped 2026-07-18; full streaming is high complexity for marginal demo delta |
| 2 | Batch end-of-day debrief (auto-segmented) | Segmentation build risk on deadline day; demo payoff is slow (multi-minute dictation) |
| 3 | Overnight debrief queue | Nothing to watch in a live demo |
| 4 | Worksheet picker + library | Real but below shortlist bar under demo lens; good first post-hackathon follow-up |
| 5 | Graceful email fallback | Robustness fix, invisible in a demo; do it, but it is a chore not a feature |
| 6 | Zero-screen capture (menu bar / Watch) | Out-of-app build risk today; demo happens in-app anyway |
| 7 | Calendar-aware debrief nudge | Same as above; strong v1.1 candidate |
| 8 | Approval-only mode | Scope reduction, not a demoable feature |
| 9 | Learning glossary with correction memory | Needs two takes to show; invisible in one pass |
| 10 | Theme and intervention graph | Needs a deep vault to shine; seeded-data demo feels staged |
| 11 | Accuracy dashboard from eval harness | Meta-feature; distracts from the core flow in a 3-minute demo |
| 12 | Amendment ledger / notes-as-commits | Compliance substrate, invisible in demo; strong product roadmap item |
| 13 | The Pass (pending-actions board) | Only legible with multi-session state; single-session demo will not show it |
| 14 | Supervision packet export | Output is a document, weak live moment; strong segment play later |
| 15 | Undo-as-transaction | Very demoable but destabilizes the execution path on deadline day |
| 16 | Natural-language vault query | Live long local-model call on stage is a latency gamble |
| 17 | Pre-session spoken intention + reconciliation | Novel and differentiating, but adds a second capture ritual; brainstorm-grade, not deadline-day |
| 18 | Spoken TTS prep briefing | Delivery-channel variant of idea 3; build the card first |
| 19 | Spaced resurfacing of stalled goals | Variant of golden-thread nudge; fold in later |
| 20 | Searchable case archive by intervention/theme | Same latency and staging concerns as vault query |

## Suggested demo arc (composition of survivors)

Pick client (prep card shows where you left off) -> dictate -> staged pipeline runs -> approval screen shows the note with evidence links and a golden-thread nudge -> speak "actually make it 4pm" (correction turn) -> approve -> calendar + note + two-artifact email (DAP for you, recap card for the client) -> vision verification confirms on screen.
