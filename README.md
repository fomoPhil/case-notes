# Debrief

One spoken debrief after each therapy session. Debrief writes the progress note, books the follow-up, drafts the client email, and verifies it all on screen. Every step runs locally on your Mac with Gemma 4: your clients' words never leave the device.

It also supports a separate **Clinician Voice Journal**: between sessions, a therapist can dictate an observation, session-prep thought, or follow-up idea. The app turns it into a dated draft in that client's local workspace. Journal entries never schedule, email, sign, or send anything.

## Current implementation

The runnable MVP is intentionally narrow and uses synthetic data by default:

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. Paste or record a spoken debrief, review the action plan, and approve it. `CASE_NOTES_EXECUTION_MODE=demo` is the default and writes only to the local demo vault. `live` mode is macOS-only and must be explicitly enabled after Calendar, Mail, Screen Recording, and Obsidian permissions have been granted.

For local Gemma inference set `CASE_NOTES_MODEL_BASE_URL` and `CASE_NOTES_MODEL_ID` to a local OpenAI-compatible server. The app refuses live actions if the local model or screen verification is unavailable.

Built for the "Build with Gemma: JustBuild" hackathon (July 17-18, 2026), Track 2: Voice-to-Action Agents.

- Plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
- Session handoff / current state: [HANDOFF.md](HANDOFF.md)
