"""RF Sentinel agent CLI. Runs are managed by the dashboard's run service (the single source of truth),
so runs started from the CLI or the dashboard can be inspected and stopped from either.

  rf-sentinel agent run "find bluetooth devices"                       # streams steps; Ctrl+C stops the run
  rf-sentinel agent run "find bluetooth devices for 2 minutes" --mode approval-required
  rf-sentinel agent run "find tire sensors" --detach                  # start and return the run id
  rf-sentinel agent status <run-id>
  rf-sentinel agent stop <run-id>
  rf-sentinel agent list
  rf-sentinel agent approve <run-id> <action-id> --operator NAME     # or: deny ... --reason "..."
  rf-sentinel agent report <run-id>
  rf-sentinel agent policy
  rf-sentinel agent run "find bluetooth devices" --local              # no dashboard: run in this process

Server: --server, else RF_SENTINEL_URL, else http://127.0.0.1:5050. Receive-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

COMMANDS = ("run", "status", "stop", "list", "approve", "deny", "report", "policy")
TERMINAL = ("done", "stopped", "denied", "error")


class Server:
    def __init__(self, url: str | None):
        self.url = (url or os.getenv("RF_SENTINEL_URL") or "http://127.0.0.1:5050").rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None):
        try:
            response = requests.request(method, f"{self.url}{path}", json=body, timeout=30)
        except requests.ConnectionError:
            raise SystemExit(f"error: cannot reach the RF Sentinel service at {self.url} "
                             f"(start the dashboard, set --server, or use --local)")
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:300]}
        if response.status_code >= 400:
            raise SystemExit(f"error: {data.get('error', response.status_code)}")
        return data


def show_job(job: dict, steps_from: int = 0) -> int:
    for step in job.get("steps", [])[steps_from:]:
        print(f"• {step['t']:>6.1f}s  {step['message']}", flush=True)
    return len(job.get("steps", []))


def print_devices(result: dict) -> None:
    devices = result.get("devices") or []
    if not devices:
        print("\nNo devices decoded in this window.")
        return
    print(f"\n{'Device':<20} {'Seen':>5} {'Best RSSI':>10}  Details")
    for d in devices:
        details = " · ".join(dict.fromkeys(str(d[k]) for k in ("identity", "name", "manufacturer",
                                                                "device_type_detail", "pdu_type") if d.get(k)))
        rssi = f"{d['best_rssi_dbfs']:.1f}" if "best_rssi_dbfs" in d else "-"
        print(f"{d['key']:<20} {d['count']:>5} {rssi:>10}  {details}")


def follow(server: Server, job_id: str, as_json: bool) -> int:
    shown, announced = 0, set()
    try:
        while True:
            job = server.call("GET", f"/api/agent/jobs/{job_id}")
            if not as_json:
                shown = show_job(job, shown)
                for p in job.get("pending_approvals", []):
                    if p["action_id"] not in announced:
                        announced.add(p["action_id"])
                        print(f"  waiting for approval: rf-sentinel agent approve {job_id} {p['action_id']} --operator NAME",
                              flush=True)
            if job["status"] in TERMINAL:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\nStopping {job_id}...", file=sys.stderr)
        server.call("POST", f"/api/agent/jobs/{job_id}/stop")
        return 130
    if as_json:
        print(json.dumps(job, indent=2, default=str))
    else:
        if job.get("error"):
            print(f"\n{job['status']}: {job['error']}")
        if job.get("result"):
            print_devices(job["result"])
            print(f"\nRun {job_id} ({job['result']['run_id']}) · report: {server.url}/api/agent/jobs/{job_id}/report")
    return 0 if job["status"] in ("done", "stopped") else 1


def run_local(args) -> int:
    from rf_platform.sdr_backend import configure
    from .core import Agent
    from .gateway import PermissionDenied
    from .profiles import IntentError
    from .report import save
    if not os.getenv("SDR_GATEWAY_BASE_URL"):
        sdr = configure()
        if not sdr.get("available", True):
            print(f"error: SoapySDR is unavailable: {sdr.get('error')}", file=sys.stderr)
            return 2
    try:
        result = Agent(mode=args.mode).run(" ".join(args.request),
                                           on_step=lambda m: None if args.json else print(f"• {m}", flush=True),
                                           gain_db=args.gain)
    except (IntentError, PermissionDenied) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    folder = save(result)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_devices(result)
        print(f"\nRun {result['run_id']}: {folder / 'report.html'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]  # `python -m rf_platform.agent "find bluetooth devices"` still works
    parser = argparse.ArgumentParser(prog="rf-sentinel agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", help="RF Sentinel service URL (default RF_SENTINEL_URL or http://127.0.0.1:5050)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", parents=[common])
    run.add_argument("request", nargs="+")
    run.add_argument("--mode", choices=["observe", "approval-required", "passive-auto"])
    run.add_argument("--gain", type=float, help="Fixed gain in dB (disables automatic gain)")
    run.add_argument("--detach", action="store_true", help="Start the run and print its id")
    run.add_argument("--json", action="store_true")
    run.add_argument("--local", action="store_true", help="Run in this process instead of the service")
    for name in ("status", "stop", "report"):
        sub.add_parser(name, parents=[common]).add_argument("run_id")
    sub.add_parser("list", parents=[common])
    sub.add_parser("policy", parents=[common])
    for name in ("approve", "deny"):
        c = sub.add_parser(name, parents=[common])
        c.add_argument("run_id")
        c.add_argument("action_id")
        c.add_argument("--operator", required=True)
        c.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    server = Server(args.server)

    if args.cmd == "run":
        if args.local:
            return run_local(args)
        body = {"command": " ".join(args.request), "source": "cli"}
        if args.mode:
            body["mode"] = args.mode
        if args.gain is not None:
            body["gain_db"] = args.gain
        job = server.call("POST", "/api/agent/run", body)
        if args.detach:
            print(job["job_id"])
            return 0
        print(f"Run {job['job_id']} ({job['mode']} mode)", flush=True)
        return follow(server, job["job_id"], args.json)
    if args.cmd == "status":
        job = server.call("GET", f"/api/agent/jobs/{args.run_id}")
        print(f"{job['job_id']}  {job['status']}  {job['mode']}  from {job['source']}  \"{job['command']}\"")
        show_job(job)
        for p in job.get("pending_approvals", []):
            print(f"  pending {p['action_id']}: {p['tool']} {p['params']}")
        if job.get("result"):
            print_devices(job["result"])
        return 0
    if args.cmd == "stop":
        print(server.call("POST", f"/api/agent/jobs/{args.run_id}/stop")["status"])
        return 0
    if args.cmd == "list":
        for job in server.call("GET", "/api/agent/jobs"):
            devices = len((job.get("result") or {}).get("devices") or [])
            print(f"{job['job_id']}  {job['status']:<17} {job['mode']:<17} {job['source']:<9} "
                  f"{devices:>3} device(s)  {job['command']}")
        return 0
    if args.cmd in ("approve", "deny"):
        result = server.call("POST", f"/api/agent/jobs/{args.run_id}/{args.cmd}",
                             {"action_id": args.action_id, "operator": args.operator, "reason": args.reason})
        print(f"{result['action_id']}: {result['decision']} by {result['operator']}")
        return 0
    if args.cmd == "report":
        job = server.call("GET", f"/api/agent/jobs/{args.run_id}")
        print(f"{server.url}/api/agent/jobs/{args.run_id}/report" if job.get("report_dir") else "No report yet.")
        return 0
    if args.cmd == "policy":
        print(json.dumps(server.call("GET", "/api/agent/policy"), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
