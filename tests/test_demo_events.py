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
