"""Public copies of run reports: no device addresses, names, cell IDs or serials; everything else intact."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.playbook import plan_playbook_deterministic, run_playbook, save
from rf_platform.agent.publish import MAC, publish, redact


class PublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": cls.temp.name}):
            book = plan_playbook_deterministic("watch for trackers for 6 seconds. then survey cell towers for 5 seconds "
                                               "and alert me if a 2G cell shows up")
            cls.result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None)
        # What a live report also carries: an advertised name, a receiver serial, a cell's ID and TAC.
        cls.result["devices"][0]["name"] = "Pat's AirPods"
        cls.result["steps"].append({"t": 1.0, "message": "Receiver: bladerf:0 (BladeRF #0 [0123abcd..89abcdef])"})
        cls.result["devices"].append({"key": "LTE:18BBA08", "protocol": "cellular", "count": 2, "cell_id": "18BBA08",
                                      "tac": "1613", "identity": "AT&T · LTE · PCI 56", "steps": [2]})
        cls.folder = save(cls.result, Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_identifiers_are_gone_and_the_rest_is_intact(self):
        original_text = (self.folder / "run.json").read_text()
        self.assertTrue(MAC.search(original_text))  # the fixture really holds addresses
        out = publish(self.folder, Path(self.temp.name) / "public")
        text = (out / "run.json").read_text() + (out / "report.html").read_text()
        self.assertIsNone(MAC.search(text))
        for secret in ("Pat's AirPods", "0123abcd", "18BBA08", "1613", "PCI 56"):
            self.assertNotIn(secret, text)
        public = json.loads((out / "run.json").read_text())
        self.assertEqual(public["redaction"]["source_run_json_sha256"],
                         hashlib.sha256((self.folder / "run.json").read_bytes()).hexdigest())
        # Same counts, alerts and narrative shape; pseudonyms are consistent wherever a device appears.
        self.assertEqual(len(public["devices"]), len(self.result["devices"]))
        self.assertEqual(len(public["alerts"]), len(self.result["alerts"]))
        self.assertEqual(public["narrative"][0], self.result["narrative"][0])
        keys = {d["key"] for d in public["devices"]}
        self.assertTrue(all(re.fullmatch(r"(ble|cell|dev)-\d\d", k) for k in keys), keys)
        for alert in public["alerts"]:
            self.assertIn(alert["device"]["key"], keys)
        self.assertIn("AT&T · LTE", [d.get("identity") for d in public["devices"]])  # carriers stay, PCI removed
        sums = dict(reversed(line.split("  ")) for line in (out / "checksums.sha256").read_text().splitlines())
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)

    def test_distinguishing_label_suffix_is_pseudonymised(self):
        result = {"devices": [{"key": "AA:BB:CC:DD:EE:00", "identity": "Apple, Inc."}],
                  "recurring": [{"key": "AA:BB:CC:DD:EE:00", "label": "Apple, Inc. (…EE:00)"}]}
        public, _ = redact(result)
        self.assertEqual(public["recurring"][0]["label"], "Apple, Inc. (ble-01)")


class HostnameTest(unittest.TestCase):
    def test_recording_host_name_is_replaced(self):
        with patch("rf_platform.agent.publish.socket.gethostname", return_value="pats-laptop"):
            public, summary = redact({"devices": [], "steps": [{"message": "unit pats-laptop"}], "unit": "pats-laptop"})
        self.assertNotIn("pats-laptop", json.dumps(public))
        self.assertEqual(public["unit"], "sensor-1")
