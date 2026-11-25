#!/usr/bin/env python3
"""Regenerate Analyst scenario fixtures from the real demo backend, in-process.

Each scenario ingests events through the normal demo ingestion path, captures /api/status after the
reference window and at the end, and writes tests/fixtures/analyst/<scenario>/:
  baseline.json, current.json   truth-stripped snapshots (what the analyst sees)
  truth.json                    expected and forbidden findings (read only by the evaluator)

Scenarios:
  new_entity          21 baseline events; then a new BLE beacon, a station move, and a strong signal
  ap_outage           2 normal cycles; then 3 cycles without the Telemetry-GW AP or its station
  wifi_sensor_outage  2 normal cycles; then 3 cycles with no WiFi events at all
  quiet_control       2 normal cycles; then 3 more normal cycles (nothing should be reported as a change)

Also writes tests/fixtures/analyst_memory/{day1,day2,day3}.json: one continuing environment for the
persistent-memory tests (day1 baseline; day2 normal; day3 the Telemetry AP and its station go quiet).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXED_CAPTURE = {"baseline": "2026-11-22T12:00:00+00:00", "current": "2026-11-22T12:05:00+00:00"}
CYCLE = 21
TELEMETRY_BSSID = "02:10:7A:00:30:11"
TELEMETRY_STATION = "72:94:11:3B:0C:90"
CHANGE_CATEGORIES = ["new_entity", "relationship_change", "signal_change", "missing_entity", "sensor_gap"]


def cycles(events, first, last):
    return [e for e in events if first * CYCLE < e["demo_sequence"] <= last * CYCLE]


def scenarios():
    from scripts.generate_demo_events import build_events, build_validation_events
    validation = build_validation_events(2.5, 10)
    reference_count = sum(e["scenario_phase"] == "baseline" for e in validation)
    normal = build_events(cycles=5)
    telemetry = lambda e: e.get("bssid") == TELEMETRY_BSSID  # noqa: E731
    wifi = lambda e: e.get("protocol") == "wifi"  # noqa: E731
    return {
        "new_entity": (validation[:reference_count], validation[reference_count:], {
            "expected": [{"category": "new_entity", "needle": "02:00:00:00:99:01"},
                         {"category": "relationship_change", "needle": TELEMETRY_STATION, "also": "02:10:7A:00:10:01"},
                         {"category": "signal_change", "needle": "02:00:00:00:99:01"}],
            "forbidden": [{"category": "missing_entity"}, {"category": "sensor_gap"}]}),
        "ap_outage": (cycles(normal, 0, 2), [e for e in cycles(normal, 2, 5) if not telemetry(e)], {
            "expected": [{"category": "missing_entity", "needle": TELEMETRY_BSSID,
                          "hypothesis_weakened": "sensor_or_collection"},
                         {"category": "missing_entity", "needle": TELEMETRY_STATION}],
            "forbidden": [{"category": "sensor_gap"}, {"category": "new_entity"}, {"category": "relationship_change"}]}),
        "wifi_sensor_outage": (cycles(normal, 0, 2), [e for e in cycles(normal, 2, 5) if not wifi(e)], {
            "expected": [{"category": "sensor_gap", "needle": "wlan-demo0", "hypothesis_supported": "sensor_or_collection"}],
            "forbidden": [{"category": "missing_entity", "protocol": "WIFI"}, {"category": "new_entity"}]}),
        "quiet_control": (cycles(normal, 0, 2), cycles(normal, 2, 5), {
            "expected": [],
            "forbidden": [{"category": c} for c in CHANGE_CATEGORIES]}),
    }


def main() -> int:
    from rf_platform.analyst.evidence import Snapshot
    with tempfile.TemporaryDirectory() as temp:
        os.environ.update({"RF_SENTINEL_DEMO_MODE": "1", "RF_SENTINEL_DEMO_START_PAUSED": "1",
                           "RF_SENTINEL_LOG_DIR": temp, "RF_SENTINEL_TEXTUAL_CONSOLE": "0",
                           "RF_SENTINEL_CONSOLE_DASHBOARD": "0",
                           "RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP": "1795348800"})
        from rf_platform.ui import _load_backend
        with patch("threading.Thread.start"):
            backend = _load_backend()
        backend.BLE_IDENTITY_CACHE_PATH = Path(temp) / "ble_identities.json"
        client = backend.app.test_client()
        for name, (reference, current, truth) in scenarios().items():
            backend.ble_identity_cache.clear()
            client.post("/api/clear", json={})
            with backend.state_lock:
                backend._reset_stats()
                backend._demo_prime_state()
            out = ROOT / "tests/fixtures/analyst" / name
            out.mkdir(parents=True, exist_ok=True)
            for label, events in (("baseline", reference), ("current", current)):
                for event in events:
                    backend._demo_ingest_payload(dict(event))
                snapshot = Snapshot.from_status(client.get("/api/status").get_json(), label, FIXED_CAPTURE[label])
                (out / f"{label}.json").write_text(json.dumps(snapshot.to_dict(), indent=1, sort_keys=True) + "\n")
                print(f"{name}/{label}: {snapshot.snapshot_id} entities={len(snapshot.entities)} "
                      f"observations={len(snapshot.observations)}")
            (out / "truth.json").write_text(json.dumps(truth, indent=1, sort_keys=True) + "\n")
        from scripts.generate_demo_events import build_events
        normal = build_events(cycles=8)
        days = {"day1": cycles(normal, 0, 2), "day2": cycles(normal, 2, 5),
                "day3": [e for e in cycles(normal, 5, 8) if e.get("bssid") != TELEMETRY_BSSID]}
        backend.ble_identity_cache.clear()
        client.post("/api/clear", json={})
        with backend.state_lock:
            backend._reset_stats()
            backend._demo_prime_state()
        out = ROOT / "tests/fixtures/analyst_memory"
        out.mkdir(parents=True, exist_ok=True)
        for n, (label, events) in enumerate(days.items()):
            for event in events:
                backend._demo_ingest_payload(dict(event))
            snapshot = Snapshot.from_status(client.get("/api/status").get_json(), label, f"2026-11-2{n + 2}T12:00:00+00:00")
            (out / f"{label}.json").write_text(json.dumps(snapshot.to_dict(), indent=1, sort_keys=True) + "\n")
            print(f"memory/{label}: {snapshot.snapshot_id} entities={len(snapshot.entities)} observations={len(snapshot.observations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
