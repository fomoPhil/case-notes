---
name: client-lookup
description: "Find the right client folder from a partial, misspelled, or voice-transcribed name."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [client, lookup, find, who is, match name, spelling, debrief, vault]
    related_skills: [session-prep]
---

# Client Lookup

Use this when the therapist gives a name that may be partial, misspelled, or
came from dictation (for example "bob smyth", "the Jane one", "my anxiety
client"). Your job is to map it to exactly one client id before doing anything
else.

You have READ ONLY access to the vault.

## Steps

1. List `Clients/` to see the available client folders.
2. Read every `Clients/*/_Profile.md` and collect each `name`, `client_id`,
   `presenting_concerns`, and `framework`.
3. Match the therapist's words against those names phonetically and loosely:
   - "smyth" matches "Smith", "jayne" matches "Jane", first-name-only is fine.
   - If they described the client instead of naming them (for example "my
     anxiety client"), match on presenting_concerns or themes.
4. Decide:
   - Exactly one plausible match: state the resolved client ("You mean Bob
     Smith, C-0001") and continue with whatever they asked.
   - More than one plausible match: ask ONE clarifying question listing the
     candidates. Do not guess.
   - No match: say so and list the client names on file.

## Answer format

- **Resolved client**: full name + id, or the clarifying question.
- Then proceed to the actual request if resolution was unambiguous.

## Done checklist

- [ ] I listed Clients/ and read the _Profile.md names
- [ ] I picked exactly one id, or asked one clarifying question, or reported no match
- [ ] I did not guess a folder id without reading a matching name

## Failure rule

Never answer about a client until the id is resolved. If unsure between two,
ask; do not pick one silently.
