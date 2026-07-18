---
name: maintain-snapshot
description: "Keep the agent's working memory current: update _Agent/Practice-Snapshot.md and _Agent/briefs/C-XXXX.md after reviewing clients. Writes ONLY inside _Agent/."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, snapshot, brief, working memory, update notes, cheat sheet, debrief, vault, agent]
    related_skills: [session-prep, caseload-risk-review, week-review]
---

# Maintain Snapshot (working memory)

Use this whenever you have just learned something current about one or more
clients and should record it, specifically:
- after you review or prep sessions for a client,
- when the therapist says "update your notes", "update your memory", "keep
  track of this", or
- at the end of any prep or review task, as a final tidy-up step.

## What you maintain

Two kinds of file, and they live ONLY in `_Agent/`:
- `_Agent/Practice-Snapshot.md` : the whole practice at a glance.
- `_Agent/briefs/C-XXXX.md` : one short cheat sheet per client.

These are YOUR working notes, not clinical records. Every file you write here
starts with this exact header line:

> Agent working memory, not a clinical record. Source of truth is the client
> files under Clients/. Last updated: YYYY-MM-DD.

## Read-only vs write (critical)

- The clinical record (Clients/, Themes/, Interventions/, Templates/) is READ
  ONLY. You read it with the read tools.
- You WRITE only with the agent_memory tools (write_file, edit_file,
  create_directory), which are scoped to `_Agent/`. If a write ever targets a
  path outside `_Agent/`, stop: that is a bug, never force it.
- PATHS for agent_memory tools: the server's root IS the `_Agent` folder, so
  never prefix paths with `_Agent/`. Write the snapshot to exactly
  `/Users/philwoolley/Projects/gemma4hackathon-casenotes/DebriefVault/_Agent/Practice-Snapshot.md`
  and briefs to
  `/Users/philwoolley/Projects/gemma4hackathon-casenotes/DebriefVault/_Agent/briefs/C-XXXX.md`
  (absolute paths always work).

## Steps (one tool call at a time)

1. Identify which clients you touched (the ones you just reviewed or prepped).
   Only refresh those, plus the snapshot.
2. For each touched client, read the current truth from the client files:
   `Clients/<id>/_Profile.md` (name, framework, next_session, risk_flags),
   `Clients/<id>/Treatment-Plan.md` (goals and Status), and the newest note in
   `Clients/<id>/Sessions/` (last-session summary, homework, risk wording).
3. Write or edit `_Agent/briefs/<id>.md` with the header line above plus:
   - Current goals and their Status (verbatim Status words).
   - Last session: date and a one or two line summary.
   - Active homework or between-session task.
   - Risk status: quote the risk_flags field and the latest risk wording
     VERBATIM; if none, write "No risk flags recorded".
   - Watch-fors: 1 to 3 short things to keep an eye on next time.
   Keep each brief UNDER 20 lines. If it grows past that, tighten it.
4. Update `_Agent/Practice-Snapshot.md`: refresh that client's one-line roster
   entry, the risk-flag list (verbatim quotes), the upcoming follow-ups (from
   each profile's next_session), recent session activity, and open homework
   threads. Re-stamp the "Last updated" line with today's date.
5. Re-stamp the "Last updated:" line in every file you changed with today's date.

## Done checklist (verify before you finish; if a box is unchecked, do it now)

- [ ] I read the current client files (profile, plan, newest note) before writing
- [ ] Each brief I wrote is under 20 lines and starts with the not-a-record header
- [ ] Risk wording and risk_flags are quoted VERBATIM from the client files
- [ ] The Practice-Snapshot roster, risk list, and follow-ups reflect what I changed
- [ ] Every file I touched has today's date on its "Last updated" line
- [ ] I wrote ONLY inside _Agent/ (no write anywhere else)

## Failure rule

If a client file you need is missing or empty, say so in the brief ("no session
notes on file yet") and do not invent contents. NEVER write outside `_Agent/`.
