#!/usr/bin/env python3
"""Negative-control evaluation of the BLE advertising decoder on recorded CS8 IQ.

  python3 scripts/eval_ble_false_positives.py .demo/iq/*.cs8 --json-out .demo/iq/fp.json

Each recording is decoded with all 40 BLE whitening sequences. A packet can only pass CRC under the
sequence of the channel it was sent on, so for a recording tuned to advertising channel N, detections
under any other sequence are false positives; for recordings away from the advertising channels (other
bands, WiFi), every detection is a false positive. With zero false positives over N sync candidates the
95% upper bound on the per-candidate false-positive rate is 3/N (rule of three), which is reported
instead of claiming "zero".
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_ble_decoder import CHANNELS, evaluate, load_detector  # noqa: E402

WHITENINGS = range(40)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--chunk-samples", type=int, default=131_072)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    decoder = load_detector(None)
    rows, trials, false_positives = [], 0, 0
    for path in args.files:
        meta = json.loads(path.with_suffix(".json").read_text())
        true_channel = CHANNELS.get(meta["center_freq_hz"])
        row = {"file": path.name, "center_freq_hz": meta["center_freq_hz"], "gain_db": meta.get("gain_db"),
               "advertising_channel": true_channel, "true_detections": None, "false_positives": 0,
               "negative_candidates": 0, "levels": meta.get("levels")}
        for whitening in WHITENINGS:
            result = evaluate(decoder, path, args.chunk_samples, whitening_channel=whitening)
            if whitening == true_channel:
                row["true_detections"], row["devices"] = result["detections"], result["devices"]
                continue
            row["false_positives"] += result["detections"]
            row["negative_candidates"] += result["stats"]["aa_candidates"]
        trials += row["negative_candidates"]
        false_positives += row["false_positives"]
        rows.append(row)
        print(f"{path.name:<22} {meta['center_freq_hz'] / 1e6:>7.1f} MHz gain {row['gain_db']:>4} | true "
              f"{row['true_detections'] if true_channel else '-':>4} | false {row['false_positives']} of "
              f"{row['negative_candidates']} negative sync candidates", flush=True)
    bound = (3.0 / trials) if trials and not false_positives else None
    summary = {"recordings": len(rows), "negative_candidates": trials, "false_positives": false_positives,
               "fp_rate_upper_95": bound}
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps({"rows": rows, "summary": summary}, indent=2) + "\n")
    return 1 if false_positives else 0


if __name__ == "__main__":
    raise SystemExit(main())
