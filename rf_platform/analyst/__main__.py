"""CLI: capture snapshots from a running backend and run a read-only investigation.

  python -m rf_platform.analyst capture --base-url http://127.0.0.1:8080 --label baseline --out baseline.json
  python -m rf_platform.analyst investigate baseline.json current.json --out .demo/analyst/run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .evidence import EvidenceStore, Snapshot, capture_status
from .investigate import run_investigation
from .report import write_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rf_platform.analyst", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="Save a trimmed, truth-stripped /api/status snapshot")
    cap.add_argument("--base-url", default="http://127.0.0.1:8080")
    cap.add_argument("--label", required=True)
    cap.add_argument("--out", type=Path, required=True)
    inv = sub.add_parser("investigate", help="Investigate snapshots (first = reference, last = current)")
    inv.add_argument("snapshots", nargs="+", type=Path)
    inv.add_argument("--out", type=Path, required=True, help="Bundle directory")
    inv.add_argument("--question")
    inv.add_argument("--provenance", default="synthetic_replay", choices=["synthetic_replay", "controlled_test", "live"])
    inv.add_argument("--max-tool-calls", type=int, default=16)
    inv.add_argument("--provider", choices=["deterministic", "pydantic-ai"], default="deterministic",
                     help="deterministic needs no model; pydantic-ai needs the analyst-llm extra and model credentials")
    inv.add_argument("--model", default=None,
                     help="Pydantic AI model string for --provider pydantic-ai (default anthropic:claude-opus-5-5; e.g. ollama:<name>)")
    args = parser.parse_args(argv)

    if args.command == "capture":
        snapshot = Snapshot.from_status(capture_status(args.base_url), args.label)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(snapshot.to_dict(), indent=1, sort_keys=True) + "\n")
        print(snapshot.snapshot_id)
        return 0

    store = EvidenceStore([Snapshot.from_dict(json.loads(p.read_text())) for p in args.snapshots])
    provider = None
    if args.provider == "pydantic-ai":
        from .llm import DEFAULT_MODEL, PydanticAIAnalyst
        provider = PydanticAIAnalyst(args.model or DEFAULT_MODEL, max_requests=args.max_tool_calls)
    investigation = run_investigation(store, provider, question=args.question, provenance=args.provenance,
                                      max_calls=args.max_tool_calls)
    write_bundle(args.out, investigation, store)
    g = investigation["grounding"]
    used = investigation["provider"]
    if used.get("fallback"):
        print(f"Provider {used['name']} failed ({used['error']}); used deterministic analysis instead.", file=sys.stderr)
    print(f"{investigation['investigation_id']}: {len(investigation['findings'])} findings, "
          f"grounding {'PASS' if g['passed'] else 'FAIL'} ({g['citations_checked']} citations) → {args.out / 'report.html'}")
    return 0 if g["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
