#!/usr/bin/env python3
"""Score a workflow planner against the reviewed phrase corpus (tests/fixtures/workflow_phrases.json).

  python3 scripts/evaluate_planner.py                                  # rules planner
  python3 scripts/evaluate_planner.py --model openai-codex:gpt-6-astra  # model planner (no fallback counted as a pass)
  python3 scripts/evaluate_planner.py --model anthropic:claude-opus-5-5 --json-out .demo/planner-eval.json

Per phrase it compares sources, duration, receiver/channel, filters, and every rule (conditions, actions,
alert severity, repeat), and checks that transmit-type requests are refused. A model answer that fell back
to the rules planner is reported as a fallback, not as a model success.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rf_platform.agent.workflow import WorkflowSpec, plan, plan_deterministic  # noqa: E402
from tests.test_workflow_corpus import shape  # noqa: E402

CORPUS = json.loads((ROOT / "tests/fixtures/workflow_phrases.json").read_text())["cases"]
PARTS = ("collect", "duration_s", "device_id", "channel", "filters", "triggers")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="Pydantic AI model string; omit for the rules planner")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    rows, totals = [], {p: 0 for p in PARTS} | {"exact": 0, "refusals_ok": 0, "fallbacks": 0, "errors": 0}
    planned_cases = [c for c in CORPUS if not c.get("refused")]
    for case in CORPUS:
        started = time.perf_counter()
        row = {"text": case["text"]}
        try:
            if args.model:
                result = plan(case["text"], "model", model=args.model)
                row["planner"] = result["planner"]
                if result["planner"] != "model":
                    row["fallback_reason"] = result["fallback_reason"]
                    totals["fallbacks"] += 1
                spec = WorkflowSpec.model_validate(result["spec"])
            else:
                spec = plan_deterministic(case["text"])
            got = json.loads(json.dumps(shape(spec)))
            if case.get("refused"):
                row["ok"] = False
                row["problem"] = "should have been refused"
            else:
                row["parts"] = {p: got[p] == case[p] for p in PARTS}
                for p in PARTS:
                    totals[p] += row["parts"][p]
                row["ok"] = all(row["parts"].values())
                if not row["ok"]:
                    row["got"] = {p: got[p] for p in PARTS if not row["parts"][p]}
                    row["expected"] = {p: case[p] for p in PARTS if not row["parts"][p]}
        except ValueError as exc:
            row["ok"] = bool(case.get("refused"))
            totals["refusals_ok"] += row["ok"]
            if not row["ok"]:
                row["problem"] = f"refused: {exc}"
        except Exception as exc:
            row["ok"], row["problem"] = False, f"{type(exc).__name__}: {exc}"
            totals["errors"] += 1
        totals["exact"] += row["ok"] and not case.get("refused")
        row["seconds"] = round(time.perf_counter() - started, 2)
        rows.append(row)
        print(f"{'ok ' if row['ok'] else 'BAD'} {row['seconds']:>5.1f}s  {case['text'][:90]}"
              + (f"  [{row.get('planner')}]" if args.model else ""), flush=True)
    n, refusals = len(planned_cases), len(CORPUS) - len(planned_cases)
    summary = {"planner": args.model or "rules", "cases": len(CORPUS), "exact_plans": f"{totals['exact']}/{n}",
               "refusals": f"{totals['refusals_ok']}/{refusals}", "fallbacks": totals["fallbacks"], "errors": totals["errors"],
               "by_part": {p: f"{totals[p]}/{n}" for p in PARTS}}
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
