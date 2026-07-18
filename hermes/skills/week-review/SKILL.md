---
name: week-review
description: "Summarize all session notes across clients within a date range."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [week, review, recap, date range, sessions this week, summary, debrief, vault]
    related_skills: [session-prep, caseload-risk-review]
---

# Week Review

Use this when the therapist asks for a recap across clients over a period: "what
happened this week", "recap last week's sessions", "summarize sessions from the
7th to the 14th".

You have READ ONLY access to the vault.

## Steps

1. Determine the date range. If the therapist gave explicit dates, use them. If
   they said "this week" or "last week", use the system date to compute the
   range and state the range you used.
2. List `Clients/` to enumerate every client folder.
3. For each client, list their `Sessions/` folder. Session filenames start with
   a date, YYYY-MM-DD. Select only notes whose date falls inside the range.
4. Read each selected note. Pull out the client, the date, the main focus, and
   any homework or risk items.
5. If no notes fall in the range, say so and state the range you checked.

## Answer format

- **Range covered**: the exact dates used.
- One short bullet per session, grouped by client:
  - **Name (id) YYYY-MM-DD**: focus, then any homework or risk item.
- **Flags to follow up**: any risk items seen across the range, or "none".

## Done checklist

- [ ] I stated the exact date range I used
- [ ] I checked every client's Sessions/ folder
- [ ] I included only notes inside the range and read each one
- [ ] I surfaced any risk items separately at the end

## Failure rule

Do not include a session you did not actually read. If a Sessions/ folder is
empty, skip that client silently (an empty caseload week is a valid answer).
