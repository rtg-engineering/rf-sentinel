"""The plan explanation is written by code from the validated plan; it must say what will actually happen."""
from __future__ import annotations

import unittest

from rf_platform.agent.explain import explain
from rf_platform.agent.playbook import plan_any


def say(text: str) -> str:
    return explain(plan_any(text, "deterministic", utc_offset_min=-300))


class ExplainTest(unittest.TestCase):
    def test_workflow(self):
        text = say("Watch for bluetooth trackers for 5 minutes, only stronger than -60. If a tracker is seen for more "
                   "than 2 minutes, send an urgent alert and record 5 seconds of IQ.")
        self.assertEqual(text, "I'll listen to Bluetooth LE for 5 minutes, keeping only trackers that are stronger than "
                               "-60 dBFS. If a device is a tracker and stays around for 2 minutes or more, I'll send you an "
                               "urgent alert and record 5 seconds of IQ as evidence.")
        self.assertIn("As soon as anything shows up, I'll stop.", say("monitor zigbee for 2 minutes and stop when anything shows up"))
        self.assertIn("If a cell is on an unknown network, I'll send you an urgent alert.",
                      say("survey cell towers for 1 minute. send an urgent alert if an unknown network appears"))

    def test_playbook_schedule_gate_focus(self):
        text = say("From 10pm to 6am, every 30 minutes, sweep for bluetooth trackers for 1 minute. If you find any, "
                   "watch them for 2 minutes and record 3 seconds of IQ.")
        self.assertTrue(text.startswith("Starting at 22:00 (UTC-05:00), every 30 min for 16 cycles"))
        self.assertIn("If step 1 found anything, I'll follow the devices step 1 found on Bluetooth LE for 2 minutes.", text)
        self.assertIn("For every device it keeps, I'll record 3 seconds of IQ as evidence.", text)


if __name__ == "__main__":
    unittest.main()
