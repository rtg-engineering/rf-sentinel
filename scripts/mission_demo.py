#!/usr/bin/env python3
"""End-to-end bounded mission through the real Docker backend and HTTP API.

The replay starts with normal activity, then the Telemetry-GW access point and its station go quiet.
A mission monitors on a short interval: it captures snapshots over HTTP, runs read-only assessments,
proposes follow-ups, and waits for an operator decision on anything restricted.

  python3 scripts/mission_demo.py --build                       # stops at the approval request
  python3 scripts/mission_demo.py --approve-as ops-demo         # an operator approves; execution is simulated

Writes .demo/analyst/mission-<UTC>/ (memory.sqlite, mission.json). Exit status is nonzero if the mission
does not reach the expected state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_demo_events import build_events  # noqa: E402
from rf_platform.analyst.evidence import Snapshot, capture_status  # noqa: E402
from rf_platform.analyst.memory import EnvironmentMemory  # noqa: E402
from rf_platform.analyst.missions import TERMINAL, Missions  # noqa: E402

CYCLE = 21
TELEMETRY_BSSID = "02:10:7A:00:30:11"


def run(args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def accepted(base):
    with urllib.request.urlopen(base + "/api/status?receipts_after=999999999", timeout=3) as response:
        return json.load(response).get("validation_metrics", {}).get("events_accepted", 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", action="store_true", help="Build rf-sentinel:public-demo first")
    parser.add_argument("--approve-as", help="Act as this named operator and approve the first restricted proposal")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between mission assessments (about three replay cycles)")
    args = parser.parse_args()

    work = ROOT / ".demo/analyst" / f"mission-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    work.mkdir(parents=True)
    events = build_events(cycles=8)  # normal 9.5 s replay cycles
    events = [e for e in events if e["demo_sequence"] <= 2 * CYCLE or e.get("bssid") != TELEMETRY_BSSID]
    for e in events:  # leave a gap after the baseline so the first capture is clean
        if e["demo_sequence"] > 2 * CYCLE:
            e["offset_s"] += 6.0
    fixture = work / "events.jsonl"
    fixture.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
    if args.build:
        run(["docker", "build", "-q", "-f", str(ROOT / "Dockerfile.demo"), "-t", "rf-sentinel:public-demo", str(ROOT)])

    name = "rf-mission-demo-" + uuid.uuid4().hex[:8]
    missions = Missions(EnvironmentMemory(work / "memory.sqlite"))
    try:
        run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080", "-e", "RF_SENTINEL_DEMO_LOOP=0",
             "-e", "RF_SENTINEL_DEMO_START_PAUSED=1", "-e", "RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800",
             "-v", f"{fixture}:/app/.demo/events/public-demo-events.jsonl:ro", "rf-sentinel:public-demo"])
        base = "http://127.0.0.1:" + run(["docker", "port", name, "8080/tcp"]).strip().rsplit(":", 1)[1]
        for _ in range(100):
            try:
                accepted(base); break
            except OSError:
                time.sleep(0.3)
        request = urllib.request.Request(base + "/api/scan/start", data=b'{"mode":"sentinel","preserve_detections":true}',
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()
        while accepted(base) < 2 * CYCLE:
            time.sleep(0.2)

        def capture():
            return Snapshot.from_status(capture_status(base), "mission")

        mid = missions.create("Lab watch (demo)", "Report and investigate changes in the lab's wireless environment",
                              {"allowed_actions": ["start_passive_capture"], "assess_interval_s": args.interval,
                               "max_assessments": 3, "duration_s": 4 * args.interval, "max_action_duration_s": 300},
                              created_by="demo-script")
        missions.submit(mid, "demo-script")
        missions.approve_scope(mid, args.approve_as or "scope-reviewer")
        missions.start(mid, capture(), args.approve_as or "scope-reviewer")
        print(f"Mission {mid} started; baseline captured at {accepted(base)} events", flush=True)
        decided = False
        while True:
            time.sleep(args.interval + 0.1)
            state = missions.tick(mid, capture)
            pending = missions.state(mid)["pending"]
            print(f"  {state}" + (f"  pending {pending['proposal_id']}: {pending['action']} {pending['params']}"
                                  if pending else ""), flush=True)
            if pending and not decided:
                decided = True
                if not args.approve_as:
                    print("  Waiting for an operator decision; stopping here (rerun with --approve-as NAME to approve).")
                    break
                result = missions.approve(mid, pending["proposal_id"], args.approve_as)
                print(f"  Approved by {args.approve_as}: executed={result['executed']} "
                      f"({result.get('result', {}).get('note', result.get('reason'))})", flush=True)
            if state in TERMINAL:
                break
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    report = missions.report(mid)
    (work / "mission.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    types = [e["type"] for e in report["events"]]
    expected = "action_executed" if args.approve_as else "approval_requested"
    ok = expected in types and "failed" not in types
    print(f"{'PASS' if ok else 'FAIL'}: {report['state']}, {report['assessments']} assessments, "
          f"{report['actions_executed']} simulated actions → {work / 'mission.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
