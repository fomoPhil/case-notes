# Debrief Architecture Contract

All modules live in `debrief/` (Python package, run with `.venv/bin/python`). This file is the integration contract: build agents implement exactly these signatures. Principle: deterministic hands, model brain, model eyes.

## Verified environment (do not re-verify, already tested)

- LM Studio server: `http://localhost:1234/v1/chat/completions`, model id `gemma-4-12b-it-qat` (loaded, VLM).
- MUST pass `"reasoning_effort": "none"` in every request (verified: disables thinking, 0 reasoning tokens).
- Structured output: `response_format: {"type":"json_schema","json_schema":{"name":..., "strict":true, "schema":{...}}}` (verified working).
- Vision: OpenAI-style `image_url` with `data:image/png;base64,...` (verified: reads screenshots accurately). Downscale screenshots with `sips -Z 1512` first.
- STT: parakeet-mlx, model `mlx-community/parakeet-tdt-0.6b-v2`, called from `.venv/bin/python`.
- macOS TCC granted for this terminal: Calendar + Mail automation via osascript, screencapture.
- Test audio without a mic: `say -o x.aiff "..."` then `ffmpeg -y -i x.aiff -ar 16000 -ac 1 x.wav`.

## Module contract

```python
# debrief/config.py
VAULT_DIR: Path            # ./vault
LMSTUDIO_URL: str; MODEL: str = "gemma-4-12b-it-qat"
CALENDAR_NAME = "Debrief"  # dedicated macOS calendar; create if missing; never touch other calendars

# debrief/llm.py
def chat(messages: list, schema: dict | None = None, images: list[str] | None = None,
         max_tokens: int = 2000, temperature: float = 0.2) -> str | dict
# images = file paths; encode as data URIs onto the LAST user message. schema given -> returns parsed dict.
# Always sends reasoning_effort:"none". Raises RuntimeError with body text on non-200 or JSON parse failure.

# debrief/stt.py
def transcribe(wav_path: str) -> str                      # parakeet
def correct_transcript(text: str) -> str                  # Gemma glossary pass, prompts/glossary.md

# debrief/dates.py
def resolve_utterance(utterance: str, now: datetime) -> datetime | None
# Pure python (dateutil + rules): "next Tuesday at 3" / "same time next week" / "tomorrow morning" (->9:00).
# Ambiguous hour 1-7 with no am/pm -> assume PM (therapists book afternoons). Returns None if unparseable.

# debrief/extract.py
def extract(transcript: str, client_ctx: dict, framework: str, now: datetime) -> dict
# One constrained LLM call. Returns EXTRACT_SCHEMA-shaped dict; then dates.resolve_utterance fills
# actions[i]["resolved_datetime"] (ISO) in a post-pass. Prompts live in prompts/extract_system.md.

EXTRACT_SCHEMA output shape:
{
  "note": {
    "data": str, "assessment": str, "plan": str,
    "risk_present": bool,
    "risk": {"assessed": bool, "ideation": str, "plan_intent_means": str,
             "protective_factors": str, "interventions_taken": str} | null,
    "interventions": [str], "themes": [str],
    "client_quotes": [str]          # verbatim quotes carried from transcript (grounding check target)
  },
  "actions": [
    {"type": "schedule_followup", "datetime_utterance": str, "duration_min": int},
    {"type": "draft_client_email", "purpose": str, "attachment": str | null}
  ],
  "unsupported_requests": [str],
  "next_session_suggestions": [str]  # 2-3, phrased as options ("Consider...", "Possible focus...")
}

# debrief/vault.py
def ensure_vault() -> None                                # scaffold folders + mock clients if missing
def list_clients() -> list[dict]                          # from Clients/*/_Profile.md frontmatter
def client_context(client_id: str) -> dict                # profile fm + summary body + last session note text
def write_session_note(client_id: str, note: dict, transcript: str, meta: dict) -> Path
# Atomic (tmp+rename), ALWAYS a new file Sessions/YYYY-MM-DD-session.md (suffix -2 if exists).
# Markdown: frontmatter per IMPLEMENTATION_PLAN.md Appendix A, then ## Data/## Assessment/## Plan,
# ## Risk only when risk_present, ## Next Session Considerations. NO em dashes anywhere in output.
def update_profile(client_id: str, new_summary: str, updates: dict) -> None
def obsidian_open_uri(path: Path) -> str

# debrief/actions.py
def create_calendar_event(title: str, dt: datetime, duration_min: int) -> bool   # osascript, CALENDAR_NAME
def open_calendar_at(dt: datetime) -> None                                       # make event visible on screen
def create_mail_draft(to: str, subject: str, body: str, attachment: Path | None) -> bool  # visible:true, NEVER send
def delete_test_events(title_prefix: str) -> int          # cleanup for repeated test runs

# debrief/verify.py
def verify_on_screen(checks: list[dict]) -> list[dict]
# check: {"surface": "calendar"|"obsidian"|"mail", "question": str}
# Brings surface frontmost (osascript activate), sleeps 1.5s, screencapture -x, sips downscale,
# llm.chat with VERIFY_SCHEMA {"confirmed": bool, "what_i_see": str}. Returns checks + results.

# debrief/pipeline.py
def run_debrief(wav_path: str, client_id: str, execute: bool = True, verify: bool = True) -> dict
# transcribe -> correct -> extract -> resolve dates -> (execute actions) -> (verify) -> update profile.
# Returns everything: transcript, note, actions w/ status, verification results, note_path, timings dict.
```

## Server contract (app.py, phase 2)

- `GET /api/clients` -> list_clients()
- `POST /api/debrief` multipart: audio file + client_id -> runs through extract, returns plan WITHOUT executing
- `POST /api/execute` json: the approved result of /api/debrief -> executes actions + verify, returns results
- Static SPA at `/` from `static/index.html`: client picker, MediaRecorder record button, transcript,
  approval checklist (shows resolved absolute datetimes), Execute button, results panel with what_i_see text.

## Mock clients (vault scaffold)

- C-0001 Bob Smith, bob@example.com, framework CBT, themes work stress/worthlessness, risk_flags SI history (matches the transcript that passed the SI test).
- C-0002 Jane Doe, jane@example.com, framework ACT, themes anxiety/sleep.
- All names/emails fictional. Include Treatment-Plan.md with 2 goals each. Include Templates/Worksheets/thought-record.pdf (generate a simple one-page PDF via Python or use a .md file if PDF is a hassle; Mail attachment can be any file).

## Style rules (hard)

- No em dashes in ANY generated copy, UI strings, prompts, or vault templates.
- Suggestions language: "Consider", "Possible focus", never "You should". Include the line "Clinical judgment and final session planning remain the therapist's responsibility." in the note's suggestions section.
- Clinical system prompts include the exact framing that passed the SI test (see prompts/extract_system.md).
