"""Cellular survey through a modem: normalisation, capability, agent workflow, dashboard ingest."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.workflow import plan_deterministic, run_workflow
from rf_platform.cellular.survey import cellular_capability, normalise


def observation(rat, plmn, cell_id, role="scanned", pci=1, rsrp=-90.0):
    return {"role": role, "source": "AT+QSCAN=3,1" if role == "scanned" else 'AT+QENG="servingcell"',
            "identity": {"rat": rat, "plmn": plmn, "mcc": plmn[:3] if plmn else None, "mnc": plmn[3:] if plmn else None,
                         "cell_id": cell_id, "pci": pci, "nr_arfcn": 633408, "tac": "10301E"},
            "signal": {"rsrp_dbm": rsrp, "rsrq_db": -10.0, "sinr_db": 8.0}, "timestamp": "2026-11-26T03:15:25Z"}


class NormaliseTest(unittest.TestCase):
    def test_operators_and_plmn_verification(self):
        known: dict = {}
        scanned = normalise(observation("NR5G", "310410", "5D156681C"), known)
        self.assertEqual((scanned["operator"], scanned["rat"], scanned["generation"]), ("AT&T", "5G NR", "5G"))
        shared = normalise(observation("NR5G", "313100", "5D156681C"), known)
        self.assertEqual(shared["key"], scanned["key"])  # one physical cell listing two PLMNs
        # The serving-cell report mis-parses this cell's PLMN; the scan of the same cell wins.
        serving = normalise(observation("NR5G", "31077", "5D156681C", role="serving"), known)
        self.assertEqual((serving["plmn"], serving["operator"], serving["plmn_verified"]), ("310410", "AT&T", True))
        unseen = normalise(observation("LTE", "31077", "ABC", role="serving"), known)
        self.assertEqual((unseen["operator"], unseen["plmn_verified"]), ("unverified PLMN 310-77", False))
        self.assertEqual(normalise(observation("LTE", "", "", role="neighbour", pci=7), known)["operator"],
                         "PLMN not reported")
        self.assertEqual(normalise(observation("GSM", "999990", "1"), known)["operator"], "unknown operator")
        self.assertEqual(normalise(observation("GSM", "999990", "1"), known)["generation"], "2G")

    def test_capability(self):
        with patch("rf_platform.cellular.survey.requests.get", side_effect=OSError("refused")):
            cap = cellular_capability("http://survey.invalid")
        self.assertFalse(cap["supported"])
        self.assertIn("no cellular survey service", cap["reason"])

        class Response:
            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data
        replies = {"/api/health": {"ok": True, "unit": "test"},
                   "/api/ports": {"ports": [{"port": "/dev/ttyUSB3", "ok": True}], "active_port": "/dev/ttyUSB3"}}
        with patch("rf_platform.cellular.survey.requests.get",
                   side_effect=lambda url, timeout: Response(replies[url.split("5098")[1]])):
            cap = cellular_capability("http://127.0.0.1:5098")
        self.assertTrue(cap["supported"])
        self.assertIn("with a SIM inserted the modem registers", cap["reason"])


FAKE_ADAPTER = '''#!/usr/bin/env python3
import json, time
cells = [("5G NR", "310410", "AT&T", "5G NR:A1", "5G"), ("LTE", "999990", "unknown operator", "LTE:B2", "4G"),
         ("2G GSM", "310260", "T-Mobile", "2G GSM:C3", "2G")]
for i in range(60):
    rat, plmn, op, key, gen = cells[i % 3]
    print(json.dumps({"kind": "cell", "protocol": "cellular", "key": key, "role": "scanned", "rat": rat, "plmn": plmn,
                      "operator": op, "generation": gen, "rssi_dbfs": -90.0, "rsrp_dbm": -90.0,
                      "identity": op + " · " + rat, "seen_at": time.time()}), flush=True)
    time.sleep(0.05)
'''


class CellularWorkflowTest(unittest.TestCase):
    def test_unknown_network_and_2g_alerts(self):
        spec = plan_deterministic("survey cell towers for 6 seconds. alert me if an unknown network appears or if a 2G cell shows up")
        self.assertEqual(spec.collect, ["cellular"])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp}):
            adapter = Path(temp) / "adapter"
            adapter.write_text(FAKE_ADAPTER)
            adapter.chmod(adapter.stat().st_mode | stat.S_IEXEC)
            agent = core.Agent("http://iq.invalid", "")
            cap = {"supported": True, "url": "http://survey.invalid", "port": "/dev/ttyUSB3", "reason": "modem ok"}
            with patch("rf_platform.cellular.survey.cellular_capability", return_value=cap), \
                    patch.object(core, "plugin_command", return_value=[sys.executable, str(adapter)]):
                result = run_workflow(agent, spec, on_step=lambda m: None)
        self.assertEqual(result["receiver"], "cellmodem:0")
        self.assertEqual(result["gains_db"], [])
        self.assertEqual({d["key"] for d in result["devices"]}, {"5G NR:A1", "LTE:B2", "2G GSM:C3"})
        alerted = {a["device"]["key"] for a in result["workflow"]["alerts"]}
        self.assertEqual(alerted, {"LTE:B2", "2G GSM:C3"})
        self.assertIn("start_scan", [a["tool"] for a in result["audit"]])

    def test_planner_phrasing(self):
        spec = plan_deterministic("Survey cell towers for 3 minutes. Send an urgent alert if an unknown network appears "
                                  "or if a 2G cell shows up, and alert me when the serving cell changes.")
        self.assertEqual((spec.collect, spec.duration_s), (["cellular"], 180))
        self.assertEqual([(t.when[0].field, t.when[0].value, t.do[0].severity) for t in spec.triggers],
                         [("operator", "unknown operator", "critical"), ("generation", "2G", "critical"),
                          ("role", "serving", "warning")])
        spec = plan_deterministic("watch cellular for 5 minutes, only verizon")
        self.assertEqual((spec.filters[0].field, spec.filters[0].value), ("operator", "Verizon"))

    def test_no_modem_is_reported(self):
        agent = core.Agent("http://iq.invalid", "")
        cap = {"supported": False, "reason": "no cellular survey service at http://x"}
        with patch("rf_platform.cellular.survey.cellular_capability", return_value=cap), \
                self.assertRaisesRegex(RuntimeError, "Cellular survey is not available"):
            agent.run("survey cell towers for 10 seconds", on_step=lambda m: None)


class DashboardIngestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend = BackendValidationTest, BackendValidationTest.backend

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_cell_becomes_cellular_entity(self):
        event = normalise(observation("NR5G", "310410", "5D156681C"), {})
        rows = self.backend._scanner_json_to_events("agent:cellular", event)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["kind"], row["protocol"], row["link"]), ("cellular_signal", "CELLULAR", event["key"]))
        self.assertIn("AT&T", row["detail"])
        self.assertIn("RSRP -90 dBm", row["detail"])
        self.assertIsNone(row["rssi_dbfs"])  # modem dBm stays out of SDR dBFS health


if __name__ == "__main__":
    unittest.main()
