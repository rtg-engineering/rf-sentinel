"""Saved plans: save by name, 'run <name>' plans from the library, re-validated on use, shared over the API."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from rf_platform.agent import library
from rf_platform.agent.playbook import plan_any

WATCH = "From 10pm to 6am, every 30 minutes, sweep for bluetooth trackers for 1 minute and alert me when a new one appears."


class LibraryTest(unittest.TestCase):
    def test_save_find_run_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            planned = plan_any(WATCH, "deterministic", utc_offset_min=-300, library_dir=base)
            library.save("Tonight's watch", planned, WATCH, base)
            for phrase in ("run tonight's watch", "Start the tonights watch playbook", "tonight's watch", "run 'Tonight's watch' now"):
                found = plan_any(phrase, "deterministic", library_dir=base)
                self.assertEqual((found["planner"], found["library"]), ("library", "Tonight's watch"), phrase)
                self.assertEqual(found["playbook"]["repeat"]["times"], 16)
                self.assertTrue(found["explain"].startswith("Starting at 22:00 (UTC-05:00)"))
            self.assertNotEqual(plan_any("find ble devices for 1 minute", "deterministic", library_dir=base)["planner"], "library")
            # A hand-edited entry that breaks the bounds is rejected when used.
            entries = json.loads((base / "library.json").read_text())
            entries[0]["plan"]["playbook"]["steps"][0]["workflow"]["duration_s"] = 99999
            (base / "library.json").write_text(json.dumps(entries))
            with self.assertRaises(ValueError):
                plan_any("run tonight's watch", "deterministic", library_dir=base)
            self.assertTrue(library.delete("tonights watch", base))
            self.assertEqual(library.load(base), [])
            with self.assertRaises(ValueError):
                library.save("bad", {"spec": {"collect": ["ble"], "duration_s": 99999}}, "", base)


class LibraryEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.client = BackendValidationTest, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_endpoints(self):
        planned = self.client.post("/api/agent/plan", json={"text": "find ble devices for 1 minute", "planner": "deterministic"}).get_json()
        ok = self.client.post("/api/agent/library", json={"name": "quick ble", "plan": {"spec": planned["spec"]}, "text": "x"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("quick ble", [e["name"] for e in self.client.get("/api/agent/library").get_json()])
        again = self.client.post("/api/agent/plan", json={"text": "run quick ble", "planner": "deterministic"}).get_json()
        self.assertEqual((again["planner"], again["spec"]["duration_s"]), ("library", 60))
        self.assertEqual(self.client.post("/api/agent/library", json={"name": "x", "plan": {}}).status_code, 400)
        self.assertEqual(self.client.delete("/api/agent/library/quick%20ble").status_code, 200)
        self.assertEqual(self.client.delete("/api/agent/library/quick%20ble").status_code, 404)


if __name__ == "__main__":
    unittest.main()
