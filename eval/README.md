# Debrief Evaluation

This harness proves the Debrief pipeline does what it claims: turn one spoken
therapist debrief into an audit-compliant DAP progress note plus the correct
admin actions, grounded in what was actually said, with zero fabrication and
zero calendar-math errors. It runs the real local Gemma model, not a mock.

## Run it

```
.venv/bin/python eval/run_eval.py
```

Outputs `eval/results.md` (human summary table) and `eval/results.json` (raw).
Exit code is `0` only if every check passes.

## What is tested

### Suite 1: Note quality (5 mock debriefs, live model)

Five fictional dictations, each exercising a different clinical shape:

1. `01_bob_cbt_si` - CBT workplace stress with passive suicidal ideation
   (denied plan/intent), a Tuesday 3pm booking, and a thought-record email.
2. `02_marcus_meds` - medication-heavy (sertraline, escitalopram, GAD, MDD)
   with a booking and no email.
3. `03_jane_act` - ACT session (defusion, values) with an email and no booking.
4. `04_rosa_family` - family-systems session ending "same time next week".
5. `05_tom_rambling` - disorganized dictation with one booking buried mid-ramble.

Each note is scored on eight automated checks:

| Check | Pass condition |
|---|---|
| DAP | `data`, `assessment`, `plan` all nonempty |
| Trio | audit trio present: a named intervention, a client-response phrase, and progress-or-barrier language (keyword heuristics below) |
| Risk | risk block present **iff** the transcript contains risk content; populated `ideation` + `plan_intent_means` when present, `null` when not |
| Ground | every string in `client_quotes` appears verbatim in the source transcript (case-insensitive, whitespace-normalized, surrounding quote/terminal punctuation stripped) |
| NoEmDash | no em dash character anywhere in the note, actions, suggestions, or unsupported requests |
| Actions | emitted actions match the expected count and multiset of types |
| Weekday | the resolved follow-up datetime falls on the expected weekday |
| Vocab | at least one framework-authentic term for the active framework appears in the note text |

### Suite 2: Date resolution (deterministic)

Ten spoken time phrases ("next Tuesday at 3", "same time next week", "two weeks
from Friday morning", ...) are resolved by `debrief.dates.resolve_utterance`
against a fixed `now` of **2026-07-18T10:00 (a Saturday)** and compared to
hand-derived expected datetimes. This suite is pure Python and fully
deterministic: the model never does date arithmetic.

## Design decisions and honest caveats

- **Fixed clock.** All resolution is against one fixed `now` so expected
  datetimes are stable and independently derived, not read back from the code
  under test.
- **Grounding normalization.** Quotes are checked verbatim, but *surrounding*
  quotation marks and a trailing period/comma the model sometimes adds are
  stripped before matching. Interior words must still match exactly, so this
  catches fabrication while ignoring cosmetic edge punctuation. Rationale is
  documented inline in `run_eval.py`.
- **Audit-trio keyword heuristics.** The trio is graded on whether the required
  clinical *content* exists, using broad word lists (e.g. response verbs like
  "reported", "denied", "was able to"; progress words like "toward", "barrier",
  "goal"). The lists are intentionally generous; the exact sets live in
  `run_eval.py` (`INTERVENTION_WORDS`, `CLIENT_RESPONSE_WORDS`,
  `PROGRESS_BARRIER_WORDS`). No heuristic was loosened to hide a genuine model
  miss; every widening would be inspected by hand first.
- **Context sanitization.** `vault.client_context()` returns YAML date objects
  that the current `extract._format_context` cannot serialize. The harness
  coerces those to ISO strings before calling `extract()` so the eval measures
  the model, not that seam bug. The seam bug is reported separately.
- **The eval catches real model defects.** A failing overall result is a
  feature, not a harness fault. When the model output is genuinely wrong (for
  example duplicating a booking), the harness fails and records it under
  "Model findings" in `results.md`; prompts are owned elsewhere and are not
  touched to make the board green.

## Latency

Per-transcript `extract()` wall time is recorded. The local model runs
single-call per note. LM Studio may serve multiple agents concurrently, so
calls can queue and individual latencies vary run to run.
