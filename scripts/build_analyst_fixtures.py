#!/usr/bin/env python3
"""Regenerate tests/fixtures/analyst/{baseline,current}.json from the real demo backend, in-process.

Ingests the minimum validation scenario (21 baseline events, then 4 changes) through the normal
demo ingestion path and captures /api/status after the baseline and at the end. Test truth fields
are stripped by Snapshot.from_status before anything is written.
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


def main() -> int:
    from scripts.generate_demo_events import build_validation_events
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
        backend.ble_identity_cache.clear()
        client = backend.app.test_client()
        with backend.state_lock:
            backend._reset_stats()
            backend._demo_prime_state()
        events = build_validation_events(2.5, 10)
        baseline_count = sum(e["scenario_phase"] == "baseline" for e in events)
        out = ROOT / "tests/fixtures/analyst"
        out.mkdir(parents=True, exist_ok=True)
        for label, upto in (("baseline", baseline_count), ("current", len(events))):
            for event in events[:upto]:
                if not event.get("_ingested"):
                    backend._demo_ingest_payload(event)
                    event["_ingested"] = True
            snapshot = Snapshot.from_status(client.get("/api/status").get_json(), label, FIXED_CAPTURE[label])
            (out / f"{label}.json").write_text(json.dumps(snapshot.to_dict(), indent=1, sort_keys=True) + "\n")
            print(f"{label}: {snapshot.snapshot_id} entities={len(snapshot.entities)} observations={len(snapshot.observations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
