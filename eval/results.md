# Debrief Evaluation Results

Generated: 2026-07-18T04:10:30  
Fixed now: 2026-07-18T10:00:00 (Saturday)  
Model: local Gemma via LM Studio  
**Overall: PASS**

## Suite 1: Note quality (live model)

| Transcript | Framework | DAP | Trio | Risk | Ground | NoEmDash | Actions | Weekday | Vocab | Latency (s) | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 01_bob_cbt_si | CBT | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 190.36 | PASS |
| 02_marcus_meds | CBT | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 102.66 | PASS |
| 03_jane_act | ACT | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 124.87 | PASS |
| 04_rosa_family | FAMILY SYSTEMS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 88.16 | PASS |
| 05_tom_rambling | CBT | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 73.65 | PASS |

All note-quality checks passed.

### Latency

Per-transcript extract() wall time. min 73.65s, mean 115.94s, max 190.36s (n=5). Note: LM Studio may be serving other agents concurrently, so calls can queue and inflate latency.

## Suite 2: Date resolution (deterministic)

Resolved against fixed now 2026-07-18T10:00:00. 10/10 passed.

| Utterance | Expected | Got | Pass |
|---|---|---|---|
| next Tuesday at 3 | 2026-07-21T15:00:00 | 2026-07-21T15:00:00 | PASS |
| same time next week | 2026-07-25T10:00:00 | 2026-07-25T10:00:00 | PASS |
| two weeks from Friday morning | 2026-08-07T09:00:00 | 2026-08-07T09:00:00 | PASS |
| tomorrow morning | 2026-07-19T09:00:00 | 2026-07-19T09:00:00 | PASS |
| next Thursday at 10am | 2026-07-23T10:00:00 | 2026-07-23T10:00:00 | PASS |
| in three days | 2026-07-21T15:00:00 | 2026-07-21T15:00:00 | PASS |
| same time next Wednesday | 2026-07-22T10:00:00 | 2026-07-22T10:00:00 | PASS |
| Friday at 2 | 2026-07-24T14:00:00 | 2026-07-24T14:00:00 | PASS |
| next week | 2026-07-25T15:00:00 | 2026-07-25T15:00:00 | PASS |
| at 4:30 on Monday | 2026-07-20T16:30:00 | 2026-07-20T16:30:00 | PASS |

## Check definitions

See eval/README.md for the full methodology and the exact keyword heuristics behind the audit-trio and grounding checks.
