"""drone_rf listen: run the external drone detector on an RF Sentinel receiver and print detections as JSON lines.

    drone_rf listen --base-url http://127.0.0.1:8790 --token T --device-id bladerf:1 --band 5g8 --json

The detector is found at --detector, $DRONE_RF_DETECTOR, or /opt/passive-shield/live_multiband_hop_detector_sender.py.
Its tuning (dwell, thresholds, cue settings) is the deployment's, not RF Sentinel's: put one argument per line in
--detector-args-file (default: $DRONE_RF_DETECTOR_ARGS, or detector.args next to the detector). This command only
adds the receiver, band, gain, and where to send events (a local UDP port it listens on).
Receive-only: the detector and this adapter never transmit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from .events import to_detection

DEFAULT_DETECTOR = "/opt/passive-shield/live_multiband_hop_detector_sender.py"
BANDS = {"5g8": ("radio5g", "ism_5g8"), "2g4": ("radio2g", "ism_2g4")}


def detector_path(explicit: str | None) -> Path | None:
    for candidate in (explicit, os.getenv("DRONE_RF_DETECTOR"), DEFAULT_DETECTOR):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def tuning_args(explicit: str | None, detector: Path) -> list[str]:
    path = explicit or os.getenv("DRONE_RF_DETECTOR_ARGS") or str(detector.parent / "detector.args")
    if not Path(path).is_file():
        return []
    out: list[str] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out += shlex.split(line)
    return out


def detector_command(args: argparse.Namespace, detector: Path, port: int) -> list[str]:
    prefix, band = BANDS[args.band]
    other = "radio2g" if prefix == "radio5g" else "radio5g"
    device = f"driver=rfsentinel,device_id={args.device_id},settle_us={args.settle_us:g}"
    return [sys.executable, "-m", "drone_rf.shim", str(detector),
            *tuning_args(args.detector_args_file, detector),
            # Ours last, so they win over anything in the tuning file.
            "--collector-host", "127.0.0.1", "--collector-port", str(port), "--station-id", args.station_id,
            f"--{prefix}-device", device, f"--{other}-device", "",
            f"--{prefix}-gain", f"{args.lna_gain_db + args.vga_gain_db:g}", "--detection-bands", band]


def listen(args: argparse.Namespace) -> int:
    detector = detector_path(args.detector)
    if detector is None:
        print(json.dumps({"kind": "error", "error": "drone detector not installed",
                          "detail": "set --detector or DRONE_RF_DETECTOR to the detector script"}), flush=True)
        return 2
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.5)
    env = dict(os.environ, DRONE_RF_BASE_URL=args.base_url, DRONE_RF_TOKEN=args.token or "",
               PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1]),
                                                        os.getenv("PYTHONPATH", "")])))
    child = subprocess.Popen(detector_command(args, detector, sock.getsockname()[1]), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    log_lines: list[str] = []

    def relay():  # detector progress -> stderr (stdout carries detections only)
        for line in child.stdout:
            log_lines.append(line.rstrip())
            del log_lines[:-20]
            if args.verbose:
                print(line.rstrip(), file=sys.stderr, flush=True)

    threading.Thread(target=relay, daemon=True).start()
    stopping = {"flag": False}

    def stop(*_):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + args.duration_s if args.duration_s else float("inf")
    emitted = 0
    try:
        while not stopping["flag"] and time.monotonic() < deadline and child.poll() is None:
            try:
                data, _ = sock.recvfrom(1 << 20)
            except socket.timeout:
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            detection = to_detection(event) if isinstance(event, dict) else None
            if detection:
                print(json.dumps(detection, sort_keys=True), flush=True)
                emitted += 1
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)  # the detector closes its sources and so releases the IQ stream
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
        sock.close()
    code = child.returncode
    if code not in (0, None, -signal.SIGTERM) and not stopping["flag"]:
        print(json.dumps({"kind": "error", "error": f"detector exited with code {code}",
                          "detail": " | ".join(log_lines[-5:])}), flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drone_rf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("listen")
    p.add_argument("--base-url", default=os.getenv("SDR_GATEWAY_BASE_URL") or "http://127.0.0.1:8790")
    p.add_argument("--token", default=os.getenv("SDR_GATEWAY_API_TOKEN") or "")
    p.add_argument("--device-id", required=True)
    p.add_argument("--band", choices=sorted(BANDS), default="5g8")
    p.add_argument("--lna-gain-db", type=float, default=0.0)
    p.add_argument("--vga-gain-db", type=float, default=40.0)
    p.add_argument("--settle-us", type=float, default=8_000.0)
    p.add_argument("--duration-s", type=float, default=0.0, help="0 = until stopped")
    p.add_argument("--station-id", default=socket.gethostname())
    p.add_argument("--detector")
    p.add_argument("--detector-args-file")
    p.add_argument("--json", action="store_true", help="accepted for parity with other plugins (output is always JSON)")
    p.add_argument("--verbose", action="store_true", help="relay the detector's own log to stderr")
    args = parser.parse_args(argv)
    return listen(args)


if __name__ == "__main__":
    raise SystemExit(main())
