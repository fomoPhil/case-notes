# Debrief pending tasks

One task per line. Format: `- [ ] <skill>: <instruction>`.
The runner (hermes/run_tasks.py) takes the first unchecked task, runs it in a
fresh Hermes session, appends the result to RESULTS.md, and checks it off.

## Build initial working memory (_Agent/)

Each brief task builds one client cheat sheet; the final task builds the
practice-wide snapshot. The agent reads the read-only client files and writes
ONLY inside _Agent/ (via the agent_memory MCP server).

- [x] maintain-snapshot: Build the working-memory brief for Bob Smith, client C-0001. Read Clients/C-0001/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0001.md.
- [x] maintain-snapshot: Build the working-memory brief for Jane Doe, client C-0002. Read Clients/C-0002/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0002.md.
- [x] maintain-snapshot: Build the working-memory brief for Deborah Ellison, client C-0003. Read Clients/C-0003/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0003.md.
- [x] maintain-snapshot: Build the working-memory brief for Priya Nair, client C-0004. Read Clients/C-0004/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0004.md.
- [x] maintain-snapshot: Build the working-memory brief for Thomas Reyes, client C-0005. Read Clients/C-0005/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0005.md.
- [x] maintain-snapshot: Build the working-memory brief for Eleanor Voss, client C-0006. Read Clients/C-0006/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0006.md.
- [x] maintain-snapshot: Build the working-memory brief for Aisha Karim, client C-0007. Read Clients/C-0007/_Profile.md, Treatment-Plan.md, and the newest note in Sessions/, then write _Agent/briefs/C-0007.md.
- [x] maintain-snapshot: Build the practice-wide snapshot. Read ONLY these files, nothing else: the seven briefs in _Agent/briefs/ (C-0001.md through C-0007.md) and each Clients/C-000X/_Profile.md (for name, next_session, and risk_flags). Do NOT read Treatment-Plan.md or any Sessions/ notes; the briefs already summarize them. Then write _Agent/Practice-Snapshot.md with the active roster (one line each), risk flags quoted verbatim, upcoming follow-ups from next_session, recent session activity, and open homework threads.

## Demo / acceptance queries (run manually, not part of the bootstrap)
# debrief chat -q "Prep me for my next session with Priya Nair, and update your notes afterward" --yolo -s session-prep
# debrief chat -q "Review my whole caseload and tell me which clients currently carry a risk flag" --yolo -s caseload-risk-review
