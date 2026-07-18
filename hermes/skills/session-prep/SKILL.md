---
name: session-prep
description: "Prep a therapist for their next session with a named client, using only the read-only vault."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [session, prep, prepare, next session, client, therapy, debrief, vault]
    related_skills: [client-lookup, caseload-risk-review]
---

# Session Prep

Use this when the therapist says any of: "prep me for my next session with X",
"get me ready for X", "brief me on X before we meet", "what should I cover with X".

You read the clinical record READ ONLY. The one place you may write is your own
working memory under `_Agent/` (see the final step and the maintain-snapshot
skill). Never write, edit, or delete anything under Clients/.

## Steps (do them in order, one tool call at a time)

0. Fast context first. If `_Agent/briefs/<client>.md` exists for this client,
   read it before anything else: it is your own cheat sheet (goals, last
   session, homework, risk, watch-fors) and gets you oriented fast. Treat it as
   a hint, not the truth: you MUST still verify every fact against the real
   client files in the steps below. If no brief exists, just continue.
1. Resolve the client. Read each `Clients/*/  _Profile.md` file and match the
   `name` field to the person the therapist named. The folder is an id like
   C-0001, not the name, so never guess it. If two clients could match, stop
   and ask which one.
2. Read that client's `_Profile.md` in full. Note framework, presenting
   concerns, themes, next_session, and risk_flags.
3. Read that client's `Treatment-Plan.md`. Quote each goal heading and its
   Status verbatim.
4. List the client's `Sessions/` folder. The newest note is the last filename
   alphabetically. If the folder is empty, say "No session notes on file yet"
   and do not invent any session history.
5. If a newest session note exists, read it. Pull out: what was worked on, the
   client's response, any homework assigned, and any "Next Session
   Considerations".
6. Update working memory. If anything in the brief you read at step 0 was stale
   or missing (new session, changed risk, new homework), refresh
   `_Agent/briefs/<client>.md` using the maintain-snapshot skill (write ONLY
   inside _Agent/, quote risk wording verbatim, re-stamp the date). If there was
   no brief, create one. This is the only writing you do.

## Answer format

- **Open goals** (verbatim goal text + status)
- **Last session focus** (what was covered and how the client responded, or
  "no notes yet")
- **Homework assigned** (or "none on file")
- **Suggested focus for next session** (clearly labelled as your suggestion)
- **Risk flags** (quote the risk_flags field; if empty, say "none recorded")

## Done checklist (verify before you answer; if any box is unchecked, do that step now)

- [ ] I matched the name to a client id by reading _Profile.md
- [ ] I read _Profile.md for that client
- [ ] I read Treatment-Plan.md and quoted the goals verbatim
- [ ] I checked the Sessions/ folder and read the newest note, OR stated there are none
- [ ] My answer includes goals, last-session focus, homework, and risk flags
- [ ] I checked _Agent/briefs/<client>.md and refreshed it if it was stale or missing (writing only in _Agent/)

## Failure rule

If a file you need is missing, name the exact file and stop. Do not fabricate
its contents.
