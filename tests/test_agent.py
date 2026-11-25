"""Operator agent: intent parsing, automatic gain, decoder feedback, and a full run with a fake plugin."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.profiles import PROFILES, IntentError, parse
from rf_platform.agent.radio import Levels, auto_gain, iq_levels, split_gain

DEVICE = {"id": "bladerf:0", "driver": "bladerf", "label": "BladeRF #0", "freq_min_hz": 70_000_000,
          "freq_max_hz": 6_000_000_000, "max_sample_rate_sps": 61_440_000, "occupied": False}


class IntentTest(unittest.TestCase):
    def test_requests_map_to_bounded_tasks(self):
        task = parse("find bluetooth devices")
        self.assertEqual((task.profile.name, task.duration_s), ("ble", 30))
        self.assertEqual(parse("Find BLE beacons for 2 minutes on bladerf:1").duration_s, 120)
        self.assertEqual(parse("find BLE beacons for 2 minutes on bladerf:1").device_id, "bladerf:1")
        zigbee = parse("look for zigbee on channel 25 for 45 seconds")
        self.assertEqual((zigbee.profile.name, zigbee.channel, zigbee.duration_s), ("zigbee", 25, 45))
        self.assertEqual(parse("any tire sensors nearby?").profile.name, "tpms")

    def test_refusals(self):
        for command, message in (("jam the bluetooth", "only receives"), ("transmit on 2.4 GHz", "only receives"),
                                 ("find wifi networks", "monitor-mode"), ("find aliens", "I can look for"),
                                 ("find bluetooth devices for 2 hours", "between 5 and 600"), ("", "Say what")):
            with self.subTest(command=command), self.assertRaisesRegex(IntentError, message):
                parse(command)


class GainTest(unittest.TestCase):
    def test_levels_and_split(self):
        loud = bytes([127, 127] * 1000)
        self.assertTrue(iq_levels(loud, 40).saturated)
        quiet = bytes([2, 0] * 1000)
        self.assertTrue(iq_levels(quiet, 40).weak)
        self.assertEqual(split_gain("bladerf", 46), (0, 46))
        lna, vga = split_gain("hackrf", 46)
        self.assertEqual((lna % 8, lna + vga), (0, 46))

    def test_auto_gain_converges_from_both_sides(self):
        class FakeRadio:
            """Strongest signal peaks at (gain - 40) dBFS; clips above 0 dBFS."""
            def measure(self, device, freq, rate, gain):
                peak = gain - 40.0
                return Levels(gain, peak - 18, min(peak, 3.0), 0.01 if peak > 0 else 0.0)
        for start in (10, 60):
            with self.subTest(start=start):
                log = []
                gain = auto_gain(FakeRadio(), DEVICE, (2_402_000_000,), 4_000_000, start_db=start, log=log.append)
                self.assertTrue(31 <= gain <= 37, (gain, log))
                self.assertTrue(any("adjusting gain" in line for line in log))

    def test_decoder_feedback_rules(self):
        decide = core.Agent._decide
        self.assertIsNone(decide([-10.0] * 5, 40, 0))                       # too few bursts to judge
        lower = decide([0.5] * 8 + [-10.0] * 12, 40, 0)
        self.assertEqual(lower[0], 34)
        raise_ = decide([-25.0] * 20, 40, 0)
        self.assertEqual(raise_[0], 46)
        self.assertIsNone(decide([-10.0] * 20, 40, 0))                      # well placed
        self.assertIsNone(decide([-25.0] * 20, 40, core.MAX_ADJUSTMENTS))   # adjustment budget used
        self.assertIsNone(decide([-25.0] * 20, 60, 0))                      # already at maximum


FAKE_PLUGIN = '''#!/usr/bin/env python3
import json, sys, time
gain = float(sys.argv[sys.argv.index("--vga-gain-db") + 1])
peak = gain - 64.0  # bursts: -24 dBFS at 40 dB (weak), -18 dBFS at 46 dB (acceptable)
for i in range(400):
    print(json.dumps({"kind": "ble_burst", "peak_dbfs": peak}), flush=True)
    if gain >= 46 and i % 40 == 0:
        print(json.dumps({"kind": "ble_adv", "address": "AA:BB:CC:00:00:%02d" % (i // 40 % 3),
                          "identity": "Test Beacon", "rssi_dbfs": -30.0 - i % 7, "seen_at": time.time()}), flush=True)
    time.sleep(0.02)
'''


class AgentRunTest(unittest.TestCase):
    def test_full_run_adjusts_gain_and_collects_detections(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = Path(temp) / "ble_scanner"
            plugin.write_text(FAKE_PLUGIN)
            plugin.chmod(plugin.stat().st_mode | stat.S_IEXEC)
            agent = core.Agent("http://iq.invalid", "")
            seen = []
            with patch.object(core, "plugin_path", return_value=str(plugin)), \
                    patch.object(core, "WINDOW_S", 1.0), \
                    patch.object(agent.radio, "choose", return_value=DEVICE):
                result = agent.run("find bluetooth devices for 6 seconds", on_step=lambda m: None,
                                   on_detection=seen.append, gain_db=40)
        self.assertEqual(result["gains_db"][:2], [40.0, 46.0])
        self.assertEqual(result["adjustments"], 1)
        self.assertEqual({d["key"] for d in result["devices"]}, {"AA:BB:CC:00:00:00", "AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"})
        self.assertTrue(all(d["identity"] == "Test Beacon" for d in result["devices"]))
        self.assertEqual(len(seen), sum(d["count"] for d in result["devices"]))
        self.assertLess(result["elapsed_s"], 15)
        self.assertTrue(any("bursts are weak" in s["message"] for s in result["steps"]))
        # Verifiable report: written, checksummed, reloadable, listed.
        import hashlib
        from rf_platform.agent import report
        self.assertTrue(result["run_id"].startswith("RUN-") and result["receive_only"])
        with tempfile.TemporaryDirectory() as runs:
            folder = report.save(result, Path(runs))
            for line in (folder / "checksums.sha256").read_text().splitlines():
                digest, name = line.split("  ")
                self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(report.load(result["run_id"], Path(runs))["devices"], result["devices"])
            self.assertEqual([r["run_id"] for r in report.list_runs(Path(runs))], [result["run_id"]])
            self.assertIn("Receive-only", (folder / "report.html").read_text())

    def test_plugin_commands_are_receive_only_and_bounded(self):
        for name in PROFILES:
            task = parse({"ble": "find bluetooth", "zigbee": "find zigbee", "tpms": "find tire sensors"}[name])
            with patch.object(core, "plugin_path", return_value="/bin/" + PROFILES[name].plugin):
                command = core.plugin_command(task, DEVICE, 40, "http://127.0.0.1:8790", "tok")
            text = " ".join(command)
            self.assertIn("bladerf:0", text)
            self.assertNotRegex(text, r"\b(tx|transmit|replay)\b")

    def test_missing_plugin_is_reported(self):
        with patch.object(core, "plugin_path", return_value=None), self.assertRaisesRegex(RuntimeError, "not installed"):
            core.Agent("http://iq.invalid", "").run("find zigbee", on_step=lambda m: None)


if __name__ == "__main__":
    unittest.main()
