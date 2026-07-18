#!/usr/bin/env python3
"""Jargon-torture eval for the clinical glossary correction pass.

Runs each realistically STT-mangled sentence in cases.json through
debrief.stt.correct_transcript() live against the local Gemma model, then scores
term presence/absence in the corrected output. Positive cases check that a
mangled clinical term is recovered; negative cases check that an ordinary word
is NOT hallucinated into jargon (SUDs the scale vs suds the dish soap).

Matching is CASE-SENSITIVE on purpose: "SUDs" present vs "suds" preserved is the
whole point of the negative cases.

Run:  .venv/bin/python eval/jargon/run_jargon_eval.py

Exit code is 0 only if the pass rate is >= the threshold in cases.json (0.85).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make the repo root importable when run as a script.
EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from debrief import stt, vault  # noqa: E402

CASES_PATH = EVAL_DIR / "cases.json"
RESULTS_JSON = EVAL_DIR / "results.json"


def _client_ctx_for(case: dict) -> dict | None:
    client_id = case.get("client_id")
    if not client_id:
        return None
    try:
        return vault.client_context(client_id)
    except Exception:
        return None


def _score_case(case: dict, corrected: str) -> dict:
    """Case-sensitive substring scoring. Returns per-check detail and pass bool."""
    checks: list[dict] = []

    for term in case.get("present", []):
        ok = term in corrected
        checks.append({"kind": "present", "term": term, "pass": ok})

    present_any = case.get("present_any")
    if present_any:
        hit = [t for t in present_any if t in corrected]
        checks.append(
            {"kind": "present_any", "term": " | ".join(present_any),
             "pass": bool(hit), "matched": hit}
        )

    for term in case.get("absent", []):
        ok = term not in corrected
        checks.append({"kind": "absent", "term": term, "pass": ok})

    passed = all(c["pass"] for c in checks) if checks else False
    return {"passed": passed, "checks": checks}


def run() -> int:
    spec = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    threshold = spec.get("_meta", {}).get("pass_threshold", 0.85)
    cases = spec["cases"]

    rows: list[dict] = []
    for case in cases:
        cid = case["id"]
        framework = case.get("framework")
        client_ctx = _client_ctx_for(case)
        print(f"[jargon] correcting {cid} ...", flush=True)
        t0 = time.perf_counter()
        try:
            corrected = stt.correct_transcript(case["mangled"], client_ctx, framework)
            err = None
        except Exception as exc:  # noqa: BLE001
            corrected = ""
            err = str(exc)
        latency = round(time.perf_counter() - t0, 2)

        scored = _score_case(case, corrected)
        rows.append({
            "id": cid,
            "framework": framework,
            "negative": bool(case.get("absent")),
            "mangled": case["mangled"],
            "corrected": corrected,
            "checks": scored["checks"],
            "passed": scored["passed"] and err is None,
            "error": err,
            "latency_s": latency,
        })
        mark = "PASS" if rows[-1]["passed"] else "FAIL"
        print(f"[jargon]   {mark}  ({latency}s)", flush=True)

    passed = sum(1 for r in rows if r["passed"])
    total = len(rows)
    rate = passed / total if total else 0.0

    _print_table(rows, passed, total, rate, threshold)

    neg_rows = [r for r in rows if r["negative"]]
    neg_passed = sum(1 for r in neg_rows if r["passed"])

    RESULTS_JSON.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": "gemma-4-12b-it-qat (LM Studio localhost:1234)",
        "threshold": threshold,
        "passed": passed,
        "total": total,
        "pass_rate": round(rate, 4),
        "negative_passed": neg_passed,
        "negative_total": len(neg_rows),
        "overall_pass": rate >= threshold,
        "rows": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[jargon] wrote {RESULTS_JSON}", flush=True)
    overall = rate >= threshold
    print(f"[jargon] OVERALL: {'PASS' if overall else 'FAIL'} "
          f"({passed}/{total} = {rate:.0%}, threshold {threshold:.0%})", flush=True)
    return 0 if overall else 1


def _print_table(rows, passed, total, rate, threshold) -> None:
    id_w = max(len(r["id"]) for r in rows)
    fw_w = max(len(str(r["framework"] or "-")) for r in rows)
    print()
    print(f"{'ID':<{id_w}}  {'FW':<{fw_w}}  {'KIND':<4}  {'RESULT':<6}  DETAIL")
    print("-" * (id_w + fw_w + 60))
    for r in rows:
        kind = "NEG" if r["negative"] else "POS"
        result = "PASS" if r["passed"] else "FAIL"
        if r["error"]:
            detail = f"ERROR: {r['error']}"
        else:
            fails = [
                f"{c['kind']}:{c['term']!r}"
                for c in r["checks"] if not c["pass"]
            ]
            detail = "ok" if not fails else "MISS " + ", ".join(fails)
        print(f"{r['id']:<{id_w}}  {str(r['framework'] or '-'):<{fw_w}}  "
              f"{kind:<4}  {result:<6}  {detail}")
    print("-" * (id_w + fw_w + 60))
    neg = [r for r in rows if r["negative"]]
    neg_pass = sum(1 for r in neg if r["passed"])
    print(f"Overall: {passed}/{total} = {rate:.0%} (threshold {threshold:.0%})  |  "
          f"negatives: {neg_pass}/{len(neg)}")
    print()


if __name__ == "__main__":
    raise SystemExit(run())
