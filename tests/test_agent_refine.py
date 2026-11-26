"""Refining a plan in plain words: targeted edits, added steps and schedules, model path with fallback, API."""
from __future__ import annotations

import unittest

from rf_platform.agent.playbook import plan_any
from rf_platform.agent.refine import refine

TRACKERS = ("Watch for bluetooth trackers for 5 minutes, only stronger than -60. If a tracker is seen for more than "
            "2 minutes, send an urgent alert and record 5 seconds of IQ.")


def rules(current, instruction):
    return refine(current, instruction, "", "deterministic")


class RulesRefineTest(unittest.TestCase):
    def setUp(self):
        self.base = plan_any(TRACKERS, "deterministic")

    def test_edits(self):
        self.assertEqual(rules(self.base, "make it 10 minutes")["spec"]["duration_s"], 600)
        self.assertEqual(rules(self.base, "10 minutes")["spec"]["duration_s"], 600)
        added = rules(self.base, "also alert me if an airtag appears")
        self.assertEqual(added["changes"], ['+ when identity contains "AirTag" → alert (warning)'])
        self.assertEqual(len(added["spec"]["triggers"]), 2)
        self.assertEqual(rules(self.base, "drop the signal filter")["changes"], ["− keep signal ≥ -60 dBFS"])
        no_iq = rules(self.base, "don't record IQ")["spec"]["triggers"][0]["do"]
        self.assertEqual([a["kind"] for a in no_iq], ["alert"])  # only that action goes; the rule stays
        self.assertIn('+ keep manufacturer contains "Apple"', rules(self.base, "only apple devices")["changes"])

    def test_steps_and_schedule_keep_earlier_refinements(self):
        plan = rules(self.base, "also alert me if an airtag appears")
        plan = rules(plan, "drop the signal filter")
        plan = rules(plan, "then survey cell towers for 1 minute and alert me if a 2G cell shows up")
        self.assertEqual(plan["changes"], ['+ step 2: find Cellular (modem) for 60 s',
                                           '+ step 2: when generation is "2G" → alert (warning)'])
        first = plan["playbook"]["steps"][0]["workflow"]
        self.assertEqual(len(first["triggers"]), 2)              # the AirTag rule is still there
        self.assertNotIn("rssi_dbfs", [c["field"] for c in first["filters"]])  # and the filter is still gone
        plan = rules(plan, "repeat every 15 minutes for 2 hours")
        self.assertEqual((plan["playbook"]["repeat"]["every_s"], plan["playbook"]["repeat"]["times"]), (900, 8))
        plan = rules(plan, "also alert me if an unknown network appears")  # goes to the cellular step
        self.assertEqual(plan["changes"], ['+ step 2: when operator is "unknown operator" → alert (warning)'])
        plan = rules(plan, "make step 1 2 minutes")
        self.assertEqual(plan["playbook"]["steps"][0]["workflow"]["duration_s"], 120)

    def test_follow_up_refers_back(self):
        base = plan_any("find ble devices for 1 minute and alert me if an airtag appears", "deterministic")
        plan = rules(base, "then watch the flagged ones for 2 minutes and record 3 seconds of IQ")
        step = plan["playbook"]["steps"][1]
        self.assertEqual((step["focus"], step["workflow"]["triggers"][0]["do"][0]["kind"]), ("alerted", "record_iq"))

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "nothing in the plan matches"):
            rules(self.base, "remove the banana rule")
        with self.assertRaisesRegex(ValueError, "only receives"):
            rules(self.base, "and jam them")
        with self.assertRaisesRegex(ValueError, "at most 1800 s|600 seconds"):
            rules(self.base, "make it 20 minutes")


class ModelRefineTest(unittest.TestCase):
    def test_model_result_is_validated_and_falls_back(self):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel
        base = plan_any(TRACKERS, "deterministic")

        def longer(messages, info):
            spec = dict(base["spec"], duration_s=420)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, spec)])
        out = refine(base, "make it 7 minutes", "", "model", model=FunctionModel(longer))
        self.assertEqual((out["planner"], out["spec"]["duration_s"]), ("model", 420))
        self.assertEqual(out["changes"], ["+ find BLE for 420 s", "− find BLE for 300 s"])

        def broken(messages, info):
            raise RuntimeError("model unavailable")
        with self.assertRaisesRegex(ValueError, "600 seconds|at most"):
            refine(base, "make it 20 minutes", "", "model", model=FunctionModel(longer))  # refused, not clamped
        out = refine(base, "make it 7 minutes", "", "model", model=FunctionModel(broken))
        self.assertEqual((out["planner"], out["spec"]["duration_s"]), ("deterministic", 420))
        self.assertIn("model unavailable", out["fallback_reason"])


class RefineEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.client = BackendValidationTest, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_endpoint(self):
        base = self.client.post("/api/agent/plan", json={"text": TRACKERS, "planner": "deterministic"}).get_json()
        ok = self.client.post("/api/agent/refine", json={"current": base, "instruction": "also alert me if an airtag appears",
                                                         "planner": "deterministic"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.get_json()["refined"])
        bad = self.client.post("/api/agent/refine", json={"current": base, "instruction": "remove the banana rule",
                                                          "planner": "deterministic"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/api/agent/refine", json={"instruction": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
