"""RF Sentinel agent CLI. Runs are managed by the dashboard's run service (the single source of truth),
so runs started from the CLI or the dashboard can be inspected and stopped from either.

  rf-sentinel agent run "find bluetooth devices"                       # streams steps; Ctrl+C stops the run
  rf-sentinel agent run "find bluetooth devices for 2 minutes" --mode approval-required
  rf-sentinel agent run "find tire sensors" --detach                  # start and return the run id
  rf-sentinel agent status <run-id>
  rf-sentinel agent stop <run-id>
  rf-sentinel agent list
  rf-sentinel agent approve <run-id> <action-id>                     # or: deny ... --reason "..." (approver role)
  rf-sentinel agent report <run-id>
  rf-sentinel agent policy
  rf-sentinel agent library                                         # saved plans; run one: agent run "run <name>"
  rf-sentinel agent library save "night watch" "from 10pm to 6am, every 30 minutes, sweep for trackers for 1 minute"
  rf-sentinel agent ready "survey cell towers for 2 minutes"          # can it run here now? (nothing runs)
  rf-sentinel agent ask <run-id> "which trackers came back every cycle?"
  rf-sentinel agent run "find bluetooth devices" --local              # no dashboard: run in this process
  rf-sentinel agent plan "watch for trackers for 5 minutes; if one is seen for 2 minutes, alert me"
  rf-sentinel agent run "watch for trackers ...; if ..., alert me and record 5 seconds of IQ" --planner model
  rf-sentinel agent run "sweep for trackers for 2 minutes. if you find any, watch them for 5 minutes and alert me
                         if one is still there after 3 minutes. then survey cell towers for 1 minute"   # a playbook

  rf-sentinel agent operators add alice --role approver --save     # on the dashboard host; prints a token once
  rf-sentinel agent operators list | remove NAME

Server: --server, else RF_SENTINEL_URL, else http://127.0.0.1:5050. Receive-only.
Token (when the dashboard requires sign-in): --token, else RF_SENTINEL_TOKEN, else ~/.config/rf-sentinel/token.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

COMMANDS = ("run", "plan", "status", "stop", "list", "approve", "deny", "report", "policy", "library", "ask", "ready",
            "operators")
TOKEN_FILE = os.path.expanduser("~/.config/rf-sentinel/token")


def saved_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""
TERMINAL = ("done", "stopped", "denied", "error")


class Server:
    def __init__(self, url: str | None, token: str | None = None):
        self.url = (url or os.getenv("RF_SENTINEL_URL") or "http://127.0.0.1:5050").rstrip("/")
        self.token = token or os.getenv("RF_SENTINEL_TOKEN") or saved_token()

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = requests.request(method, f"{self.url}{path}", json=body, timeout=timeout, headers=headers)
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


def print_story(result) -> None:
    story = result.get("narrative") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("narrative")
    story = story or []
    if story:
        print("\nWhat happened")
        for line in story:
            print(f"  · {line}")


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
        steps = f"  [step {', '.join(map(str, d['steps']))}]" if d.get("steps") else ""
        print(f"{d['key']:<20} {d['count']:>5} {rssi:>10}  {details}{steps}")


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
            print_story(job["result"])
            print(f"\nRun {job_id} ({job['result']['run_id']}) · report: {server.url}/api/agent/jobs/{job_id}/report")
    return 0 if job["status"] in ("done", "stopped") else 1


def print_plan(planned: dict) -> None:
    summary = planned["summary"]
    who = planned["planner"] + (f" ({planned['model']})" if planned.get("model") else "")
    print(f"{'Playbook' if planned.get('playbook') else 'Plan'} ({who}"
          f"{'; fallback: ' + planned['fallback_reason'] if planned.get('fallback_reason') else ''})")
    if planned.get("explain"):
        import textwrap
        print(textwrap.fill(planned["explain"], width=100, initial_indent="  ", subsequent_indent="  ") + "\n")
    if planned.get("playbook"):
        for index, step in enumerate(summary["steps"]):
            print(f"  {index + 1}. {step['title']}")
            for label, value in (("IF", step.get("gate")), ("FOCUS", step.get("focus"))):
                if value:
                    print(f"     {label:<7} {value}")
            print(f"     FIND    {step['find']}")
            for f in step["filters"]:
                print(f"     FILTER  {f}")
            for t in step["triggers"]:
                print(f"     WHEN    {t}")
        print(f"  up to {summary['total_s']} s of collection")
        return
    print(f"  FIND    {summary['find']}")
    for f in summary["filters"]:
        print(f"  FILTER  {f}")
    for t in summary["triggers"]:
        print(f"  WHEN    {t}")
    for note in planned["spec"].get("notes", []):
        print(f"  note    {note}")


def run_local(args) -> int:
    from rf_platform.sdr_backend import configure
    from .core import Agent
    from .gateway import PermissionDenied
    from .profiles import IntentError
    from .report import save
    if not os.getenv("SDR_GATEWAY_BASE_URL") and not args.simulate:
        sdr = configure()
        if not sdr.get("available", True):
            print(f"error: SoapySDR is unavailable: {sdr.get('error')}", file=sys.stderr)
            return 2
    from .playbook import PlaybookSpec, plan_any, run_playbook, save as save_playbook
    from .workflow import WorkflowSpec, run_workflow
    try:
        planned = plan_any(" ".join(args.request), args.planner)
        if not args.json:
            print_plan(planned)
        agent = Agent(mode=args.mode, simulate=args.simulate or None)
        step = lambda m: None if args.json else print(f"• {m}", flush=True)  # noqa: E731
        info = {k: planned[k] for k in ("planner", "model", "fallback_reason")}
        if planned.get("playbook"):
            result = run_playbook(agent, PlaybookSpec.model_validate(planned["playbook"]), on_step=step, gain_db=args.gain,
                                  planner=info)
            save = save_playbook
        else:
            result = run_workflow(agent, WorkflowSpec.model_validate(planned["spec"]), on_step=step, gain_db=args.gain,
                                  planner=info)
    except (IntentError, PermissionDenied, ValueError) as exc:
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
        print_story(result)
        print(f"\nRun {result['run_id']}: {folder / 'report.html'}")
    return 0


def operators(args) -> int:
    from pathlib import Path
    from rf_platform.auth import OperatorStore, auth_dir
    store = OperatorStore(auth_dir(Path(args.log_dir)))
    if args.action == "list":
        for o in store.list():
            print(f"{o['name']:<24} {o['role']:<9} added {o['created_at']}")
        if not store.list():
            print(f"No operators in {store.path}.")
        return 0
    if not args.name:
        raise SystemExit("error: give the operator's name")
    if args.action == "remove":
        print("removed" if store.remove(args.name) else f"no operator {args.name}")
        return 0
    try:
        token = store.add(args.name, args.role)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"error: {exc}")
    print(f"{args.name} ({args.role}) added to {store.path}.")
    print(f"Token (shown once; sign in to the dashboard with it, or set RF_SENTINEL_TOKEN):\n{token}")
    if args.save:
        os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        print(f"Saved to {TOKEN_FILE} for this user's CLI.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]  # `python -m rf_platform.agent "find bluetooth devices"` still works
    parser = argparse.ArgumentParser(prog="rf-sentinel agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", help="RF Sentinel service URL (default RF_SENTINEL_URL or http://127.0.0.1:5050)")
    common.add_argument("--token", help="Operator token (default RF_SENTINEL_TOKEN or ~/.config/rf-sentinel/token)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", parents=[common])
    run.add_argument("request", nargs="+")
    run.add_argument("--mode", choices=["observe", "approval-required", "passive-auto"])
    run.add_argument("--gain", type=float, help="Fixed gain in dB (disables automatic gain)")
    run.add_argument("--detach", action="store_true", help="Start the run and print its id")
    run.add_argument("--json", action="store_true")
    run.add_argument("--local", action="store_true", help="Run in this process instead of the service")
    run.add_argument("--simulate", action="store_true",
                     help="Use simulated receivers and a scripted scenario (no radio; everything labelled SIMULATED)")
    run.add_argument("--planner", choices=["auto", "model", "deterministic"], default="deterministic",
                     help="Who turns the request into a plan (model needs RF_SENTINEL_PLANNER_MODEL or a login)")
    plan_cmd = sub.add_parser("plan", parents=[common], help="Show the plan for a request without running it")
    plan_cmd.add_argument("request", nargs="+")
    plan_cmd.add_argument("--planner", choices=["auto", "model", "deterministic"], default="auto")
    plan_cmd.add_argument("--json", action="store_true")
    for name in ("status", "stop", "report"):
        sub.add_parser(name, parents=[common]).add_argument("run_id")
    sub.add_parser("list", parents=[common])
    sub.add_parser("policy", parents=[common])
    lib = sub.add_parser("library", parents=[common], help="Saved plans; run one with: agent run \"run <name>\"")
    lib.add_argument("action", nargs="?", choices=["list", "save", "delete"], default="list")
    lib.add_argument("name", nargs="?")
    lib.add_argument("request", nargs="*", help="for save: the request to plan and store under the name")
    ask_cmd = sub.add_parser("ask", parents=[common], help="Ask about a finished run")
    ask_cmd.add_argument("run_id")
    ask_cmd.add_argument("question", nargs="+")
    ask_cmd.add_argument("--planner", choices=["auto", "model", "deterministic"], default="auto")
    ready = sub.add_parser("ready", parents=[common], help="Plan a request and check it can run here now (nothing runs)")
    ready.add_argument("request", nargs="+")
    ready.add_argument("--mode", choices=["observe", "approval-required", "passive-auto"])
    for name in ("approve", "deny"):
        c = sub.add_parser(name, parents=[common])
        c.add_argument("run_id")
        c.add_argument("action_id")
        c.add_argument("--operator", help="only when the dashboard runs without sign-in; otherwise your token names you")
        c.add_argument("--reason", default="")
    ops = sub.add_parser("operators", help="Manage dashboard operators (on the dashboard host; edits its log dir)")
    ops.add_argument("action", choices=["list", "add", "remove"])
    ops.add_argument("name", nargs="?")
    ops.add_argument("--role", choices=["viewer", "operator", "approver"], default="operator")
    ops.add_argument("--log-dir", default=os.getenv("RF_SENTINEL_LOG_DIR", "/var/log/rf_sentinel"),
                     help="the dashboard's RF_SENTINEL_LOG_DIR (operators live in <log dir>/auth/)")
    ops.add_argument("--save", action="store_true", help=f"also store the new token in {TOKEN_FILE} for this user's CLI")
    args = parser.parse_args(argv)
    if args.cmd == "operators":
        return operators(args)
    server = Server(args.server, args.token)

    if args.cmd == "run":
        if args.local:
            return run_local(args)
        planned = server.call("POST", "/api/agent/plan", {"text": " ".join(args.request), "planner": args.planner},
                              timeout=180)
        if not args.json:
            print_plan(planned)
        body = {"command": " ".join(args.request), "source": "cli", "simulate": args.simulate,
                **({"playbook": planned["playbook"]} if planned.get("playbook") else {"workflow": planned["spec"]}),
                "planner": {k: planned[k] for k in ("planner", "model", "fallback_reason")}}
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
    if args.cmd == "plan":
        try:
            planned = server.call("POST", "/api/agent/plan", {"text": " ".join(args.request), "planner": args.planner},
                                  timeout=180)
        except SystemExit as exc:
            if "cannot reach" not in str(exc):
                raise
            from .playbook import plan_any  # no service: plan locally
            try:
                planned = plan_any(" ".join(args.request), args.planner)
            except ValueError as err:
                raise SystemExit(f"error: {err}")
        print(json.dumps(planned, indent=2) if args.json else "", end="")
        if not args.json:
            print_plan(planned)
        return 0
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
                             {"action_id": args.action_id, "operator": args.operator or "", "reason": args.reason})
        print(f"{result['action_id']}: {result['decision']} by {result['operator']}")
        return 0
    if args.cmd == "report":
        job = server.call("GET", f"/api/agent/jobs/{args.run_id}")
        print(f"{server.url}/api/agent/jobs/{args.run_id}/report" if job.get("report_dir") else "No report yet.")
        return 0
    if args.cmd == "ask":
        out = server.call("POST", f"/api/agent/jobs/{args.run_id}/ask", {"question": " ".join(args.question),
                                                                          "planner": args.planner}, timeout=180)
        print(out["answer"])
        print(f"\n({'answered by ' + out['model'] + ', checked against the run' if out['by'] == 'model' else 'answered from the run records by code'}"
              + (f"; model: {out['fallback_reason']}" if out.get("fallback_reason") else "") + ")")
        return 0
    if args.cmd == "ready":
        planned = server.call("POST", "/api/agent/plan", {"text": " ".join(args.request), "planner": "deterministic"}, timeout=60)
        plan = {"playbook": planned["playbook"]} if planned.get("playbook") else {"spec": planned["spec"]}
        out = server.call("POST", "/api/agent/readiness", {"plan": plan, **({"mode": args.mode} if args.mode else {})})
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
        for c in out["checks"]:
            print(f"{mark[c['status']]}  {c['name']}: {c['detail']}")
        print("\nReady." if out["status"] == "ok" else "\nReady, with warnings." if out["ok"] else "\nNot ready.")
        return 0 if out["ok"] else 1
    if args.cmd == "library" and args.action == "save":
        if not args.name or not args.request:
            raise SystemExit('usage: agent library save NAME "request ..."')
        text = " ".join(args.request)
        planned = server.call("POST", "/api/agent/plan", {"text": text, "planner": "deterministic"}, timeout=60)
        plan = {"playbook": planned["playbook"]} if planned.get("playbook") else {"spec": planned["spec"]}
        saved = server.call("POST", "/api/agent/library", {"name": args.name, "plan": plan, "text": text})
        print(f"Saved \"{saved['name']}\". Run it with: rf-sentinel agent run \"run {saved['name']}\"")
        return 0
    if args.cmd == "library" and args.action == "delete":
        server.call("DELETE", f"/api/agent/library/{args.name}")
        print(f"Removed \"{args.name}\"")
        return 0
    if args.cmd == "library":
        for entry in server.call("GET", "/api/agent/library"):
            summary = entry.get("summary") or {}
            n = len(summary.get("steps", []))
            what = (f"{n} step{'s' if n != 1 else ''}" + (f", {summary['repeat']}" if summary.get("repeat") else "")
                    if entry["kind"] == "playbook" else summary.get("find", ""))
            print(f"{entry['name']:<28} {entry['kind']:<9} {what}")
        return 0
    if args.cmd == "policy":
        print(json.dumps(server.call("GET", "/api/agent/policy"), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
