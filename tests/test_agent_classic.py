"""Bluetooth Classic in the agent: capability check, BLE fallback, and BLE + Classic on two receivers."""
from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.capabilities import classic_capability
from tests.test_agent import DEVICE, FAKE_PLUGIN

BLADERF_1 = {**DEVICE, "id": "bladerf:1", "label": "BladeRF #1"}
RTLSDR = {"id": "rtlsdr:0", "driver": "rtlsdr", "label": "RTL-SDR", "freq_min_hz": 24_000_000,
          "freq_max_hz": 1_766_000_000, "max_sample_rate_sps": 3_200_000, "occupied": False}
HACKRF_NARROW = {**DEVICE, "id": "airspy:0", "driver": "airspy", "max_sample_rate_sps": 10_000_000}

FAKE_CLASSIC = '''#!/usr/bin/env python3
import json, sys, time
assert sys.argv[1] == "listen" and "--source" in sys.argv
for i in range(200):
    if i % 20 == 0:
        print(json.dumps({"kind": "classic_lap", "protocol": "btc", "type": "lap_initialized",
                          "lap": "9E8B%02X" % (i // 20 % 2), "rssi_dbfs": -40.0, "seen_at": time.time()}), flush=True)
    if i % 50 == 0:
        print(json.dumps({"type": "metrics", "samples_processed": i * 100000, "barker_hits": i * 3,
                          "access_hits": i // 20, "lap_events": i // 20, "resolved_events": 0, "fhs_events": 0}),
              flush=True)
    time.sleep(0.02)
'''


class CapabilityTest(unittest.TestCase):
    def test_reasons_when_unsupported(self):
        self.assertIn("not installed", classic_capability([DEVICE], None)["reason"])
        narrow = classic_capability([RTLSDR, HACKRF_NARROW], "/bin/bluetooth_classic")
        self.assertFalse(narrow["supported"])
        self.assertIn("20 MS/s", narrow["reason"])
        self.assertIn("airspy:0 (max 10 MS/s)", narrow["reason"])
        busy = classic_capability([DEVICE], "/bin/bluetooth_classic", exclude=("bladerf:0",))
        self.assertFalse(busy["supported"])
        self.assertIn("second receiver", busy["reason"])

    def test_picks_a_free_wideband_receiver_other_than_ble(self):
        with patch("rf_platform.agent.capabilities.classic_decoder_binary", return_value=Path("/x/decoder")):
            cap = classic_capability([DEVICE, BLADERF_1], "/bin/bluetooth_classic", exclude=("bladerf:0",))
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["device"]["id"], "bladerf:1")


class AgentClassicRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugins = {}
        for name, source in (("ble_scanner", FAKE_PLUGIN), ("bluetooth_classic", FAKE_CLASSIC)):
            path = Path(self.temp.name) / name
            path.write_text(source)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
            self.plugins[name] = str(path)

    def tearDown(self):
        self.temp.cleanup()

    def run_agent(self, command, devices):
        agent = core.Agent("http://iq.invalid", "")

        def choose(freqs, rate, wanted=None, exclude=()):
            pool = [d for d in devices if d["id"] not in exclude and (not wanted or d["id"] == wanted)
                    and d["max_sample_rate_sps"] >= rate and d["freq_min_hz"] <= min(freqs)
                    and max(freqs) <= d["freq_max_hz"]]
            if not pool:
                raise RuntimeError("no receiver")
            return pool[0]
        steps = []
        with patch.object(core, "plugin_path", side_effect=lambda name: self.plugins.get(name)), \
                patch.object(core, "WINDOW_S", 1.0), \
                patch.object(agent.radio, "choose", side_effect=choose), \
                patch.object(agent.radio, "devices", return_value=devices), \
                patch("rf_platform.agent.capabilities.classic_decoder_binary", return_value=Path("/x/decoder")):
            result = agent.run(command, on_step=steps.append, gain_db=46)
        return result, steps

    def test_bluetooth_request_runs_ble_and_classic_on_two_receivers(self):
        result, steps = self.run_agent("find bluetooth devices for 5 seconds", [DEVICE, BLADERF_1])
        protocols = {d["protocol"] for d in result["devices"]}
        self.assertEqual(protocols, {"ble", "classic"})
        self.assertIn("LAP 9E8B00", {d["key"] for d in result["devices"]})
        self.assertEqual(result["receiver"], "bladerf:0")
        self.assertEqual(result["companion"]["receiver"], "bladerf:1")
        self.assertIsNone(result["companion"]["error"])
        self.assertTrue(result["capabilities"]["classic"]["supported"])
        self.assertTrue(any(s.startswith("[Classic]") for s in steps))
        self.assertGreater(result["decoder_stats"]["classic"]["lap_events"], 0)
        start_scans = [a for a in result["audit"] if a["tool"] == "start_scan"]
        self.assertEqual({a["params"]["device_id"] for a in start_scans}, {"bladerf:0", "bladerf:1"})

    def test_one_receiver_reports_the_limitation_and_runs_ble(self):
        result, steps = self.run_agent("find bluetooth devices for 5 seconds", [DEVICE])
        self.assertEqual({d["protocol"] for d in result["devices"]}, {"ble"})
        self.assertIsNone(result["companion"])
        self.assertFalse(result["capabilities"]["classic"]["supported"])
        self.assertTrue(any("Bluetooth Classic not run" in s and "second receiver" in s for s in steps))

    def test_classic_request_without_wideband_receiver_falls_back_to_ble(self):
        narrow_24 = {**HACKRF_NARROW, "id": "airspy:0"}
        result, steps = self.run_agent("find bluetooth classic devices for 5 seconds", [narrow_24])
        self.assertEqual(result["profile"], "ble")
        self.assertTrue(any("Classic is not available here" in s and "Continuing with BLE" in s for s in steps))


if __name__ == "__main__":
    unittest.main()
