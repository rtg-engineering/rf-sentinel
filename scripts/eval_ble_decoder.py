#!/usr/bin/env python3
"""Evaluate BLE advertising decoders on recorded CS8 IQ (from scripts/record_iq.py).

  python3 scripts/eval_ble_decoder.py .demo/iq/ble-ch37.cs8 .demo/iq/ble-ch38.cs8 .demo/iq/ble-ch39.cs8 \\
      --baseline-ref origin/main --chunk-samples 16384 131072 --json-out .demo/iq/eval.json

Compares the decoder at a git ref (the baseline) with the working-tree decoder on identical samples.
Both run on a stream-time clock, so time-based de-duplication behaves as it would live.

  detections      CRC-valid advertising packets reported (repeats of one payload within 1 s suppressed)
  devices         distinct advertiser addresses
  payloads        distinct packet contents
  false_positives CRC-valid packets found when de-whitening with the WRONG channel's sequence. Real
                  packets cannot pass that way, so any hit is a false detection by construction.
  lost            payloads the baseline found that the candidate did not
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DETECTOR = "rf_platform/plugins/bluetooth-lowenergy/src/bluetooth_lowenergy/detector.py"
CHANNELS = {2_402_000_000: 37, 2_426_000_000: 38, 2_480_000_000: 39}


def load_detector(ref: str | None):
    """Import detector.py from the working tree, or from a git ref, as a standalone module."""
    if ref is None:
        path = ROOT / DETECTOR
    else:
        source = subprocess.run(["git", "show", f"{ref}:{DETECTOR}"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        # Same directory as the live decoder, so path-relative lookups inside it still resolve.
        path = (ROOT / DETECTOR).with_name(f"_eval_baseline_{ref.replace('/', '_')}.py")
        path.write_text(source)
    spec = importlib.util.spec_from_file_location(f"ble_detector_{abs(hash((ref, str(path))))}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if ref is not None:
            path.unlink(missing_ok=True)
    return module


class StreamClock:
    """Stands in for the decoder module's `time` so wall-clock logic follows the recording's timeline."""

    def __init__(self):
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now


def evaluate(module, path: Path, chunk_samples: int, whitening_channel: int | None = None) -> dict:
    meta = json.loads(path.with_suffix(".json").read_text())
    rate, freq = meta["sample_rate_sps"], meta["center_freq_hz"]
    channel = CHANNELS.get(freq, 37)
    clock, real_time = StreamClock(), module.time
    module.time = clock
    detector = module.BLEAdvertisingDetector(sample_rate_sps=rate, center_freq_hz=freq,
                                             channel=channel if whitening_channel is None else whitening_channel)
    chunk_bytes = chunk_samples * 2
    packets, addresses, events = set(), set(), 0
    started = time.perf_counter()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(chunk_bytes):
                clock.now += len(chunk) / 2 / rate
                _, out = detector.process_iq_i8(chunk)
                for event in out:
                    if event.get("kind") == "ble_adv":
                        events += 1
                        packets.add((channel, event.get("packet")))
                        addresses.add(event.get("address"))
    finally:
        module.time = real_time
    elapsed = time.perf_counter() - started
    stats = dict(getattr(detector, "stats", {}) or {})
    return {"file": path.name, "channel": channel, "seconds": meta["seconds"], "chunk_samples": chunk_samples,
            "detections": events, "devices": len(addresses), "payloads": len(packets),
            "realtime_factor": round(meta["seconds"] / elapsed, 2), "stats": stats,
            "_packets": packets, "_addresses": addresses}


def wrong_channel(channel: int) -> int:
    return {37: 38, 38: 39, 39: 37}.get(channel, 37)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--baseline-ref", default="origin/main")
    parser.add_argument("--chunk-samples", type=int, nargs="+", default=[16_384, 131_072])
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    decoders = {"baseline": load_detector(args.baseline_ref), "candidate": load_detector(None)}
    rows = []
    keys = ("detections", "devices", "payloads", "false_positives")
    for chunk in args.chunk_samples:
        for path in args.files:
            result = {}
            for name, mod in decoders.items():
                res = evaluate(mod, path, chunk)
                res["false_positives"] = evaluate(mod, path, chunk, wrong_channel(res["channel"]))["detections"]
                result[name] = res
            base, cand = result["baseline"], result["candidate"]
            lost = base["_packets"] - cand["_packets"]
            row = {"file": path.name, "channel": base["channel"], "chunk_samples": chunk,
                   "baseline": {k: v for k, v in base.items() if not k.startswith("_")},
                   "candidate": {k: v for k, v in cand.items() if not k.startswith("_")},
                   "lost_payloads": len(lost), "new_payloads": len(cand["_packets"] - base["_packets"])}
            rows.append(row)
            print(f"{path.name:<22} chunk {chunk:>6} | " + " | ".join(
                f"{name}: {r['detections']:>4} det {r['devices']:>3} dev {r['payloads']:>3} payloads "
                f"{r['false_positives']} FP {r['realtime_factor']:>5.1f}x RT" for name, r in (("baseline", base), ("candidate", cand)))
                + f" | lost {len(lost)}", flush=True)
    totals = {}
    for chunk in args.chunk_samples:
        subset = [r for r in rows if r["chunk_samples"] == chunk]
        totals[str(chunk)] = {name: {**{k: sum(r[name][k] for r in subset) for k in keys},
                                     "min_realtime_factor": min(r[name]["realtime_factor"] for r in subset)}
                              for name in ("baseline", "candidate")}
        totals[str(chunk)]["lost_payloads"] = sum(r["lost_payloads"] for r in subset)
    if args.json_out:
        args.json_out.write_text(json.dumps({"baseline_ref": args.baseline_ref, "rows": rows, "totals": totals},
                                            indent=2, default=str) + "\n")
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
