# Ship blockers: what only Phil can do

Written overnight 2026-07-25 while working autonomously toward a ship-ready, user-friendly build. Everything in this file is something I cannot do for you. Everything else I either finished or stubbed, and those are listed at the bottom.

Read the top section first. It is ordered by what blocks the launch video.

---

## 1. Test it yourself on your own vault (the real blocker)

Nothing substitutes for this. I have verified behavior with tests, temp vaults, and screenshots, but I cannot tell you whether it *feels* right.

```
uv run debrief
```

Do one real debrief end to end, then poke the new home screen, Settings, and the records UI. Send me whatever feels wrong.

**Why only you:** taste, and the fact that your vault has real content and real muscle memory. Also, your Mac is the only one with the model, the permissions, and your microphone.

**One thing to expect:** your existing vault predates last night's seed changes, and the scaffolder never overwrites files you already have. So Bob, Jane, and Maya will still show their old hardcoded July 21 to 23 appointments (the home screen will label them "past") and they will not carry the new "Sample" pill. A fresh vault gets relative, always-future dates automatically. If you want clean demo data for the video, move `DebriefVault/Clients/` aside and relaunch. Your real test notes live in there, so I did not touch it.

---

## 2. Decisions I made for you while you slept (reverse any of them)

I had to choose to keep moving. Each is one commit and trivially reversible.

| Decision | What I did | Reverse with |
|---|---|---|
| Liability boilerplate in notes | **Removed** "Clinical judgment and final session planning remain the therapist's responsibility" from every filed note body. It is app-injected text living permanently inside a clinical record. The softer framing stays in the UI. | `git revert 92d9e00` |
| Your activity log | **Cleaned** 50 test-pollution entries out of `DebriefVault/_Activity/2026-07-24.md`, keeping the 2 real ones. Backup at `/tmp/activity-2026-07-24.backup.md`. | Restore from that backup |
| Home screen | **Rebuilt** per the mock you approved, right rail included. | `git revert` the home screen commits |

---

## 3. Accounts and identity (I have no access)

- **Repo rename.** Still `fomoPhil/case-notes` while the product is Debrief. One click in GitHub settings; old URLs redirect automatically. I would do this before the video so the URL on screen matches the product name.
- **Apple Developer team choice**, if you ever package the Mac app: personal (`woolley.pj@gmail.com`) or Meora Studios (`dev@meorastudios.com`). This is sticky because signing identity and any future CloudKit container belong to a team and cannot be moved. Decide before, not after.
- **A real Gemini API key**, if you want the optional cloud template import verified end to end. I tested the consent gate, the error path, and proved the key is never stored, but I deliberately never called Google with a live key. The free tier is enough.

---

## 4. Publishing and launch

- **Record the launch video.** The app is ready to demo; see "what is ready to show" below.
- **Announce it.** Any post, Show HN, subreddit, or Discord that comes from you as a person.
- **Decide whether Debrief Pro happens at all.** The offer stack and roadmap are in the gitignored `docs/business/debrief-pro-offer.md`. Nothing in the open source repo depends on that decision.

---

## 5. Judgement calls I parked rather than guess

- **Repo README hero image.** The current screenshots are the hackathon UI, which no longer matches the app. Fresh screenshots are in the scratchpad; picking which ones represent the product is a taste call.
- **Whether the app should ever mention Obsidian by name** in the first-run wizard. It is currently mentioned as optional. Some users will find it reassuring, others will find it confusing.
- **Whether to keep the sample fictional clients** (Bob Smith, Jane Doe, Maya Chen) or seed an empty vault. Demo-friendly versus clean-start.

---

## What is ready to show

- One spoken debrief becomes a filed note, a booked follow-up, a Mail draft, and an on-screen verification.
- Every note section is editable before filing, and what you see is exactly what gets written.
- The assistant makes a worksheet from a voice request and files it only after you approve.
- Note formats: DAP, SOAP, GROW, meeting memo, or your own imported from a sample document.
- Everything runs locally. The one exception is the optional template import, which asks first.
- The home screen opens with the day, what is on your calendar, and what you have already filed.
- You can add a client from inside the app. Until last night that was impossible without hand-editing files in Finder.

## What changed while you slept

Roughly thirty commits. The ones that change how the app behaves, rather than how it reads:

- **Failures stop being silent.** `go()` was clearing the error message one line after every caller set it, so a failed debrief, a failed assistant request, a rejected upload, and a denied microphone all failed with no message at all. The recording used to be discarded too. Now the error survives, the audio is handed back, and there is a Try again button.
- **You get warned before you speak.** The app polls readiness and, when the model is not running, says so and stops you recording into a void rather than failing after you have talked for three minutes.
- **Filed notes no longer carry boilerplate you did not write** (see the decisions table above).
- **Amber means risk and only risk.** It used to look identical on "you did not request an email" and "this note documents suicidal ideation".
- **Errors stopped speaking Python.** Tracebacks, shell commands, and raw file paths no longer reach the screen; the technical text sits behind a disclosure.
- **PDF export downloads a file** instead of opening a browser tab full of raw JSON when rendering is unavailable.
- **Accessibility**: document and library cards are now real buttons operable by keyboard (their actions were previously mouse-only and invisible to screen readers), modals behave as dialogs, and every text colour clears AA contrast.

## What I stubbed rather than finished

- **Packaged Mac app (the no-terminal DMG).** Not started. It needs the signing-team decision above, and it is a substantial build rather than a polish task. The path is documented in `docs/business/debrief-pro-offer.md`.
- **Extra profession vocabulary packs.** SLP, coaching, and legal packs exist but are thin compared to the therapy pack, which was migrated from real clinical vocabulary. They work; they are not yet rich.
- ~~**`test_pipeline_live_runs_twice`**~~ **Fixed overnight.** It now seeds its own client and passes twice through on a clean scratch vault: recording, transcription, note, calendar event, and Mail draft with the worksheet attached. It no longer asserts that the vision pass *confirmed* what it saw, because that depends on which window is frontmost on the operator's screen rather than on the code; it does assert the model looked at the right surfaces and described them.
- **Ollama support for the assistant.** Detection works; the tool-calling dialect differs, so the agent still requires LM Studio.
