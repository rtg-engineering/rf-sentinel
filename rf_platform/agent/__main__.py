"""RF Sentinel agent CLI.

  python -m rf_platform.agent "find bluetooth devices"
  python -m rf_platform.agent "find bluetooth devices for 2 minutes on bladerf:1"
  python -m rf_platform.agent "look for zigbee on channel 25" --json
  python -m rf_platform.agent "find tire sensors" --gain 30        # skip automatic gain
  python -m rf_platform.agent list                                 # past runs
  python -m rf_platform.agent report RUN-xxxxxxxxxxxx              # where a run's verifiable report is

Receive-only. Uses the IQ service at SDR_GATEWAY_BASE_URL, or starts the built-in SoapySDR service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .core import Agent
from .profiles import IntentError


def history(argv: list[str]) -> int:
    from . import report
    if argv[0] == "list":
        for run in report.list_runs():
            print(f"{run['run_id']}  {run['started_at'][:19]}  {run['receiver']:<10} {run['devices']:>3} device(s)  {run['command']}")
        return 0
    if len(argv) < 2:
        print("usage: python -m rf_platform.agent report <run-id>", file=sys.stderr)
        return 2
    try:
        run = report.load(argv[1])
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    folder = report.runs_dir() / run["run_id"]
    print(f"{run['run_id']}: {run['command']} -> {len(run['devices'])} device(s); gains {run['gains_db']} dB")
    print(f"report: {folder / 'report.html'}\nverify: (cd {folder} && sha256sum -c checksums.sha256)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("list", "report"):
        return history(argv)
    parser = argparse.ArgumentParser(prog="python -m rf_platform.agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="+", help='What to look for, e.g. "find bluetooth devices"')
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    parser.add_argument("--gain", type=float, help="Fixed gain in dB (disables automatic gain)")
    parser.add_argument("--base-url", help="IQ service URL (default: SDR_GATEWAY_BASE_URL, else built-in service)")
    parser.add_argument("--no-save", action="store_true", help="Do not write a run report")
    args = parser.parse_args(argv)
    command = " ".join(args.command)

    if not (args.base_url or os.getenv("SDR_GATEWAY_BASE_URL")):
        from rf_platform.sdr_backend import configure
        sdr = configure()
        if not sdr.get("available", True):
            print(f"error: SoapySDR is unavailable: {sdr.get('error')}", file=sys.stderr)
            return 2

    def step(message: str) -> None:
        if not args.json:
            print(f"• {message}", flush=True)

    try:
        result = Agent(args.base_url).run(command, on_step=step, gain_db=args.gain)
    except IntentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if not args.no_save:
        from .report import save
        folder = save(result)
        result["report_path"] = str(folder / "report.html")
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    if result["devices"]:
        print(f"\n{'Device':<20} {'Seen':>5} {'Best RSSI':>10}  Details")
        for d in result["devices"]:
            details = " · ".join(dict.fromkeys(str(d[k]) for k in ("identity", "name", "manufacturer",
                                                                    "device_type_detail", "pdu_type") if d.get(k)))
            rssi = f"{d['best_rssi_dbfs']:.1f}" if "best_rssi_dbfs" in d else "-"
            print(f"{d['key']:<20} {d['count']:>5} {rssi:>10}  {details}")
    else:
        print("\nNo devices decoded in this window.")
    if result.get("report_path"):
        print(f"\nRun {result['run_id']}: {result['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
