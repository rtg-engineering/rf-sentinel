"""CLI: capture snapshots from a running backend and run a read-only investigation.

  python -m rf_platform.analyst capture --base-url http://127.0.0.1:8080 --label baseline --out baseline.json
  python -m rf_platform.analyst investigate baseline.json current.json --out .demo/analyst/run

Persistent memory (append-only SQLite; --db defaults to .demo/analyst/memory.sqlite):
  python -m rf_platform.analyst memory baseline baseline.json --reason "Site survey, week 1"
  python -m rf_platform.analyst memory assess current.json --against last --out .demo/analyst/run
  python -m rf_platform.analyst memory history | timeline <entity_key> | verify <seq> | audit
  python -m rf_platform.analyst memory annotate --entity <key> --kind known_device --text "Facilities beacon"
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
    mem = sub.add_parser("memory", help="Persistent, append-only environment memory")
    mem.add_argument("--db", type=Path, default=Path(".demo/analyst/memory.sqlite"))
    msub = mem.add_subparsers(dest="memory_command", required=True)
    base = msub.add_parser("baseline", help="Record a new baseline version from a snapshot file")
    base.add_argument("snapshot", type=Path)
    base.add_argument("--name", default="default")
    base.add_argument("--reason", default="Baseline set")
    base.add_argument("--author", default="operator")
    asm = msub.add_parser("assess", help="Investigate a snapshot against the last assessment or the baseline")
    asm.add_argument("snapshot", type=Path)
    asm.add_argument("--against", choices=["last", "baseline"], default="last")
    asm.add_argument("--name", default="default", help="Baseline name")
    asm.add_argument("--out", type=Path, help="Also write an evidence bundle here")
    asm.add_argument("--provenance", default="synthetic_replay", choices=["synthetic_replay", "controlled_test", "live"])
    msub.add_parser("history", help="List baselines and assessments")
    tl = msub.add_parser("timeline", help="One entity across every stored snapshot")
    tl.add_argument("entity_key")
    ver = msub.add_parser("verify", help="Recompute a stored assessment from stored snapshots")
    ver.add_argument("seq", type=int)
    msub.add_parser("audit", help="Every stored record in order")
    ann = msub.add_parser("annotate", help="Record an operator review; never changes evidence")
    ann.add_argument("--kind", required=True, choices=["confirm", "dispute", "note", "known_device", "unknown_device"])
    ann.add_argument("--text", required=True)
    ann.add_argument("--entity")
    ann.add_argument("--investigation")
    ann.add_argument("--finding")
    ann.add_argument("--author", default="operator")
    args = parser.parse_args(argv)

    if args.command == "memory":
        return memory_command(args)

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


def memory_command(args) -> int:
    from .memory import EnvironmentMemory, MemoryStoreError, assess
    memory = EnvironmentMemory(args.db)

    def show(value):
        print(json.dumps(value, indent=2, sort_keys=True, default=str))

    try:
        if args.memory_command == "baseline":
            show(memory.set_baseline(Snapshot.from_dict(json.loads(args.snapshot.read_text())), args.name,
                                     args.author, args.reason))
        elif args.memory_command == "assess":
            current = Snapshot.from_dict(json.loads(args.snapshot.read_text()))
            inv = assess(memory, current, args.against, args.name, provenance=args.provenance)
            if args.out:
                scope = inv["scope"]
                store = EvidenceStore([memory.snapshot(scope["reference_snapshot_id"]), memory.snapshot(scope["current_snapshot_id"])])
                store.computed.update(inv["computed_results"])
                write_bundle(args.out, inv, store)
            print(f"Assessment {inv['memory']['assessment_seq']}: {inv['investigation_id']} compared against the "
                  f"{inv['memory']['compared_against']}; {len(inv['findings'])} findings; grounding "
                  f"{'PASS' if inv['grounding']['passed'] else 'FAIL'}")
            return 0 if inv["grounding"]["passed"] else 1
        elif args.memory_command == "history":
            show({"baselines": memory.baseline_history(), "assessments": memory.assessments()})
        elif args.memory_command == "timeline":
            show(memory.entity_timeline(args.entity_key))
        elif args.memory_command == "verify":
            result = memory.verify_assessment(args.seq)
            show(result)
            return 0 if result["reproduced"] else 1
        elif args.memory_command == "audit":
            show(memory.audit_log())
        elif args.memory_command == "annotate":
            show(memory.annotate(args.kind, args.text, args.author, args.entity, args.investigation, args.finding))
    except MemoryStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
