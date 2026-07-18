# Debrief: One Spoken Debrief Runs a Therapist's Entire Post-Session Admin

**Track 2: Voice-to-Action Agents · Build with Gemma: JustBuild · July 17-18, 2026**

Repo: https://github.com/BlainThomas/case-notes

## The problem

After every session, a solo therapist loses 15-30 minutes to admin: writing the progress note, booking the follow-up, sending the client their homework. AI scribes exist, but they are cloud services, and therapists are being asked to upload their clients' trauma disclosures to someone else's servers to save that time. Many refuse, and they keep losing a third of their working day to paperwork.

## What we built

Debrief is an agent you talk to once, after each session. The therapist speaks a 60-90 second debrief in natural language ("...we did cognitive restructuring on the fraud thought... book him for next Tuesday at 3, and send him the thought record worksheet"). The agent then:

1. Writes an audit-ready DAP progress note, grounded in the transcript with verbatim client quotes, framework-authentic vocabulary (CBT, ACT, DBT, family systems, EMDR, psychodynamic), and a mandatory structured risk section whenever suicidal ideation is mentioned. The note files into an Obsidian vault that doubles as the practice's second brain (client profiles, treatment plans, session history), and opens on screen.
2. Books the follow-up in Apple Calendar.
3. Drafts the client email in Apple Mail with the worksheet attached (always a draft, never auto-sent).
4. Closes the loop with screen understanding: it screenshots Calendar, Obsidian, and Mail, and Gemma 4 vision reads the live screen to confirm each action actually happened, reporting what it saw ("red event block 3:00 PM to 4:00 PM titled 'Bob 3:00 PM session'").

Every step runs on the Mac. The demo uses fictional clients and local processing throughout; this is a prototype, not a healthcare compliance claim.

## How the model stack works

One local model, three roles. **Gemma 4 12B (QAT, via LM Studio, thinking disabled through the API) is the brain:** a single JSON-schema-constrained call extracts the clinical note AND the requested actions from the transcript. **It is also the eyes:** the same model's vision reads the screenshots for verification. **It is also the editor:** a glossary pass fixes clinical-term transcription errors (GAD, sertraline, EMDR). Speech-to-text is parakeet-mlx on Apple Silicon (a 10-minute dictation transcribes in seconds, fully offline).

The architecture principle is **deterministic hands, model brain, model eyes.** The model decides what to do and verifies that it happened, but actions execute through deterministic code (atomic file writes, AppleScript). The model never does date math: it copies the spoken time phrase ("next Tuesday at 3") and Python resolves it, with the resolved absolute datetime shown in an approval checklist before anything executes. A dedup guard makes double-booking impossible. This split is why a 12B local model is enough to run a reliable agent.

Gemma is essential, not decorative: the entire premise (clients' words never leave the machine) is only possible because the model generating the notes, planning the actions, and reading the screen runs locally, and the 12B QAT build leaves room for speech recognition on the same 16 GB-class laptop.

## Evidence and evaluation

We defined success before the demo and measured it:

| Suite | Result |
|---|---|
| Unit tests (dates, vault, schemas) | 54/54 pass |
| Date-resolution suite (10 spoken phrasings) | 10/10 |
| Eval: 5 fictional debriefs x 8 automated checks (DAP completeness, audit trio, risk-section-iff-SI, verbatim quote grounding, action extraction, resolved weekday, framework vocabulary) | 40/40 |
| Full live pipeline, synthesized voice to verified screen, end to end | Passed 2x, vision verification 3/3 surfaces both runs |
| Exact browser demo path over HTTP | Passed 2x |

The eval harness earned its keep before the demo: it caught the model duplicating a calendar booking on family-systems phrasing (reproducible 6 of 6 runs) and a crash on real client profiles. Both were fixed and re-verified; the eval now runs fully green. A safety test also came first: we confirmed Gemma 4 does not refuse transcripts containing suicidal ideation when framed as clinical documentation, since a therapist tool that refuses risk content would be useless exactly when documentation matters most.

Typical latency on an M1 Max: speech to approved plan in about 40 seconds, execution plus screen verification in another 35-75 seconds.

## Honest limitations and what is next

Actions execute via AppleScript rather than UI clicking; screen understanding lives in the verification loop. The note is a draft the clinician owns and signs; suggestions are options, never directives. Next: a native SwiftUI app, a voice correction turn ("actually make it 4"), EHR support, and a formal privacy and legal review before any real clinical use.

## Run it

```
HF_HUB_OFFLINE=1 .venv/bin/python app.py   # then open http://127.0.0.1:8377
```

Requires: Apple Silicon Mac, LM Studio serving gemma-4-12b-it-qat, parakeet-mlx, macOS Automation and Screen Recording permissions.
