#!/usr/bin/env python3
"""Measure "ask about a run" against a saved run: how many model answers pass the citation check.

  python3 scripts/evaluate_ask.py <run dir or run.json> [--model openai-codex:gpt-6-astra] [--json-out FILE]

For each question in QUESTIONS it asks the model (its answer is checked mechanically: cited devices exist, and every
number is cited to a tool-call field that holds it) and records whether the answer was accepted, the rejection
reason otherwise, and where each number came from. Accepted is not the same as correct: a reviewer should read the
answers beside their sources. The output can contain device identifiers from the run; keep it with the run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUESTIONS = [
    "what alerted?",
    "which trackers came back every cycle?",
    "how many devices were decoded in every cycle?",
    "what was the strongest device?",
    "how many devices were new?",
    "how many trackers were there?",
    "summarize the run",
    "did any step fail or get skipped?",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run")
    parser.add_argument("--model", default=None, help="Pydantic AI model string (default: the planner model)")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    from rf_platform.agent.ask import ask
    from rf_platform.agent.workflow import default_planner_model
    path = Path(args.run)
    result = json.loads((path / "run.json" if path.is_dir() else path).read_text())
    model = args.model or default_planner_model()
    if model is None:
        parser.error("no model: pass --model or configure RF_SENTINEL_PLANNER_MODEL / a login")
    rows = []
    for question in QUESTIONS:
        started = time.time()
        out = ask(result, question, "model", model=model)
        rows.append({"question": question, "accepted": out["by"] == "model", "answer": out["answer"],
                     "sources": out.get("sources") or [], "rejected_because": out.get("fallback_reason"),
                     "seconds": round(time.time() - started, 1)})
        mark = "accepted" if rows[-1]["accepted"] else f"rejected: {out.get('fallback_reason')}"
        print(f"- {question}  [{mark}]")
        for line in str(out["answer"]).splitlines()[:6]:
            print(f"    {line}")
        for src in rows[-1]["sources"]:
            print(f"      {src}")
    summary = {"run_id": result.get("run_id"), "simulated": bool(result.get("simulated")),
               "model": model if isinstance(model, str) else type(model).__name__,
               "accepted": f"{sum(r['accepted'] for r in rows)}/{len(rows)}"}
    print(json.dumps(summary))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
