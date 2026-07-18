# Debrief

One spoken debrief after each therapy session. Debrief writes the progress note, books the follow-up, drafts the client email, and verifies it all on screen. Every step runs locally on your Mac with Gemma 4: your clients' words never leave the device.

It also supports a separate **Clinician Voice Journal**: between sessions, a therapist can dictate an observation, session-prep thought, or follow-up idea. The app turns it into a dated draft in that client's local workspace. Journal entries never schedule, email, sign, or send anything.

## Run it

The full agent (STT, DAP note extraction, admin actions, vision verification):

```bash
uv sync
HF_HUB_OFFLINE=1 .venv/bin/python app.py
```

Open `http://127.0.0.1:8377`. Pick a client, record a spoken debrief, review the note and action plan, and approve it. Requires: Apple Silicon Mac, LM Studio serving `gemma-4-12b-it-qat` on port 1234, parakeet-mlx, and macOS Automation (Calendar, Mail) plus Screen Recording permissions. The demo vault ships with fictional clients; all processing is local.

An earlier, simpler voice-journal MVP also lives in `app/` (port 8765, demo mode by default); it predates the full agent and is kept for reference.

Built for the "Build with Gemma: JustBuild" hackathon (July 17-18, 2026), Track 2: Voice-to-Action Agents.

- Plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
- Session handoff / current state: [HANDOFF.md](HANDOFF.md)
