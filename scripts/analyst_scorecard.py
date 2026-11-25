#!/usr/bin/env python3
"""Measure the Analyst on every held-out scenario and publish a scorecard.

  python3 scripts/analyst_scorecard.py                                   # deterministic only
  python3 scripts/analyst_scorecard.py --model openai-codex:<model> --runs 3

Every number on the card is computed from captured investigation runs. Writes, under --output
(default .demo/analyst/scorecard/): scorecard.json, scorecard.html, scorecard.png (rendered by the
validation image when Docker is available), and one evidence bundle per scenario run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rf_platform.analyst.evaluate import evaluate  # noqa: E402
from rf_platform.analyst.evidence import EvidenceStore, Snapshot  # noqa: E402
from rf_platform.analyst.investigate import run_investigation  # noqa: E402
from rf_platform.analyst.report import write_bundle  # noqa: E402

FIXTURES = ROOT / "tests/fixtures/analyst"
LIMITATIONS = ("Held-out synthetic scenarios generated from the demo backend. They measure investigation logic and "
               "grounding on known changes, not live RF detection accuracy or performance in an uncontrolled environment. "
               "The scenario set is small; results show behavior on these cases, not a general accuracy rate.")


def git_version() -> str:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", ".", ":(exclude)docs/*.md"],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return commit + ("-dirty" if dirty else "")


def run_provider(label: str, make_provider, runs: int, out: Path) -> dict:
    rows = []
    for scenario in sorted(p.name for p in FIXTURES.iterdir() if (p / "truth.json").exists()):
        truth = json.loads((FIXTURES / scenario / "truth.json").read_text())
        for run in range(1, runs + 1):
            store = EvidenceStore([Snapshot.from_dict(json.loads((FIXTURES / scenario / f"{n}.json").read_text()))
                                   for n in ("baseline", "current")])
            inv = run_investigation(store, make_provider())
            write_bundle(out / "bundles" / label / scenario / f"run-{run}", inv, store)
            result = evaluate(inv, truth)
            checks = result["checks"]
            info = inv["provider"]
            rows.append({
                "scenario": scenario, "run": run, "passed": result["passed"],
                "expected_total": sum(k.startswith("expected_") for k in checks),
                "expected_found": sum(v for k, v in checks.items() if k.startswith("expected_")),
                "false_positive_checks_failed": sum(not v for k, v in checks.items() if k.startswith("no_")),
                "findings": len(inv["findings"]),
                "unsupported_findings": sum(not f["grounded"] for f in inv["findings"]),
                "grounding_passed": inv["grounding"]["passed"],
                "citations": inv["grounding"]["citations_checked"],
                "actions_executed": len(inv["executed_actions"]),
                "refused_tool_calls": result["refused_tool_calls"],
                "tool_calls": result["tool_calls"],
                "authored_by_provider": not info.get("fallback"),
                "categories": sorted(f["category"] for f in inv["findings"]),
                "input_tokens": info.get("input_tokens"), "output_tokens": info.get("output_tokens"),
                "requests": info.get("requests"), "failed_checks": [k for k, v in checks.items() if not v],
            })
            print(f"{label:<14} {scenario:<20} run {run}: {'PASS' if result['passed'] else 'FAIL'}"
                  f"{'' if rows[-1]['authored_by_provider'] else ' (fallback)'}", flush=True)
    return summarize(rows)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    by_scenario: dict[str, list[dict]] = {}
    for r in rows:
        by_scenario.setdefault(r["scenario"], []).append(r)
    tokens = [r["input_tokens"] + r["output_tokens"] for r in rows if r["input_tokens"] is not None]
    return {
        "runs": n, "scenarios": sorted(by_scenario),
        "scenario_runs_passed": sum(r["passed"] for r in rows),
        "expected_changes": sum(r["expected_total"] for r in rows),
        "expected_changes_found": sum(r["expected_found"] for r in rows),
        "false_positive_checks_failed": sum(r["false_positive_checks_failed"] for r in rows),
        "findings": sum(r["findings"] for r in rows),
        "unsupported_findings": sum(r["unsupported_findings"] for r in rows),
        "runs_fully_grounded": sum(r["grounding_passed"] for r in rows),
        "citations_checked": sum(r["citations"] for r in rows),
        "actions_executed": sum(r["actions_executed"] for r in rows),
        "refused_tool_calls": sum(r["refused_tool_calls"] for r in rows),
        "runs_authored_by_provider": sum(r["authored_by_provider"] for r in rows),
        "same_finding_categories_every_run": all(len({tuple(r["categories"]) for r in rs}) == 1 for rs in by_scenario.values()),
        "mean_tokens_per_run": round(sum(tokens) / len(tokens)) if tokens else None,
        "rows": rows,
    }


def card(score: dict) -> str:
    def cell(v):
        return html.escape("—" if v is None else ("Yes" if v is True else "No" if v is False else str(v)))
    cols = list(score["providers"])
    rows = [
        ("Scenario runs passed", lambda s: f"{s['scenario_runs_passed']} / {s['runs']}"),
        ("Expected changes found", lambda s: f"{s['expected_changes_found']} / {s['expected_changes']}"),
        ("False-positive checks failed", lambda s: s["false_positive_checks_failed"]),
        ("Unsupported findings", lambda s: f"{s['unsupported_findings']} / {s['findings']}"),
        ("Citations re-checked by code", lambda s: s["citations_checked"]),
        ("Actions executed", lambda s: s["actions_executed"]),
        ("Written by provider (no fallback)", lambda s: f"{s['runs_authored_by_provider']} / {s['runs']}"),
        ("Same findings every run", lambda s: s["same_finding_categories_every_run"]),
        ("Mean tokens per run", lambda s: s["mean_tokens_per_run"]),
    ]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(f"<tr><td>{html.escape(label)}</td>" + "".join(f"<td>{cell(fn(score['providers'][c]))}</td>" for c in cols) + "</tr>"
                   for label, fn in rows)
    return f"""<!doctype html><meta charset="utf-8"><title>RF Sentinel Analyst scorecard</title>
