"""Discover the airwaves: sweep analysis, band naming, follow-up proposals, and the simulated end-to-end run."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from rf_platform.agent import discover as d
from rf_platform.agent.gateway import AgentGateway
from rf_platform.agent.playbook import plan_any
from rf_platform.agent.simulate import SimRadio


def scene(passes=4):
    out = []
    for k in range(passes):
        out += d.simulated_samples(d.START_HZ, d.STOP_HZ, k)
    return out


class AnalyseTest(unittest.TestCase):
    def test_bands_found_named_and_wide_carriers_kept_whole(self):
        bands = {b["band"]: b for b in d.summarise_bands(d.analyse(scene())["segments"])}
        for name in ("FM broadcast", "2.4 GHz ISM", "Cellular 850 MHz (B5/n5) downlink", "C-band 5G (n77)",
                     "433 MHz ISM", "NOAA Weather Radio"):
            self.assertIn(name, bands)
        cband = bands["C-band 5G (n77)"]  # a 40 MHz carrier spans several 15 MHz steps
        self.assertGreaterEqual(cband["hi_hz"] - cband["lo_hz"], 35_000_000)
        self.assertTrue(bands["FM broadcast"]["steady"])
        self.assertFalse(bands["433 MHz ISM"]["steady"])  # the scripted 433 MHz burst is on every third pass
        self.assertEqual(bands["2.4 GHz ISM"]["decoder"], "ble,classic,zigbee")
        self.assertIsNone(bands["FM broadcast"]["decoder"])

    def test_proposal_is_a_plain_request_the_planner_validates(self):
        text, why = d.propose(d.summarise_bands(d.analyse(scene())["segments"]))
        plan = plan_any(text, "deterministic")
        collected = [s["workflow"]["collect"][0] for s in plan["playbook"]["steps"]]
        self.assertEqual(collected, ["ble", "classic", "zigbee", "tpms", "cellular"])
        self.assertTrue(any(w.startswith("Zigbee: energy in 2.4 GHz ISM") for w in why))

    def test_quiet_spectrum_proposes_nothing(self):
        quiet = [{"hz_low": 100_000_000 + i * 15_000_000, "hz_high": 114_900_000 + i * 15_000_000,
                  "db_values": [-95.0 + (j % 3) * 0.3 for j in range(150)]} for i in range(10)]
        self.assertEqual(d.propose(d.summarise_bands(d.analyse(quiet)["segments"])), (None, []))

    def test_requests_route_to_discovery(self):
        for text in ("what's on the air around me?", "discover the airwaves for 2 minutes", "sweep the whole spectrum"):
            self.assertIn("discovery", plan_any(text, "deterministic"), text)
        self.assertEqual(plan_any("discover the airwaves for 2 minutes", "deterministic")["discovery"]["duration_s"], 120)
        self.assertIn("spec", plan_any("find ble devices for 30 seconds", "deterministic"))
        with self.assertRaises(ValueError):
            plan_any("discover the airwaves for 2 hours", "deterministic")


class RunTest(unittest.TestCase):
    def test_simulated_discovery_end_to_end_and_observe_denies(self):
        with patch.object(d, "MIN_S", 9), patch("rf_platform.agent.simulate.SimRadio.sweep_samples",
                                                  lambda self, sid: scene(3)):
            gateway = AgentGateway("passive-auto", "run-test")
            result = d.discover(SimRadio(), gateway, 9, simulated=True)
            self.assertTrue(result["analysis"]["passes"] >= 3)
            self.assertIn("Proposed next:", result["narrative"][-1])
            self.assertEqual(gateway.audit[0]["tool"], "spectrum_sweep")
            from rf_platform.agent.gateway import PermissionDenied
            with self.assertRaises(PermissionDenied):
                d.discover(SimRadio(), AgentGateway("observe", "run-x"), 9, simulated=True)
        html = d.render({**result, "run_id": "RUN-x", "started_at": "now"})
        self.assertIn("SIMULATED DISCOVERY", html)
        self.assertIn("<polyline", html)


if __name__ == "__main__":
    unittest.main()
