"""Readiness checks before Run: what a plan needs, probed read-only."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from rf_platform.agent.playbook import plan_any
from rf_platform.agent.readiness import check
from rf_platform.agent.simulate import SimRadio


class DownRadio:
    def devices(self):
        raise RuntimeError("The IQ service at http://x is not running")


class NarrowRadio:
    def devices(self):
        return [{"id": "rtlsdr:0", "freq_min_hz": 24_000_000, "freq_max_hz": 1_766_000_000, "max_sample_rate_sps": 3_200_000}]


WATCH = ("From 10pm to 6am, every 30 minutes, sweep for bluetooth trackers for 1 minute and send an urgent alert when "
         "a new one appears. Then survey cell towers for 1 minute.")


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan = plan_any(WATCH, "deterministic")

    def tearDown(self):
        self.temp.cleanup()

    def by_name(self, out):
        return {c["name"]: c["status"] for c in out["checks"]}

    def test_states(self):
        no_modem = lambda: {"supported": False, "reason": "no survey service"}  # noqa: E731
        out = check(self.plan, DownRadio(), "passive-auto", self.dir, cellular=no_modem)
        self.assertFalse(out["ok"])
        self.assertEqual(self.by_name(out)["IQ service"], "fail")
        self.assertEqual(self.by_name(out)["Cellular modem"], "fail")
        self.assertEqual(self.by_name(out)["Alert delivery"], "warn")  # a long watch with dashboard-only alerts
        out = check(self.plan, NarrowRadio(), "approval-required", self.dir,
                    cellular=lambda: {"supported": True, "reason": "modem ok"})
        self.assertEqual(self.by_name(out)["Bluetooth Low Energy advertisements"], "fail")  # rtl-sdr cannot reach 2.4 GHz
        self.assertEqual(self.by_name(out)["Unattended"], "warn")
        out = check(self.plan, SimRadio(), "passive-auto", self.dir, simulate=True)
        self.assertTrue(out["ok"])
        self.assertEqual(self.by_name(out)["Receivers"], "ok")
        self.assertNotIn("Cell survey length", self.by_name(out))  # simulated modem: no scan timing

    def test_short_cell_survey_warns_about_unnamed_neighbours(self):
        modem = lambda: {"supported": True, "reason": "modem ok"}  # noqa: E731
        short = check(plan_any("survey cell towers for 30 seconds", "deterministic"), SimRadio(), "passive-auto",
                      self.dir, cellular=modem)
        self.assertEqual(self.by_name(short)["Cell survey length"], "warn")
        self.assertTrue(short["ok"])  # a warning, not a failure: serving and neighbour cells are still listed
        long = check(plan_any("survey cell towers for 2 minutes", "deterministic"), SimRadio(), "passive-auto",
                     self.dir, cellular=modem)
        self.assertNotIn("Cell survey length", self.by_name(long))


if __name__ == "__main__":
    unittest.main()
