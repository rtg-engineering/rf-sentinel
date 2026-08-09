from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_demo_events import build_events, write_events


class DemoEventsTest(unittest.TestCase):
    def test_build_events_covers_public_demo_protocols(self) -> None:
        events = build_events(cycles=1)
        protocols = {str(event.get("protocol") or "").lower() for event in events}
        self.assertTrue({"btc", "ble", "ieee802154", "wifi", "fm"}.issubset(protocols))
        self.assertGreaterEqual(len(events), 10)
        self.assertEqual(events, sorted(events, key=lambda item: item["offset_s"]))

    def test_build_events_has_wifi_hub_spoke_demo(self) -> None:
        events = build_events(cycles=1)
        demo_mesh_stations = {
            event.get("source")
            for event in events
            if event.get("protocol") == "wifi"
            and event.get("bssid") == "02:10:7A:00:20:06"
            and event.get("kind") == "data"
        }
        self.assertGreaterEqual(len(demo_mesh_stations), 5)

    def test_build_events_has_weekly_pattern_of_life_demo(self) -> None:
        events = build_events(cycles=1)
        demo_mesh_ap = next(
            event
            for event in events
            if event.get("protocol") == "wifi"
            and event.get("kind") == "probe_response"
            and event.get("ssid") == "DemoMesh-2G"
        )
        demo_beacon = next(event for event in events if event.get("name") == "Demo Beacon-01")
        mesh_offsets = demo_mesh_ap.get("demo_seen_day_offsets")
        beacon_offsets = demo_beacon.get("demo_seen_day_offsets")
        self.assertEqual(mesh_offsets[0], 0)
        self.assertEqual(mesh_offsets[-1], 231)
        self.assertTrue(all(right - left == 7 for left, right in zip(mesh_offsets, mesh_offsets[1:])))
        self.assertEqual(beacon_offsets, [0, 7, 14, 21, 28])

    def test_write_events_creates_jsonl_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "public-demo-events.jsonl"
            manifest = write_events(output, cycles=1)
            self.assertTrue(output.exists())
            self.assertTrue(output.with_name("manifest.json").exists())
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(manifest["event_count"], len(rows))
            self.assertGreater(output.stat().st_size, 0)
            self.assertTrue(manifest["public_safe"])


if __name__ == "__main__":
    unittest.main()
