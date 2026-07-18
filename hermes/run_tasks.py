#!/usr/bin/env python3
"""
Debrief x Hermes serial task runner.

Why this exists: a local 12B model degrades as its context grows. Running each
task in its own FRESH Hermes session, with an external markdown scratchpad as
the only memory between tasks, keeps every task on a clean context.

How it works:
  1. Read hermes/state/TASKS.md, a checklist. Each pending line looks like:
        - [ ] session-prep: Prep me for my next session with Bob Smith
     The token before the colon is the Hermes skill to preload. The rest is the
     instruction.
  2. Take the FIRST unchecked task only.
  3. Spawn a brand-new non-interactive Hermes session (no --resume / --continue,
     so context is fresh) with that skill preloaded and the instruction as the
     query. The agent is READ ONLY (vault filesystem MCP, read tools only), so
     the agent does NOT write results itself. It ends its reply with a line
        STATUS: DONE
     or
        STATUS: BLOCKED: <reason>
  4. The runner captures the agent's answer, appends it to
     hermes/state/RESULTS.md together with the skill's Done checklist (copied
     from the SKILL.md so a human can audit the loop), and on DONE marks the
     checkbox in TASKS.md.
  5. Loop to the next unchecked task. Strictly serial.

Usage:
    python3 hermes/run_tasks.py            # run all pending tasks
    python3 hermes/run_tasks.py --one      # run only the next pending task
"""

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "hermes" / "state"
TASKS = STATE / "TASKS.md"
RESULTS = STATE / "RESULTS.md"
SKILLS_DIR = REPO / "hermes" / "skills"

PROFILE_CMD = ["debrief"]  # wrapper for: hermes -p debrief

TASK_RE = re.compile(r"^- \[( |x)\] ([a-z0-9-]+):\s*(.+?)\s*$")


def read_tasks():
    lines = TASKS.read_text().splitlines()
    tasks = []
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m:
            tasks.append(
                {"index": i, "done": m.group(1) == "x", "skill": m.group(2),
                 "instruction": m.group(3)}
            )
    return lines, tasks


def skill_checklist(skill):
    """Pull the '## Done checklist' block out of a skill's SKILL.md."""
    path = SKILLS_DIR / skill / "SKILL.md"
    if not path.exists():
        return "(no SKILL.md found for this skill)"
    grab, out = False, []
    for line in path.read_text().splitlines():
        if line.strip().lower().startswith("## done checklist"):
            grab = True
            continue
        if grab and line.startswith("## "):
            break
        if grab:
            out.append(line)
    return "\n".join(out).strip() or "(no checklist in SKILL.md)"


def build_prompt(instruction, skill):
    return (
        f"{instruction}\n\n"
        f"Use the '{skill}' skill and its steps. Use only the read-only vault "
        f"files. When you are finished, end your reply with a single status "
        f"line on its own: 'STATUS: DONE' if you completed every step, or "
        f"'STATUS: BLOCKED: <reason>' if a needed file was missing."
    )


def run_task(task):
    prompt = build_prompt(task["instruction"], task["skill"])
    cmd = PROFILE_CMD + ["chat", "-q", prompt, "-Q", "--yolo",
                         "-s", task["skill"]]
    print(f"  running skill={task['skill']} :: {task['instruction'][:60]}...",
          flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    answer = proc.stdout.strip()
    # Fresh Hermes quiet mode sometimes routes the answer to stderr; fall back.
    if not answer:
        answer = proc.stderr.strip()
    status = "BLOCKED: no output from agent"
    for line in answer.splitlines():
        s = line.strip()
        if s.upper().startswith("STATUS:"):
            status = s.split(":", 1)[1].strip()
    return answer, status


def append_result(task, answer, status):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = [
        f"\n## {task['skill']} :: {task['instruction']}",
        f"- run at: {stamp}",
        f"- status: {status}",
        "",
        "### Agent answer",
        "```",
        answer,
        "```",
        "",
        "### Skill done-checklist (for human audit)",
        "```",
        skill_checklist(task["skill"]),
        "```",
        "",
        "---",
    ]
    with RESULTS.open("a") as f:
        f.write("\n".join(block) + "\n")


def mark_done(lines, index):
    lines[index] = lines[index].replace("- [ ]", "- [x]", 1)
    TASKS.write_text("\n".join(lines) + "\n")


def main():
    only_one = "--one" in sys.argv
    if not RESULTS.exists():
        RESULTS.write_text(
            f"# Debrief task results\n\nStarted {datetime.now():%Y-%m-%d %H:%M:%S}\n\n---\n"
        )
    while True:
        lines, tasks = read_tasks()
        pending = [t for t in tasks if not t["done"]]
        if not pending:
            print("All tasks complete.")
            break
        task = pending[0]
        print(f"[task] {task['skill']}: {task['instruction']}")
        answer, status = run_task(task)
        append_result(task, answer, status)
        if status.upper().startswith("DONE"):
            mark_done(lines, task["index"])
            print(f"  -> DONE, checkbox marked. ({len(pending)-1} left)")
        else:
            print(f"  -> {status}. Leaving unchecked and stopping.")
            break
        if only_one:
            break


if __name__ == "__main__":
    main()
