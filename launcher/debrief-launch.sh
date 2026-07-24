#!/bin/bash
#
# Debrief launcher, the "brain" behind Debrief.app.
#
# Boots everything Debrief needs (LM Studio server, the Gemma model, the local
# app server) with friendly, plain-language checks, then opens the UI as a
# chromeless app window. Any failed step shows a calm macOS dialog that a
# non-technical user can act on, never a raw error.
#
# Safe to run repeatedly: if a piece is already up, it is left exactly as-is.
# In particular, an already-running app server is NEVER restarted, so the
# permission grants of whatever started it (e.g. the Terminal) are preserved.

# Note: we deliberately do NOT use `set -e`. Each step is checked explicitly so
# we can show a friendly dialog instead of dying silently.

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolve this script's real location (follow symlinks) so REPO_ROOT is correct
# whether run from Terminal or spawned by Debrief.app.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
LAUNCHER_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -P "$LAUNCHER_DIR/.." >/dev/null 2>&1 && pwd)"

LMS_BIN="/Users/philwoolley/.lmstudio/bin/lms"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
DEBRIEF_DOCTOR="$REPO_ROOT/.venv/bin/debrief-doctor"
SERVER_LOG="$LAUNCHER_DIR/server.log"

LM_URL="http://localhost:1234/api/v0/models"
APP_URL="http://127.0.0.1:8377"
APP_HEALTH_URL="http://127.0.0.1:8377/api/clients"

MODEL_ID="gemma-4-12b-it-qat"
MIN_CONTEXT=64000

# One-time marker so the optional Obsidian nudge is only ever shown once.
OBSIDIAN_NUDGE_MARKER="$HOME/.debrief-obsidian-nudge-shown"

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

log() {
  # Timestamped progress to stderr (visible in Terminal / Console, not to user).
  echo "[debrief $(date '+%H:%M:%S')] $*" >&2
}

# Show a friendly, non-technical macOS dialog and exit. Never surfaces stderr.
#   $1 = short step label (for our logs)
#   $2 = plain-language message shown to the user
fail() {
  local step="$1"
  local message="$2"
  log "FAILED at step: $step"
  /usr/bin/osascript >/dev/null 2>&1 <<OSA
display dialog "$message" with title "Debrief" buttons {"OK"} default button "OK" with icon caution
OSA
  exit 1
}

# Pick a Python interpreter for JSON parsing (venv preferred, else system).
python_bin() {
  if [ -x "$VENV_PYTHON" ]; then
    echo "$VENV_PYTHON"
  else
    command -v python3
  fi
}

# ---------------------------------------------------------------------------
# Step A, LM Studio server
# ---------------------------------------------------------------------------

ensure_lm_server() {
  log "Checking LM Studio server..."
  if curl -s --max-time 2 "$LM_URL" >/dev/null 2>&1; then
    log "LM Studio server is already running."
    return 0
  fi

  log "LM Studio server not responding. Starting it..."
  if [ ! -x "$LMS_BIN" ]; then
    # LM Studio is missing entirely (no lms binary AND nothing answering on
    # :1234). Offer to open the download page instead of only giving advice.
    log "LM Studio not installed. Offering the download page."
    local choice
    choice="$(/usr/bin/osascript 2>/dev/null <<'OSA'
display dialog "Debrief needs LM Studio, a free app that runs its private AI. Install it, open it once, then run Debrief again." with title "Debrief" buttons {"Not Now", "Open Download Page"} default button "Open Download Page" with icon caution
OSA
)"
    if [[ "$choice" == *"Open Download Page"* ]]; then
      open "https://lmstudio.ai" >/dev/null 2>&1
    fi
    log "FAILED at step: lm-server (LM Studio not installed)"
    exit 1
  fi
  "$LMS_BIN" server start >/dev/null 2>&1

  # Wait up to 15s for it to answer.
  for _ in $(seq 1 15); do
    if curl -s --max-time 2 "$LM_URL" >/dev/null 2>&1; then
      log "LM Studio server is up."
      return 0
    fi
    sleep 1
  done

  fail "lm-server" "Debrief could not start its AI engine (LM Studio). Open the LM Studio app, then try Debrief again."
}

# ---------------------------------------------------------------------------
# Step B, Model loaded at the right context length
# ---------------------------------------------------------------------------

