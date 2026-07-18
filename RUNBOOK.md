# Demo Runbook

## Start

On the demo Mac, both local services are already configured:

- Gemma 4 E4B: `http://127.0.0.1:8081`
- Debrief: `http://127.0.0.1:8765`

Open `http://127.0.0.1:8765` in the Mac browser. Use only the fictional Bob and Jordan records in `~/CaseNotesDemoVault`.

## Demo Flow

1. Choose **Clinician voice journal** and Bob.
2. Record a short spoken observation, then review the local transcript.
3. Create the review plan and confirm it contains only a draft-write action.
4. Approve it. Obsidian opens the new dated draft.
5. For the post-session debrief, speak a request that explicitly includes a follow-up appointment and draft email. Review the resolved date before approval.

## First-Run Permissions

macOS must allow the process running Debrief to automate Calendar and Mail, and to record the screen. Approve those prompts only on the demo Mac. Screen Recording is required for the final Gemma vision verification step.

Do not use real client records, real client email addresses, or production calendars in the hackathon demonstration.

## Validation

```bash
cd ~/src/case-notes
~/.local/bin/uv run pytest -q
curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8081/health
```

The local pipeline has been validated with generated spoken audio, local Gemma structured planning, a local Obsidian draft write, and Gemma image understanding. Calendar, Mail, and live screen capture remain permission-gated by macOS.
