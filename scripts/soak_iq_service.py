#!/usr/bin/env python3
"""Stress the IQ service the way agent runs do: start a stream, read it over the websocket for a few seconds, stop
it, repeat, on several receivers at once. Used to reproduce a native crash seen after hours of standing watches.

  SDR_GATEWAY_API_TOKEN=... python3 scripts/soak_iq_service.py --minutes 30 --devices bladerf:0 bladerf:1

Prints one line per minute; exits non-zero as soon as the service stops answering.
"""
from __future__ import annotations

import argparse
import os
import threading
import time

import requests
import websocket


def churn(base: str, token: str, device: str, stop: threading.Event, counts: dict, errors: list) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    freqs = [2_402_000_000, 2_426_000_000, 2_480_000_000, 2_442_000_000]
    i = 0
    while not stop.is_set():
        try:
            r = requests.post(f"{base}/streams/start", headers=headers, timeout=10,
                              json={"device_id": device, "center_freq_hz": freqs[i % len(freqs)],
                                    "sample_rate_sps": 4_000_000 if i % 3 else 20_000_000, "lna_gain_db": 0,
                                    "vga_gain_db": 30 + (i % 4) * 5})
            if r.status_code == 409:
                time.sleep(0.5)
                continue
            r.raise_for_status()
            sid = r.json()["stream_id"]
            ws = websocket.create_connection(f"{base.replace('http', 'ws', 1)}/ws/iq/{sid}?keep=1&token={token}", timeout=8)
            end = time.time() + 1.0 + (i % 4) * 0.7
            while time.time() < end:
                counts[device] = counts.get(device, 0) + len(ws.recv())
            ws.close()
            requests.post(f"{base}/streams/{sid}/stop", headers=headers, timeout=10)
            counts[f"{device} cycles"] = counts.get(f"{device} cycles", 0) + 1
        except Exception as exc:  # noqa: BLE001 - the soak reports whatever breaks
            errors.append(f"{time.strftime('%H:%M:%S')} {device}: {type(exc).__name__}: {exc}"[:300])
            time.sleep(1)
        i += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--devices", nargs="+", default=["bladerf:0", "bladerf:1"])
    args = parser.parse_args()
    token = os.getenv("SDR_GATEWAY_API_TOKEN", "")
    stop, counts, errors = threading.Event(), {}, []
    threads = [threading.Thread(target=churn, args=(args.base, token, d, stop, counts, errors), daemon=True)
               for d in args.devices]
    for t in threads:
        t.start()
    deadline = time.time() + args.minutes * 60
    try:
        while time.time() < deadline:
            time.sleep(60)
            try:
                requests.get(f"{args.base}/health", timeout=5).raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"{time.strftime('%H:%M:%S')} SERVICE DOWN: {exc}", flush=True)
                return 1
            cycles = {k: v for k, v in counts.items() if k.endswith("cycles")}
            print(f"{time.strftime('%H:%M:%S')} ok {cycles} errors={len(errors)}"
                  + (f" last: {errors[-1]}" if errors else ""), flush=True)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