# Lists every LOADED instance whose id starts with MODEL_ID, one per line as
# "<id> <loaded_context_length>". LM Studio names stacked duplicates with a
# ":2" suffix, so we prefix-match to catch them all.
#
# IMPORTANT: we capture the curl body into a variable and feed it to
# `python3 -c` on stdin. We must NOT pipe curl into `python -` (program on
# stdin), the pipe and the here-program both claim stdin and the JSON is lost,
# which is exactly what made an earlier version think the model was unloaded and
# stack a duplicate. Parsing is real JSON, never grep.
list_loaded_instances() {
  local py resp
  py="$(python_bin)"
  [ -n "$py" ] || return 1
  resp="$(curl -s --max-time 3 "$LM_URL" 2>/dev/null)"
  [ -n "$resp" ] || return 0
  printf '%s' "$resp" | "$py" -c '
import sys, json
prefix = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for m in data.get("data", []):
    mid = m.get("id", "")
    if mid.startswith(prefix) and m.get("state") == "loaded":
        print(mid, m.get("loaded_context_length", 0) or 0)
' "$MODEL_ID"
}

# Highest loaded_context_length among loaded matching instances (empty if none).
max_loaded_context() {
  list_loaded_instances | awk '{ if ($2+0 > m) m = $2+0 } END { if (NR > 0) print m }'
}

# Unload every loaded instance matching the prefix (used before a clean reload).
unload_all_matching() {
  list_loaded_instances | while read -r id _ctx; do
    [ -n "$id" ] && "$LMS_BIN" unload "$id" >/dev/null 2>&1
  done
}

# Defensive de-dupe: keep exactly ONE loaded instance at >= MIN_CONTEXT and
# unload any extras (the ":2"+ suffixed 7GB duplicates). Runs only after a load.
dedupe_instances() {
  local kept=""
  list_loaded_instances | while read -r id ctx; do
    if [ -z "$kept" ] && [ "$ctx" -ge "$MIN_CONTEXT" ] 2>/dev/null; then
      kept="$id"
    else
      log "Unloading extra model instance: $id"
      "$LMS_BIN" unload "$id" >/dev/null 2>&1
    fi
  done
}

ensure_model() {
  log "Checking the AI model..."
  local ctx
  ctx="$(max_loaded_context)"

  # 1) Already loaded with enough context -> do NOTHING. This is the demo path.
  if [ -n "$ctx" ] && [ "$ctx" -ge "$MIN_CONTEXT" ] 2>/dev/null; then
    log "Model already loaded at $ctx context (>= $MIN_CONTEXT). Leaving it untouched."
    return 0
  fi

  # 2) Loaded but too small -> unload the exact instance(s), then reload once.
  if [ -n "$ctx" ]; then
    log "Model loaded at $ctx (< $MIN_CONTEXT). Unloading, then reloading at $MIN_CONTEXT..."
    unload_all_matching
    sleep 1
  else
    # 3) Absent -> a single load at the right context.
    log "Model not loaded. Loading $MODEL_ID at $MIN_CONTEXT (can take ~60s)..."
  fi

  # Exactly one load call. `lms load` is NOT idempotent, so we only ever reach
  # here when we have confirmed nothing suitable is already loaded.
  "$LMS_BIN" load "$MODEL_ID" --context-length "$MIN_CONTEXT" -y >/dev/null 2>&1

  # 4) Poll up to 120s for a loaded instance at >= MIN_CONTEXT before failing.
  local i
  for i in $(seq 1 120); do
    ctx="$(max_loaded_context)"
    if [ -n "$ctx" ] && [ "$ctx" -ge "$MIN_CONTEXT" ] 2>/dev/null; then
      log "Model ready at $ctx context."
      # 5) Make sure exactly one instance is loaded; drop any duplicates.
      dedupe_instances
      return 0
    fi
    sleep 1
  done

  # The model would not load, most often because it has not been downloaded
  # yet. Offer to take them straight to LM Studio's model search instead of just
  # giving advice. (A download page is wrong here; the model lives inside LM
  # Studio, not on a web page.)
  log "Model could not be loaded. Offering to get the model."
  local choice
  choice="$(/usr/bin/osascript 2>/dev/null <<OSA
display dialog "Debrief could not load its AI model ($MODEL_ID). You may just need to download it inside LM Studio first, then try Debrief again." with title "Debrief" buttons {"Not Now", "Get the Model"} default button "Get the Model" with icon caution
OSA
)"
  if [[ "$choice" == *"Get the Model"* ]]; then
    open -a "LM Studio" >/dev/null 2>&1
    /usr/bin/osascript >/dev/null 2>&1 <<OSA
display dialog "In LM Studio, search for $MODEL_ID in the model search and download it. Once it finishes, run Debrief again." with title "Debrief" buttons {"OK"} default button "OK" with icon note
OSA
  fi
  log "FAILED at step: model"
  exit 1
}

# ---------------------------------------------------------------------------
# Step B2, Readiness gate: hand the health verdict to debrief-doctor
# ---------------------------------------------------------------------------

