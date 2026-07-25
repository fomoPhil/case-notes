# Debrief

One spoken debrief after each therapy session. Debrief writes the progress note, books the follow-up, drafts the client email, and verifies it all on screen. Everything runs locally on your Mac with Gemma 4.

> **2nd place, Build with Gemma: JustBuild hackathon (July 2026)**, Track 2: Voice-to-Action Agents. The frozen winning build is tagged [`v0.1-hackathon`](https://github.com/fomoPhil/case-notes/releases/tag/v0.1-hackathon). The full submission writeup is in [WRITEUP.md](WRITEUP.md).

![Review the note and action plan before anything executes](design/kaggle-media/04-review-note.png)

<p>
  <img src="design/kaggle-media/01-choose-client.png" width="19%" alt="Choose a client">
  <img src="design/kaggle-media/02-recording.png" width="19%" alt="Record a spoken debrief">
  <img src="design/kaggle-media/03-processing.png" width="19%" alt="Local processing">
  <img src="design/kaggle-media/04-review-note.png" width="19%" alt="Review the plan">
  <img src="design/kaggle-media/05-verified-results.png" width="19%" alt="Verified on screen">
</p>

## What it does

After every session, a solo therapist loses 15 to 30 minutes to admin. Debrief turns one 60 to 90 second spoken debrief into all of it:

1. **Files an audit-ready progress note** into a plain-markdown client vault, grounded in the transcript with verbatim client quotes, framework-authentic vocabulary (CBT, ACT, DBT, family systems, EMDR, psychodynamic), and a mandatory structured risk section whenever suicidal ideation is mentioned. The note format is yours to pick (see "Make it yours" below).
2. **Books the follow-up** in Apple Calendar (the resolved date and time are shown for approval first).
3. **Drafts the client email** in Apple Mail with the worksheet attached. Always a draft, never auto-sent.
4. **Verifies it on screen**: it screenshots Calendar, the note, and Mail, and Gemma 4 vision reads the live screen to confirm each action actually happened.

There is also an **assistant** for everything that is not a session debrief. Ask it out loud for a box breathing worksheet, an email draft, or something from a client's history, and it prepares the work for you to approve. It stages proposals; it never files or sends on its own.

Your recordings, notes, and client records never leave this Mac. The one exception is the optional template importer described under "Make it yours", which asks first and sends only the one template document you choose. The demo data is fictional.

## Make it yours

A Settings screen (and a first-run wizard that walks you through the same choices) lets you shape Debrief to how you actually work. Everything here is stored in a plain-text `_Settings` folder inside your vault.

- **Profession onboarding.** Pick your profession (therapy and others) so the vocabulary and note guidance match your field.
- **Note formats.** Choose the structure of the filed note: DAP, SOAP, GROW, a meeting memo, or a custom format you import (see below). The risk section only appears in clinical formats.
- **Two speech-to-text engines.** Parakeet (the default, fast and fully offline) or mlx-whisper. The first transcription on a new engine downloads its model once, then runs offline.
- **Personal dictionary.** Add names, acronyms, and terms you use so they are transcribed correctly. Your dictionary is layered into the transcription correction pass.
- **Editable review.** Before anything is filed, you review the note and can edit any section inline. Your edits are exactly what gets filed, word for word.
- **Template import.** Upload a blank or example note template (`.md`, `.txt`, or `.docx`) and Debrief compiles it into a reusable format with your own sections. This runs locally by default. You can optionally turn on a one-time cloud boost that sends only that one template document, once, to Google Gemini using your own API key; nothing else is ever sent, the key is never stored, and if it fails Debrief falls back to compiling locally.

## How it works

The architecture principle is **deterministic hands, model brain, model eyes.**

- **Brain**: Gemma 4 12B (QAT, via LM Studio) makes one JSON-schema-constrained call that extracts the clinical note and the requested actions from the transcript. It is also the editor (a glossary pass fixes clinical-term transcription errors) and the eyes (its vision reads the verification screenshots).
- **Hands**: actions execute through deterministic code only (atomic file writes, AppleScript). The model never does date math: it copies the spoken time phrase ("next Tuesday at 3") and Python resolves it, with the absolute datetime shown in an approval checklist before anything runs. A dedup guard makes double-booking impossible.
- **Stack**: FastAPI server + a vanilla JS web UI with no build step + a plain markdown vault + LM Studio for the model + parakeet-mlx or mlx-whisper for fully offline speech-to-text on Apple Silicon.

This split is why a 12B local model is enough to run a reliable agent.

The vault is plain markdown files on disk. [Obsidian](https://obsidian.md) is a nice optional viewer for it, not a dependency.

## Requirements

- Apple Silicon Mac
- [LM Studio](https://lmstudio.ai) with the Gemma model loaded at 64k context:

  ```bash
  lms load gemma-4-12b-it-qat --context-length 64000 -y
  ```

- macOS Automation permissions for Calendar and Mail, plus Screen Recording for the vision verification step (macOS prompts on first use)
- [uv](https://docs.astral.sh/uv/) and ffmpeg

## Quickstart

```bash
uv sync --extra pdf
uv run debrief
```

The `pdf` extra enables PDF export of notes and worksheets (WeasyPrint; run `brew install pango` if PDF rendering reports missing libraries). Skip the extra if you only want markdown notes.

First run downloads the default speech-to-text model, Parakeet (a few hundred MB); after that everything works fully offline (set HF_HUB_OFFLINE=1 to force it). If you switch to the mlx-whisper engine in Settings, its model (about 1.6 GB) downloads once on first use.

Open http://127.0.0.1:8377. The client vault scaffolds itself on first run with three fictional clients. Pick one, record a spoken debrief, review the note and action plan, and approve it.

### First run

The app opens with a setup screen. It checks your model server and Gemma model, shows where your vault lives, and walks you through the three macOS permissions (Calendar, Mail, Screen Recording) with a Grant button that shows each system prompt so you can click Allow. Screen Recording needs the app quit and reopened after you allow it. You can skip permissions and grant them later, and reopen the wizard any time from Setup in the sidebar. Running `debrief-doctor` in the terminal shows the same model and vault checks.

For development you can also run the server directly with `HF_HUB_OFFLINE=1 .venv/bin/python app.py`. To check your setup at any time, run `uv run debrief-doctor`.

## Roadmap

An in-app voice assistant for open-ended requests, a client records UI, a settings screen with profession and format choices, and editable pre-file review have shipped. A one-command install and more profession packs are next.

## License

MIT. See [LICENSE](LICENSE).
