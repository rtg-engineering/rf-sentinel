"""Asking about a finished run: code answers from the run's records; model answers are checked against them."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.ask import ask, device_table
from rf_platform.agent.playbook import plan_playbook_deterministic, run_playbook


class AskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": cls.temp.name}):
            book = plan_playbook_deterministic("watch for trackers for 6 seconds. if you find any, watch them for 5 seconds "
                                               "and send an urgent alert if one is seen 2 times. then survey cell towers "
                                               "for 5 seconds and alert me if a 2G cell shows up")
            cls.result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_code_answers(self):
        keys = {d["key"] for d in device_table(self.result)}
        alerts = ask(self.result, "what alerted?", "deterministic")
        self.assertEqual(alerts["by"], "rules")
        self.assertIn("critical", alerts["answer"])
        self.assertTrue(set(alerts["devices"]) <= keys)
        self.assertIn("more than one step", ask(self.result, "which devices stayed around in both steps?", "deterministic")["answer"])
        self.assertIn("tracker(s)", ask(self.result, "any trackers?", "deterministic")["answer"])
        self.assertIn("2G GSM", ask(self.result, "which cells did you see?", "deterministic")["answer"])
        self.assertIn("I can answer", ask(self.result, "what is the meaning of life", "deterministic")["answer"])

    def test_model_answers_are_checked(self):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel
        tracker = next(d for d in device_table(self.result) if d["kind"] == "tracker")

        def honest(messages, info):
            if len(messages) == 1:
                return ModelResponse(parts=[ToolCallPart("list_devices", {"kind": "tracker"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "answer": f"{tracker['label']} was seen {tracker['count']} times.", "devices": [tracker["key"]]})])
        out = ask(self.result, "how often was the tracker seen?", "model", model=FunctionModel(honest))
        self.assertEqual((out["by"], out["checked"]), ("model", True))

        def invents_number(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "answer": f"{tracker['label']} was seen 987654 times.", "devices": [tracker["key"]]})])
        out = ask(self.result, "what alerted?", "model", model=FunctionModel(invents_number))
        self.assertEqual(out["by"], "rules")
        self.assertIn("987654", out["fallback_reason"])

        def invents_device(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "A drone.", "devices": ["DE:AD:BE:EF:00:00"]})])
        out = ask(self.result, "what alerted?", "model", model=FunctionModel(invents_device))
        self.assertIn("never recorded", out["fallback_reason"])


class AskEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_saved_run(self):
        from rf_platform.agent.report import save
        from rf_platform.agent.workflow import plan_deterministic, run_workflow
        result = run_workflow(core.Agent(simulate=True), plan_deterministic("find ble devices for 5 seconds"), on_step=lambda m: None)
        save(result, self.backend.RF_SENTINEL_LOG_DIR / "agent-runs")
        ok = self.client.post(f"/api/agent/jobs/saved-{result['run_id']}/ask", json={"question": "what was strongest?",
                                                                                     "planner": "deterministic"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("Strongest", ok.get_json()["answer"])
        self.assertEqual(self.client.post("/api/agent/jobs/saved-RUN-000000000000/ask", json={"question": "x"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
