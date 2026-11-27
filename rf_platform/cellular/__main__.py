"""Cellular survey adapter: stream modem cell observations as RF Sentinel JSON detection events.

  python -m rf_platform.cellular --url http://127.0.0.1:5098 --poll-s 3 --qscan-every-s 90

Serving/neighbour cells are polled every --poll-s; a full network scan runs in the background every
--qscan-every-s (0 = only at start). One JSON object per line; runs until interrupted.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time

from .survey import SurveyClient, normalise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url")
    parser.add_argument("--poll-s", type=float, default=3.0)
    parser.add_argument("--qscan-every-s", type=float, default=90.0)
    parser.add_argument("--no-qscan", action="store_true")
    args = parser.parse_args(argv)
    client = SurveyClient(args.url)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    known: dict = {}
    lock = threading.Lock()

    def emit(observations, label):
        events = []
        with lock:
            for observation in observations:
                event = normalise(observation, known)
                if event:
                    event["seen_at"] = time.time()
                    events.append(event)
        for event in events:
            print(json.dumps(event, default=str), flush=True)
        print(json.dumps({"kind": "cell_survey_stats", "channel": label, "cells": len(events)}), flush=True)

    def qscan_loop():
        while not stop.is_set():
            try:
                emit(client.qscan(), "qscan")
            except Exception as exc:
                print(f"qscan failed: {exc}", file=sys.stderr, flush=True)
            if args.qscan_every_s <= 0 or stop.wait(args.qscan_every_s):
                return

    # The full scan holds the modem for about a minute, so it starts only after the first serving/neighbour
    # poll has reported: started together, the quick poll queues behind it and a short survey reports nothing.
    qscan_started = args.no_qscan
    try:
        while not stop.is_set():
            try:
                emit(client.scan(), "serving")
            except Exception as exc:
                print(f"scan failed: {exc}", file=sys.stderr, flush=True)
            if not qscan_started:
                qscan_started = True
                threading.Thread(target=qscan_loop, daemon=True).start()
            stop.wait(args.poll_s)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
