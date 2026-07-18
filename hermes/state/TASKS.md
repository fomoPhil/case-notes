# Debrief pending tasks

One task per line. Format: `- [ ] <skill>: <instruction>`.
The runner (hermes/run_tasks.py) takes the first unchecked task, runs it in a
fresh Hermes session, appends the result to RESULTS.md, and checks it off.

- [ ] session-prep: Prep me for my next session with Bob Smith
- [ ] caseload-risk-review: Review my whole caseload and tell me which clients currently carry a risk flag