# The old ad-hoc curl reachability checks are gone. debrief-doctor is now the
# single source of truth for "is Debrief ready": it probes the model server,
# confirms the gemma model is loaded, and checks the vault and ffmpeg. We run it
# AFTER ensure_lm_server + ensure_model have started the server and loaded the
# model, gate on its exit code, and surface its plain-text output in the dialog.
run_doctor() {
  log "Running debrief-doctor readiness checks..."
  local output rc
  if [ -x "$DEBRIEF_DOCTOR" ]; then
    output="$("$DEBRIEF_DOCTOR" 2>&1)"
    rc=$?
  elif [ -x "$VENV_PYTHON" ]; then
    output="$("$VENV_PYTHON" -m debrief.doctor 2>&1)"
    rc=$?
  else
    log "debrief-doctor not found; skipping readiness gate."
    return 0
  fi

  # Echo the full table to our logs for debugging.
  printf '%s\n' "$output" >&2

  if [ "$rc" -ne 0 ]; then
    fail "doctor" "Debrief is not quite ready:

$output"
  fi
  log "debrief-doctor passed."
}

# ---------------------------------------------------------------------------
# Step C, App server (started once; never restarted if already up)
# ---------------------------------------------------------------------------

ensure_app_server() {
  log "Checking the Debrief app server..."
  if curl -s --max-time 2 "$APP_HEALTH_URL" >/dev/null 2>&1; then
    # Already running, leave it exactly as-is so we keep whatever permissions
    # its parent process was granted (Terminal on demo day).
    log "App server already running, leaving it untouched."
    return 0
  fi

  log "App server not running. Starting it..."
  if [ ! -x "$VENV_PYTHON" ]; then
    fail "app-server" "Debrief could not find its program files. Make sure Debrief is in its usual folder, then try again."
  fi

  # Background it, detached, logging to launcher/server.log.
  (
    cd "$REPO_ROOT" || exit 1
    HF_HUB_OFFLINE=1 nohup "$VENV_PYTHON" app.py >>"$SERVER_LOG" 2>&1 &
  )

  # Wait up to 20s for the server to answer.
  for _ in $(seq 1 20); do
    if curl -s --max-time 2 "$APP_HEALTH_URL" >/dev/null 2>&1; then
      log "App server is up."
      return 0
    fi
    sleep 1
  done

  fail "app-server" "Debrief's window could not start. Wait a few seconds and try again. If it keeps happening, restart your Mac."
}

# ---------------------------------------------------------------------------
# Step D, Open the UI as a chromeless app window
# ---------------------------------------------------------------------------

open_ui() {
  log "Opening the Debrief window..."
  if [ -d "/Applications/Google Chrome.app" ]; then
    # Chromeless, its own window and Dock presence.
    open -na "Google Chrome" --args --app="$APP_URL" >/dev/null 2>&1 \
      && { log "Opened in Chrome app window."; return 0; }
  fi
  # Fallback: default browser (still lands on the client picker).
  log "Chrome app window unavailable, falling back to default browser."
  open "$APP_URL" >/dev/null 2>&1 \
    || fail "open-ui" "Debrief is ready, but the window could not open by itself. Open your web browser and go to $APP_URL"
}

# ---------------------------------------------------------------------------
# Step E: Optional Obsidian nudge (soft, non-blocking, shown once ever)
# ---------------------------------------------------------------------------

# Runs LAST, AFTER the window is already open, so it can never prevent Debrief
# from starting. Purely a friendly suggestion; if Obsidian is present or we have
# already nudged once, this is a no-op.
nudge_obsidian() {
  [ -e "$OBSIDIAN_NUDGE_MARKER" ] && return 0
  if [ -d "/Applications/Obsidian.app" ]; then
    return 0
  fi

  log "Obsidian not installed. Showing the one-time optional nudge."
  # Mark it shown first, so it never nags again regardless of the choice.
  touch "$OBSIDIAN_NUDGE_MARKER" 2>/dev/null

  local choice
  choice="$(/usr/bin/osascript 2>/dev/null <<'OSA'
display dialog "Optional: Obsidian is a free app for browsing your practice vault visually. Debrief works fine without it." with title "Debrief" buttons {"Skip", "Open Download Page"} default button "Skip" with icon note
OSA
)"
  if [[ "$choice" == *"Open Download Page"* ]]; then
    open "https://obsidian.md" >/dev/null 2>&1
  fi
}

# ---------------------------------------------------------------------------
# Run the checks in order.
# ---------------------------------------------------------------------------

main() {
  log "Starting Debrief (repo: $REPO_ROOT)"
  ensure_lm_server
  ensure_model
  run_doctor
  ensure_app_server
  open_ui
  log "Debrief is ready."
  # Soft, non-blocking suggestion, always after the window is open.
  nudge_obsidian
}

main "$@"