<style>body{{margin:0;background:#10202d;color:#edf5f9;font:19px system-ui}}main{{padding:44px;width:1040px}}
h1{{font-size:38px;margin:6px 0 18px}}small{{color:#79e6c4}}table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #2b4a5e}}th{{color:#aabfcd;font-weight:600}}
td:first-child{{color:#aabfcd}}p{{font-size:15px;line-height:1.55;color:#aabfcd}}</style>
<main><small>SYNTHETIC HELD-OUT SCENARIOS · {html.escape(', '.join(score['scenarios']))}</small>
<h1>RF Sentinel Analyst scorecard</h1><table><tr><th></th>{head}</tr>{body}</table>
<p>Commit {html.escape(score['application_version'])} · {html.escape(score['generated_at'][:10])}. {html.escape(score['limitations'])}</p></main>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="Also score the Pydantic AI provider with this model string")
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario for the model provider")
    parser.add_argument("--output", type=Path, default=ROOT / ".demo/analyst/scorecard")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    from rf_platform.analyst.deterministic import DeterministicAnalyst
    providers = {"Deterministic": run_provider("deterministic", DeterministicAnalyst, 1, out)}
    if args.model:
        from rf_platform.analyst.llm import PydanticAIAnalyst
        providers[f"LLM ({args.model.split(':', 1)[-1]})"] = run_provider(
            "llm", lambda: PydanticAIAnalyst(args.model), args.runs, out)
    score = {"schema_version": "1.0", "application_version": git_version(),
             "generated_at": datetime.now(timezone.utc).isoformat(), "synthetic": True,
             "scenarios": next(iter(providers.values()))["scenarios"], "providers": providers,
             "limitations": LIMITATIONS}
    (out / "scorecard.json").write_text(json.dumps(score, indent=2, sort_keys=True) + "\n")
    (out / "scorecard.html").write_text(card(score))
    rendered = subprocess.run(["docker", "run", "--rm", "--user", f"{subprocess.os.getuid()}:{subprocess.os.getgid()}",
                               "-e", "HOME=/tmp", "-v", f"{out}:/results", "rf-sentinel:validation",
                               "scripts/validation_browser.py", "card", "/results/scorecard.html", "/results/scorecard.png"],
                              capture_output=True, text=True)
    print(f"Card PNG: {'written' if rendered.returncode == 0 else 'not rendered (' + rendered.stderr.strip()[-200:] + ')'}")
    ok = all(p["scenario_runs_passed"] == p["runs"] for p in providers.values())
    print(f"{'PASS' if ok else 'FAIL'}: {out / 'scorecard.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
