#!/usr/bin/env python3
"""End-to-end Analyst demo through the real Docker backend and HTTP API.

1. Replays the minimum validation scenario (21 baseline events, then 4 changes) in the demo container.
2. Pauses after the baseline, captures a snapshot over HTTP, resumes, and captures again at the end.
3. Runs a read-only investigation (deterministic by default) and writes the evidence bundle.
4. Scores the result against scenario truth from the fixture, which the analyst never receives.

Exit status is nonzero if grounding or evaluation fails. Requires Docker and the
rf-sentinel:public-demo image (pass --build to build it).
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
from scripts.generate_demo_events import build_validation_events  # noqa: E402
from rf_platform.analyst.evaluate import evaluate  # noqa: E402
from rf_platform.analyst.evidence import EvidenceStore, Snapshot, capture_status  # noqa: E402
from rf_platform.analyst.investigate import run_investigation  # noqa: E402
from rf_platform.analyst.report import write_bundle  # noqa: E402

CHANGE_DELAY_S = 4.0


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


def post(base, path, payload):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for(base, condition, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/status?receipts_after=999999999", timeout=3) as response:
                metrics = json.load(response).get("validation_metrics", {})
            if condition(metrics):
                return metrics
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for replay state")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", action="store_true", help="Build rf-sentinel:public-demo first")
    parser.add_argument("--provider", choices=["deterministic", "pydantic-ai"], default="deterministic")
    parser.add_argument("--model", help="Pydantic AI model string (default anthropic:claude-opus-5-5)")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = ROOT / ".demo/analyst" / f"demo-{stamp}"
    work.mkdir(parents=True)
    events = build_validation_events(2.5, 10)
    baseline_count = sum(e["scenario_phase"] == "baseline" for e in events)
    for event in events:
        if event["scenario_phase"] != "baseline":
            event["offset_s"] += CHANGE_DELAY_S
    fixture = work / "events.jsonl"  # contains truth; mounted into the backend, read by the evaluator only
    fixture.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))

    if args.build:
        print("Building rf-sentinel:public-demo", flush=True)
        run(["docker", "build", "-q", "-f", "Dockerfile.demo", "-t", "rf-sentinel:public-demo", "."], cwd=ROOT)
    name = "rf-analyst-demo-" + uuid.uuid4().hex[:8]
    try:
        run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
             "-e", "RF_SENTINEL_DEMO_LOOP=0", "-e", "RF_SENTINEL_DEMO_START_PAUSED=1",
             "-e", "RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800",
             "-v", f"{fixture}:/app/.demo/events/public-demo-events.jsonl:ro", "rf-sentinel:public-demo"])
        port = run(["docker", "port", name, "8080/tcp"]).strip().rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        wait_for(base, lambda m: m.get("events_attempted") == 0)
        post(base, "/api/scan/start", {"mode": "sentinel", "preserve_detections": True})
        wait_for(base, lambda m: m.get("events_accepted", 0) >= baseline_count)
        post(base, "/api/scan/stop", {})
        paused = wait_for(base, lambda m: True)
        if paused["events_accepted"] != baseline_count:
            raise RuntimeError(f"Baseline capture missed the boundary: {paused['events_accepted']} accepted")
        snapshots = [Snapshot.from_status(capture_status(base), "baseline")]
        post(base, "/api/scan/start", {"mode": "sentinel", "preserve_detections": True})
        wait_for(base, lambda m: m.get("complete"))
        snapshots.append(Snapshot.from_status(capture_status(base), "current"))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    provider = None
    if args.provider == "pydantic-ai":
        from rf_platform.analyst.llm import DEFAULT_MODEL, PydanticAIAnalyst
        provider = PydanticAIAnalyst(args.model or DEFAULT_MODEL)
    store = EvidenceStore(snapshots)
    investigation = run_investigation(store, provider, provenance="synthetic_replay")
    bundle = write_bundle(work / "bundle", investigation, store)
    # Same scenario as tests/fixtures/analyst/new_entity; its truth is read by the evaluator only.
    truth = json.loads((ROOT / "tests/fixtures/analyst/new_entity/truth.json").read_text())
    result = evaluate(investigation, truth)
    (work / "evaluation.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for check, ok in result["checks"].items():
        print(f"{'PASS' if ok else 'FAIL'}  {check}")
    if investigation["provider"].get("fallback"):
        print(f"NOTE  provider {investigation['provider']['name']} failed; deterministic fallback used: "
              f"{investigation['provider']['error']}")
    print(f"{'PASS' if result['passed'] else 'FAIL'}: {investigation['investigation_id']} → {bundle / 'report.html'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
