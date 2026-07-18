---
name: caseload-risk-review
description: "List every client with a non-empty risk flag and quote their latest risk documentation."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [risk, safety, caseload, review, suicidal, SI, risk flags, debrief, vault]
    related_skills: [session-prep]
---

# Caseload Risk Review

Use this when the therapist asks about risk across the whole caseload: "who is
at risk", "risk review", "any safety flags", "which clients need watching".

You have READ ONLY access to the vault. This is a safety-relevant task, so be
precise and quote sources.

## Steps

1. List `Clients/` to enumerate every client folder.
2. For each client, read `_Profile.md` and inspect the `risk_flags` frontmatter
   field.
3. Keep only clients whose `risk_flags` is non-empty.
4. For each flagged client, open their `Sessions/` folder and read the newest
   note (last filename alphabetically). Find any risk assessment or risk
   wording and quote it verbatim. If there are no session notes, say the flag
   exists in the profile but there is no session-level documentation yet.

## Answer format

- One block per flagged client:
  - **Name (id)** and the exact `risk_flags` value.
  - **Latest risk documentation**: verbatim quote + which file it came from, or
    "no session note on file".
- If no client has a risk flag, say "No clients currently carry a risk flag."

## Done checklist

- [ ] I read _Profile.md for every client folder
- [ ] I included every client with a non-empty risk_flags and excluded the rest
- [ ] For each flagged client I quoted the risk wording verbatim or stated none exists
- [ ] I named the source file for each quote

## Failure rule

Never downgrade or paraphrase a risk flag. Quote it exactly. If a profile is
unreadable, name the file and flag it for manual review rather than skipping it
silently.
