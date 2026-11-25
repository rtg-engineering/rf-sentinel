#!/usr/bin/env python3
"""Evaluate an Analyst provider on every held-out scenario in tests/fixtures/analyst/.

  python3 scripts/evaluate_analyst.py                                  # deterministic
  python3 scripts/evaluate_analyst.py --provider pydantic-ai --model openai-codex:<model> --runs 2

Writes .demo/analyst/eval-<UTC>/ with one bundle per scenario/run and results.json. Exit status is
nonzero unless every scenario passes in every run. Truth files are read by the evaluator only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rf_platform.analyst.evaluate import evaluate  # noqa: E402
from rf_platform.analyst.evidence import EvidenceStore, Snapshot  # noqa: E402
from rf_platform.analyst.investigate import run_investigation  # noqa: E402
from rf_platform.analyst.report import write_bundle  # noqa: E402

FIXTURES = ROOT / "tests/fixtures/analyst"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=["deterministic", "pydantic-ai"], default="deterministic")
    parser.add_argument("--model", help="Pydantic AI model string")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--scenario", action="append", help="Limit to named scenario(s)")
    args = parser.parse_args()
    scenarios = sorted(p.name for p in FIXTURES.iterdir() if (p / "truth.json").exists())
    scenarios = [s for s in scenarios if not args.scenario or s in args.scenario]
    out = ROOT / ".demo/analyst" / f"eval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    rows = []
    for scenario in scenarios:
        truth = json.loads((FIXTURES / scenario / "truth.json").read_text())
        for run in range(1, args.runs + 1):
            store = EvidenceStore([Snapshot.from_dict(json.loads((FIXTURES / scenario / f"{n}.json").read_text()))
                                   for n in ("baseline", "current")])
            provider = None
            if args.provider == "pydantic-ai":
                from rf_platform.analyst.llm import DEFAULT_MODEL, PydanticAIAnalyst
                provider = PydanticAIAnalyst(args.model or DEFAULT_MODEL)
            inv = run_investigation(store, provider)
            write_bundle(out / scenario / f"run-{run}", inv, store)
            result = evaluate(inv, truth)
            info = inv["provider"]
            row = {"scenario": scenario, "run": run, "passed": result["passed"],
                   "failed_checks": [k for k, v in result["checks"].items() if not v],
                   "authored_by": "deterministic-fallback" if info.get("fallback") else info["name"],
                   "provider_error": info.get("error"), "tool_calls": result["tool_calls"],
                   "refused_tool_calls": result["refused_tool_calls"],
                   "citations": inv["grounding"]["citations_checked"], "warnings": len(inv["grounding"]["warnings"]),
                   "findings": [f["category"] for f in inv["findings"]],
                   "requests": info.get("requests"), "input_tokens": info.get("input_tokens"),
                   "output_tokens": info.get("output_tokens")}
            rows.append(row)
            print(f"{'PASS' if row['passed'] else 'FAIL'}  {scenario:<20} run {run}  {row['authored_by']:<22} "
                  f"tools {row['tool_calls']:>2}  citations {row['citations']:>3}  "
                  f"{'failed: ' + ', '.join(row['failed_checks']) if row['failed_checks'] else ''}", flush=True)
    model_rows = [r for r in rows if r["authored_by"] != "deterministic-fallback"]
    summary = {"provider": args.provider, "model": args.model, "runs": args.runs, "scenarios": scenarios,
               "passed": all(r["passed"] for r in rows),
               "model_authored_runs": len(model_rows), "total_runs": len(rows), "rows": rows}
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"{'PASS' if summary['passed'] else 'FAIL'}: {sum(r['passed'] for r in rows)}/{len(rows)} runs; "
          f"{len(model_rows)}/{len(rows)} authored by the provider → {out / 'results.json'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
