"""CLI: capture snapshots from a running backend and run a read-only investigation.

  python -m rf_platform.analyst capture --base-url http://127.0.0.1:8080 --label baseline --out baseline.json
  python -m rf_platform.analyst investigate baseline.json current.json --out .demo/analyst/run

Persistent memory (append-only SQLite; --db defaults to .demo/analyst/memory.sqlite):
  python -m rf_platform.analyst memory baseline baseline.json --reason "Site survey, week 1"
  python -m rf_platform.analyst memory assess current.json --against last --out .demo/analyst/run
  python -m rf_platform.analyst memory history | timeline <entity_key> | verify <seq> | audit
  python -m rf_platform.analyst memory annotate --entity <key> --kind known_device --text "Facilities beacon"

Bounded missions (same --db; restricted actions need --operator approval; execution is simulated):
  python -m rf_platform.analyst mission create --name "Lab watch" --objective "..." --allow start_passive_capture
  python -m rf_platform.analyst mission submit <id>; ... approve-scope <id> --operator ops-lead
  python -m rf_platform.analyst mission start <id> --operator ops-lead --base-url http://127.0.0.1:8080
  python -m rf_platform.analyst mission run <id> --base-url http://127.0.0.1:8080    # ticks until finished
  python -m rf_platform.analyst mission approve <id> <proposal_id> --operator ops-lead
  python -m rf_platform.analyst mission status <id> | pause | resume | cancel | deny | list
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
    mis = sub.add_parser("mission", help="Bounded investigation missions")
    mis.add_argument("--db", type=Path, default=Path(".demo/analyst/memory.sqlite"))
    xsub = mis.add_subparsers(dest="mission_command", required=True)
    cr = xsub.add_parser("create")
    cr.add_argument("--name", required=True)
    cr.add_argument("--objective", required=True)
    cr.add_argument("--allow", action="append", default=[], help="Restricted action the scope permits (repeatable)")
    cr.add_argument("--sensors", default="all", help='"all" or comma-separated sensor IDs')
    cr.add_argument("--interval", type=float, default=300, help="Seconds between assessments")
    cr.add_argument("--duration", type=float, default=86400, help="Mission duration in seconds")
    cr.add_argument("--max-assessments", type=int, default=48)
    cr.add_argument("--max-actions", type=int, default=3)
    cr.add_argument("--max-action-duration", type=float, default=600)
    cr.add_argument("--approval-ttl", type=float, default=3600)
    cr.add_argument("--operator", default="operator")
    for name in ("submit", "approve-scope", "reject-scope", "start", "tick", "run", "pause", "resume", "cancel", "status"):
        c = xsub.add_parser(name)
        c.add_argument("mission_id")
        c.add_argument("--operator", default="operator")
        c.add_argument("--reason", default="")
        if name in ("start", "tick", "run"):
            c.add_argument("--snapshot", type=Path, help="Snapshot file (default: capture from --base-url)")
            c.add_argument("--base-url", default="http://127.0.0.1:8080")
        if name == "run":
            c.add_argument("--poll", type=float, default=5.0, help="Seconds between ticks")
            c.add_argument("--max-ticks", type=int, default=0, help="Stop after this many ticks (0 = until finished)")
    for name in ("approve", "deny"):
        c = xsub.add_parser(name)
        c.add_argument("mission_id")
        c.add_argument("proposal_id")
        c.add_argument("--operator", required=True, help="Named operator; required for every approval decision")
        c.add_argument("--reason", default="")
    xsub.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "mission":
        return mission_command(args)
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


def mission_command(args) -> int:
    import time
    from .memory import EnvironmentMemory
    from .missions import TERMINAL, MissionError, Missions
    missions = Missions(EnvironmentMemory(args.db))

    def show(value):
        print(json.dumps(value, indent=2, sort_keys=True, default=str))

    def capture():
        if getattr(args, "snapshot", None):
            return Snapshot.from_dict(json.loads(args.snapshot.read_text()))
        return Snapshot.from_status(capture_status(args.base_url), "mission")

    cmd = args.mission_command
    try:
        if cmd == "create":
            scope = {"allowed_actions": args.allow, "assess_interval_s": args.interval, "duration_s": args.duration,
                     "max_assessments": args.max_assessments, "max_actions": args.max_actions,
                     "max_action_duration_s": args.max_action_duration, "approval_ttl_s": args.approval_ttl,
                     "sensors": "all" if args.sensors == "all" else [s for s in args.sensors.split(",") if s]}
            print(missions.create(args.name, args.objective, scope, args.operator))
        elif cmd == "list":
            show([{**m, "state": missions.state(m["mission_id"])["state"]} for m in missions.memory.missions()])
        elif cmd == "status":
            report = missions.report(args.mission_id)
            show({k: report[k] for k in ("mission_id", "state", "assessments", "actions_executed", "pending")}
                 | {"last_events": [{"type": e["type"], "actor": e["actor"]} for e in report["events"][-8:]]})
        elif cmd == "submit":
            missions.submit(args.mission_id, args.operator)
        elif cmd == "approve-scope":
            missions.approve_scope(args.mission_id, args.operator)
        elif cmd == "reject-scope":
            missions.reject_scope(args.mission_id, args.operator, args.reason)
        elif cmd == "start":
            missions.start(args.mission_id, capture(), args.operator)
        elif cmd == "tick":
            print(missions.tick(args.mission_id, capture))
        elif cmd == "run":
            ticks = 0
            while True:
                state = missions.tick(args.mission_id, capture)
                ticks += 1
                pending = missions.state(args.mission_id)["pending"]
                print(f"{time.strftime('%H:%M:%S')} {state}"
                      + (f"  pending {pending['proposal_id']}: {pending['action']} {pending['params']}" if pending else ""),
                      flush=True)
                if state in TERMINAL or (args.max_ticks and ticks >= args.max_ticks):
                    break
                time.sleep(args.poll)
        elif cmd == "approve":
            show(missions.approve(args.mission_id, args.proposal_id, args.operator))
        elif cmd == "deny":
            missions.deny(args.mission_id, args.proposal_id, args.operator, args.reason or "denied by operator")
        elif cmd == "pause":
            missions.pause(args.mission_id, args.operator, args.reason)
        elif cmd == "resume":
            missions.resume(args.mission_id, args.operator)
        elif cmd == "cancel":
            missions.cancel(args.mission_id, args.operator, args.reason)
        if cmd not in ("create", "list", "status", "tick", "run", "approve"):
            print(missions.state(args.mission_id)["state"])
    except MissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
