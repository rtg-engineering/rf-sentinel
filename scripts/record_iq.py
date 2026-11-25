#!/usr/bin/env python3
"""Record raw CS8 IQ from the IQ service to a file (+ .json metadata) for reproducible decoder evaluation.

  python3 scripts/record_iq.py --device bladerf:0 --freq-hz 2402000000 --rate 4000000 --gain 48 \\
      --seconds 20 --out .demo/iq/ble-ch37.cs8

Receive-only. Uses SDR_GATEWAY_BASE_URL (default http://127.0.0.1:8790) and SDR_GATEWAY_API_TOKEN.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rf_platform.agent.radio import Radio, iq_levels, split_gain  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", required=True)
    parser.add_argument("--freq-hz", type=int, required=True)
    parser.add_argument("--rate", type=int, default=4_000_000)
    parser.add_argument("--gain", type=float, required=True)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--bandwidth-hz", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import websocket
    radio = Radio()
    device = next(d for d in radio.devices() if d["id"] == args.device)
    lna, vga = split_gain(device["driver"], args.gain)
    body = {"device_id": args.device, "center_freq_hz": args.freq_hz, "sample_rate_sps": args.rate,
            "lna_gain_db": lna, "vga_gain_db": vga, "baseband_filter_hz": args.bandwidth_hz or args.rate // 2}
    response = requests.post(f"{radio.base_url}/streams/start", headers=radio.headers, json=body, timeout=30)
    response.raise_for_status()
    stream_id = response.json()["stream_id"]
    wanted = int(args.rate * args.seconds) * 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    digest, written = hashlib.sha256(), 0
    started = datetime.now(timezone.utc).isoformat()
    try:
        ws = websocket.create_connection(f"ws://{radio.base_url.split('://', 1)[1]}/ws/iq/{stream_id}?keep=1"
                                         + (f"&token={radio.token}" if radio.token else ""), timeout=15)
        with args.out.open("wb") as fh:
            sample = b""
            while written < wanted:
                chunk = ws.recv()[: wanted - written]
                fh.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if len(sample) < 4_000_000:
                    sample += chunk
        ws.close()
    finally:
        requests.post(f"{radio.base_url}/streams/{stream_id}/stop", headers=radio.headers, timeout=10)
    levels = iq_levels(sample, args.gain)
    meta = {"device_id": args.device, "driver": device["driver"], "center_freq_hz": args.freq_hz,
            "sample_rate_sps": args.rate, "gain_db": args.gain, "format": "cs8", "bytes": written,
            "seconds": written / 2 / args.rate, "sha256": digest.hexdigest(), "recorded_at": started,
            "levels": {"p999_dbfs": round(levels.p999_dbfs, 1), "rms_dbfs": round(levels.rms_dbfs, 1),
                       "clip_fraction": levels.clip_fraction}}
    args.out.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{args.out}: {meta['seconds']:.1f} s, {written / 1e6:.0f} MB, {levels.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
