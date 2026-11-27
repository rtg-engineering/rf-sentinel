#!/usr/bin/env python3
"""Score a workflow planner against the reviewed phrase corpus (tests/fixtures/workflow_phrases.json).

  python3 scripts/evaluate_planner.py                                  # rules planner
  python3 scripts/evaluate_planner.py --model openai-codex:gpt-6-astra  # model planner (no fallback counted as a pass)
  python3 scripts/evaluate_planner.py --model anthropic:claude-opus-5-5 --json-out .demo/planner-eval.json
  python3 scripts/evaluate_planner.py --playbooks [--model ...]         # multi-step corpus (playbook_phrases.json)

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
TEXT_OPS = {"eq": "contains", "ne": "not_contains"}


def equivalent(value):
    """Normalise text conditions so exact-vs-contains on the same value and capitalisation count as equal."""
    if isinstance(value, list) and len(value) == 3 and isinstance(value[2], str):
        return [value[0], TEXT_OPS.get(value[1], value[1]), value[2].lower()]
    if isinstance(value, list):
        return sorted((equivalent(v) for v in value), key=json.dumps)
    if isinstance(value, dict):
        return {k: equivalent(v) for k, v in value.items()}
    return value


STEP_PARTS = ("focus", "focus_step", "gate", "gate_step", "gate_conditions", "collect", "duration_s", "channel",
              "filters", "triggers")


def evaluate_playbooks(args) -> int:
    from rf_platform.agent.playbook import PlaybookSpec, plan_any, plan_playbook_deterministic
    from tests.test_agent_playbook import PLAYBOOKS, schedule_shape, step_shape
    rows, totals = [], {"exact": 0, "equivalent": 0, "steps_exact": 0, "steps": 0, "refusals_ok": 0, "fallbacks": 0, "errors": 0}
    totals |= {p: 0 for p in STEP_PARTS}
    planned_cases = [c for c in PLAYBOOKS if not c.get("refused")]
    for case in PLAYBOOKS:
        started = time.perf_counter()
        row = {"text": case["text"]}
        try:
            if args.model:
                result = plan_any(case["text"], "model", model=args.model, utc_offset_min=0)
                row["planner"] = result["planner"]
                if result["planner"] != "model":
                    row["fallback_reason"] = result["fallback_reason"]
                    totals["fallbacks"] += 1
                book = PlaybookSpec.model_validate(result["playbook"])
            else:
                book = plan_playbook_deterministic(case["text"], utc_offset_min=0)
            got = [step_shape(book, i) for i in range(len(book.steps))]
            if case.get("refused"):
                row["ok"], row["problem"] = False, "should have been refused"
            else:
                want = case["steps"]
                schedule_ok = "repeat" not in case or schedule_shape(book) == (case["repeat"], case["start"])
                if "repeat" in case and not schedule_ok:
                    row["schedule"] = {"got": schedule_shape(book), "expected": (case["repeat"], case["start"])}
                row["ok"] = got == want and schedule_ok
                row["equivalent"] = (equivalent(got) == equivalent(want) and schedule_ok) if len(got) == len(want) else False
                totals["equivalent"] += row["equivalent"]
                totals["steps"] += len(want)
                for g, w in zip(got, want):
                    totals["steps_exact"] += g == w
                    for p in STEP_PARTS:
                        totals[p] += g[p] == w[p]
                if not row["ok"]:
                    row["got"], row["expected"] = got, want
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
    n, refusals = len(planned_cases), len(PLAYBOOKS) - len(planned_cases)
    summary = {"planner": args.model or "rules", "cases": len(PLAYBOOKS), "exact_playbooks": f"{totals['exact']}/{n}",
               "equivalent_playbooks": f"{totals['equivalent']}/{n}", "steps_exact": f"{totals['steps_exact']}/{totals['steps']}",
               "refusals": f"{totals['refusals_ok']}/{refusals}", "fallbacks": totals["fallbacks"], "errors": totals["errors"],
               "by_part_per_step": {p: f"{totals[p]}/{totals['steps']}" for p in STEP_PARTS}}
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="Pydantic AI model string; omit for the rules planner")
    parser.add_argument("--playbooks", action="store_true", help="Score the multi-step (playbook) corpus instead")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.playbooks:
        return evaluate_playbooks(args)
    rows, totals = [], {p: 0 for p in PARTS} | {"exact": 0, "equivalent": 0, "refusals_ok": 0, "fallbacks": 0, "errors": 0}
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
                row["equivalent"] = all(equivalent(got[p]) == equivalent(case[p]) for p in PARTS)
                totals["equivalent"] += row["equivalent"]
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
    summary = {"planner": args.model or "rules", "cases": len(CORPUS), "exact_plans": f"{totals['exact']}/{n}", "equivalent_plans": f"{totals['equivalent']}/{n}",
               "refusals": f"{totals['refusals_ok']}/{refusals}", "fallbacks": totals["fallbacks"], "errors": totals["errors"],
               "by_part": {p: f"{totals[p]}/{n}" for p in PARTS}}
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
