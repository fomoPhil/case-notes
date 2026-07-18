# Debrief x Hermes Agent (local "second brain" over the vault)

This layer lets a therapist talk to their Debrief vault in natural language,
fully locally, using [Hermes Agent](https://hermes-agent.nousresearch.com) as
the conversational front end and the already-running LM Studio server as the
model. Nothing leaves the machine.

Demo moment: "Prep me for my next session with Bob Smith" and Hermes answers
from the vault files (verbatim treatment goals, real risk flags), read-only.

## What this is

- A dedicated Hermes **profile** named `debrief`, fully isolated from the user's
  main Hermes setup (its own config, sessions, and MCP servers). Nothing here
  touches the primary profile.
- Model: the existing LM Studio OpenAI-compatible server at
  `http://localhost:1234/v1`, model `gemma-4-12b-it-qat`. We do not install or
  restart anything.
- Vault access: a **read-only** filesystem MCP scoped to exactly the
  `DebriefVault` folder. Only 3 read tools are exposed (list, read, search); the
  server's write tools are never surfaced. Our pipeline stays the only writer.

## Files here

- `config.yaml` — the exact profile config (copy of the installed
  `~/.hermes/profiles/debrief/config.yaml`). Fully commented.
- `skills/` — four per-task Hermes skills tuned for a small local model
  (session-prep, client-lookup, caseload-risk-review, week-review). Each has
  numbered steps, a done-checklist, and a failure rule.
- `run_tasks.py` — a serial runner that executes each task in a **fresh** Hermes
  context, using `state/TASKS.md` and `state/RESULTS.md` as the only memory
  between tasks (avoids small-model context degradation).
- `state/TASKS.md`, `state/RESULTS.md` — the runner's task queue and audit log.
- `acceptance_transcript.txt` — captured proof the agent reads the vault.

## Prerequisites (already true on the demo Mac)

- Hermes Agent installed. If starting fresh:
  `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
- LM Studio running with the model **loaded at a 64K context window**. This is
  required — see the note below. Load it with:

  ```
  lms load gemma-4-12b-it-qat --context-length 64000 -y
  ```

  Verify with `lms ps` (CONTEXT column should read 64000).

### Why the 64K context matters

Hermes refuses any model it detects as having under 64K context. Separately, LM
Studio splits the loaded context across its `PARALLEL` slots (default 4), so a
model loaded at 32000 only gives each request ~8000 usable tokens, which
overflows once the agent prompt plus tool schemas plus file reads add up. Load
the model at 64000 and both problems disappear. Do not lower the parallelism or
touch LM Studio during the demo; just load at 64000.

## Install (reproduce this profile from scratch)

```bash
# 1. Create the isolated profile (also creates a `debrief` wrapper on PATH)
hermes profile create debrief

# 2. Drop in the config (model endpoint + read-only MCP + operator prompt)
cp hermes/config.yaml ~/.hermes/profiles/debrief/config.yaml

# 3. Install the four task skills into the profile
for s in session-prep client-lookup caseload-risk-review week-review; do
  mkdir -p ~/.hermes/profiles/debrief/skills/$s
  cp hermes/skills/$s/SKILL.md ~/.hermes/profiles/debrief/skills/$s/SKILL.md
done

# 4. Sanity check
debrief config check          # config version prints, no errors
debrief mcp list              # filesystem: 3 selected, enabled
debrief skills list | tail -1 # ...4 local skills enabled
```

`debrief` is just a wrapper for `hermes -p debrief`.

## Run

Interactive:

```bash
debrief chat
```

One-shot (scriptable, what the acceptance test uses):

```bash
debrief chat -q "Summarize Bob Smith's current treatment goals and what we worked on in his most recent session. Use the vault files." --yolo
```

Fresh-context serial runner (each task in a clean session):

```bash
python3 hermes/run_tasks.py          # runs all pending tasks in state/TASKS.md
python3 hermes/run_tasks.py --one    # runs only the next pending task
```

Notes:
- `--yolo` skips approval prompts so a non-interactive run does not hang. The
  agent only has read tools, so there is nothing destructive to approve anyway.
- First run of a session spawns the `npx` filesystem MCP; subsequent tool calls
  are instant.
- Latency for a full multi-file query is roughly 1 to 3 minutes on this machine
  (local 12B, many small tool calls). Fine for a chat surface, not instant.

## Read-only guarantee (how)

- `toolsets: []` in the config disables all of Hermes's built-in terminal /
  file / web / browser tools.
- The `filesystem` MCP is scoped to a single root (`DebriefVault`) and uses a
  strict `tools.include` whitelist of `list_directory`, `read_text_file`,
  `search_files`. Its write tools (`write_file`, `edit_file`, `move_file`,
  `create_directory`) are never exposed to the model.
- Verified at runtime: a path outside the vault returns "Access denied - path
  outside allowed directories" (see acceptance_transcript.txt).

## Working memory: the _Agent/ layer

Hermes keeps track of the most important stuff and updates it as it goes,
without ever being able to touch the clinical record. Design:

- The clinical record (`Clients/`, `Themes/`, `Interventions/`, `Templates/`)
  stays READ ONLY for Hermes, exactly as above.
- A SECOND filesystem MCP server, `agent_memory`, is scoped to
  `DebriefVault/_Agent` and whitelists `write_file`, `edit_file`,
  `create_directory` plus the read tools. Because the server root IS `_Agent`,
  the write tools physically cannot reach the clinical files: the guarantee is
  enforced by path scoping, not just by prompt.
- What lives in `_Agent/`:
  - `Practice-Snapshot.md`: the practice at a glance (roster one-liners, risk
    flags quoted verbatim, upcoming follow-ups from each profile's
    `next_session`, recent session activity, open homework threads), with a
    date-stamped "Last updated" line.
  - `briefs/C-XXXX.md`: a 10 to 15 line cheat sheet per client (goals + status,
    last session, homework, risk status verbatim, watch-fors). Each file's
    header states it is agent working memory, not a clinical record.
- The `maintain-snapshot` skill owns these files: it fires after session
  reviews, on "update your notes/memory", and at the end of prep tasks. Rule:
  NEVER write outside `_Agent/`.
- The `session-prep` skill reads `_Agent/briefs/<client>.md` first for fast
  context (step 0), verifies against the real client files, and refreshes the
  brief if it was stale (final step).
- Bootstrap: the initial `_Agent/` content was generated by Hermes itself via
  `run_tasks.py` (one maintain-snapshot task per client plus one snapshot task
  in `state/TASKS.md`), proving the scoped write path works end to end.
