"""Agent workflows: planning (rules and model), filters, triggers, actions, and policy on actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.gateway import AgentGateway, ApprovalBroker
from rf_platform.agent.workflow import (Action, Condition, Trigger, WorkflowEngine, WorkflowSpec, plan,
                                        plan_deterministic, run_workflow)
from tests.test_agent import DEVICE, FAKE_PLUGIN
import importlib.util

HAS_PYDANTIC_AI = importlib.util.find_spec("pydantic_ai") is not None

TRACKERS = ("Watch for bluetooth trackers for 5 minutes, only stronger than -60. If a tracker is seen for more "
            "than 2 minutes, send an urgent alert and record 5 seconds of IQ. If an AirTag appears, scan "
            "bluetooth classic for 60 seconds.")


class PlannerTest(unittest.TestCase):
    def test_find_filter_when_do(self):
        spec = plan_deterministic(TRACKERS)
        self.assertEqual((spec.collect, spec.duration_s), (["ble"], 300))
        self.assertEqual({(c.field, c.op, c.value) for c in spec.filters},
                         {("rssi_dbfs", "ge", -60.0), ("device_class", "eq", "tracker")})
        first, second = spec.triggers
        self.assertIn(("dwell_s", "ge", 120.0), {(c.field, c.op, c.value) for c in first.when})
        self.assertEqual([(a.kind, a.severity if a.kind == "alert" else a.seconds) for a in first.do],
                         [("alert", "critical"), ("record_iq", 5)])
        self.assertEqual((second.do[0].kind, second.do[0].protocol, second.do[0].seconds), ("start_scan", "classic", 60))
        self.assertEqual(spec.command(), "find ble devices for 300 seconds")

    def test_phrasings(self):
        spec = plan_deterministic("monitor zigbee for 10 minutes and stop when anything shows up")
        self.assertEqual((spec.collect, [a.kind for a in spec.triggers[0].do]), (["zigbee"], ["stop"]))
        spec = plan_deterministic("alert me if a new device stronger than -40 shows up in the next 3 minutes")
        self.assertEqual((spec.collect, spec.duration_s), (["ble", "classic"], 180))
        self.assertEqual({c.field for c in spec.triggers[0].when}, {"rssi_dbfs", "new"})
        spec = plan_deterministic("look for apple devices for 90 seconds; whenever a phone is seen 5 times, "
                                  "alert me 'phone nearby' and keep watching 60 seconds")
        trigger = spec.triggers[0]
        self.assertTrue(trigger.repeat)
        self.assertEqual([a.kind for a in trigger.do], ["alert", "extend"])
        self.assertEqual(trigger.do[0].message, "phone nearby")
        self.assertEqual(spec.filters[0].value, "Apple")
        self.assertEqual(plan_deterministic("find bluetooth devices").triggers, [])

    def test_refusals_and_bounds(self):
        for text in ("jam the airtag", "replay that packet", "page the headset"):
            with self.assertRaisesRegex(ValueError, "only receives"):
                plan_deterministic(text)
        with self.assertRaises(ValueError):
            WorkflowSpec(collect=["ble"], duration_s=5000)
        with self.assertRaises(ValueError):
            WorkflowSpec(collect=["zigbee", "tpms"])
        self.assertEqual(Action(kind="record_iq", seconds=999).seconds, 10)  # bounded, whatever the planner says
        self.assertEqual(Action(kind="start_scan", protocol="classic", seconds=9999).seconds, 300)
        with self.assertRaises(ValueError):
            Condition(field="rssi_dbfs", op="contains", value="x")
        with self.assertRaises(ValueError):
            Action(kind="transmit")  # not an action at all


class ModelPlannerTest(unittest.TestCase):
    @unittest.skipUnless(HAS_PYDANTIC_AI, "pydantic-ai is optional (pip install '.[analyst-llm]')")
    def test_model_plan_is_validated_and_falls_back(self):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        def good(messages, info):
            spec = {"goal": "trackers", "collect": ["ble"], "duration_s": 120,
                    "filters": [{"field": "device_class", "op": "eq", "value": "tracker"}],
                    "triggers": [{"when": [{"field": "dwell_s", "op": "ge", "value": 60}],
                                  "do": [{"kind": "alert", "severity": "critical"}, {"kind": "record_iq", "seconds": 60}]}]}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, spec)])

        planned = plan("watch for trackers that follow me", "model", model=FunctionModel(good))
        self.assertEqual(planned["planner"], "model")
        self.assertEqual(planned["spec"]["triggers"][0]["do"][1]["seconds"], 10)  # clamped by the schema

        def broken(messages, info):
            raise RuntimeError("model unavailable")
        planned = plan("find bluetooth devices for 2 minutes", "model", model=FunctionModel(broken))
        self.assertEqual((planned["planner"], planned["spec"]["duration_s"]), ("deterministic", 120))
        self.assertIn("model unavailable", planned["fallback_reason"])
        with self.assertRaisesRegex(ValueError, "only receives"):
            plan("jam everything", "model", model=FunctionModel(good))
        # Unsupported sources are refused deterministically too, before the model is asked.
        from rf_platform.agent.workflow import _plan_with_model
        with self.assertRaisesRegex(ValueError, "WiFi"):
            _plan_with_model("watch wifi for 2 minutes", FunctionModel(good))


class FakeAgent:
    def __init__(self, mode="passive-auto", broker=None):
        self.gateway = AgentGateway(mode, "run-wf", broker or ApprovalBroker())
        self._stop = threading.Event()
        self.gateway.stop = self._stop
        self.extended, self.finished = [], None
        self.radio = type("R", (), {"choose": staticmethod(lambda freqs, rate, *a, **k: {**DEVICE, "id": "bladerf:1"}),
                                    "base_url": "http://iq.invalid", "token": "", "headers": {}})()

    def extend(self, seconds, limit_s):
        self.extended.append(seconds)
        return seconds

    def finish_early(self, reason):
        self.finished = reason


def detection(key, label="AirTag", rssi=-50.0, **extra):
    return {"agent_key": key, "agent_profile": "ble", "identity": label, "address": key, "rssi_dbfs": rssi,
            "channel": 37, **extra}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def engine(self, spec, agent=None):
        steps = []
        engine = WorkflowEngine(spec, agent or FakeAgent(), steps.append, evidence_dir=Path(self.temp.name) / "ev")
        return engine, steps

    def test_filters_triggers_and_actions(self):
        spec = WorkflowSpec(collect=["ble"], duration_s=60,
                            filters=[Condition(field="rssi_dbfs", op="ge", value=-70)],
                            triggers=[Trigger(when=[Condition(field="device_class", op="eq", value="tracker")],
                                              do=[Action(kind="alert", message="tracker"), Action(kind="tag", label="tracker"),
                                                  Action(kind="extend", seconds=30)]),
                                      Trigger(when=[Condition(field="count", op="ge", value=3)], do=[Action(kind="stop")])])
        agent = FakeAgent()
        engine, steps = self.engine(spec, agent)
        engine.observe(detection("AA", rssi=-90))            # filtered out: too weak
        for _ in range(3):
            engine.observe(detection("BB", rssi=-40))        # tracker: alert+tag+extend once; count 3 -> stop
        engine.observe(detection("CC", label="Pixel 9", rssi=-45))
        out = engine.finish()
        self.assertEqual({d["key"] for d in out["matched"]}, {"BB", "CC"})
        self.assertEqual(out["triggers_fired"], 2)
        self.assertEqual([a["message"] for a in out["alerts"]], ["tracker"])
        self.assertEqual(engine.devices["BB"]["tags"], ["tracker"])
        self.assertEqual(agent.extended, [30.0])
        self.assertIn("AirTag", agent.finished)
        tools = [a["tool"] for a in agent.gateway.audit]
        self.assertEqual(tools.count("raise_alert"), 1)
        self.assertIn("extend_scan", tools)
        # The second run sees BB as known, not new.
        engine2, _ = self.engine(WorkflowSpec(collect=["ble"], filters=[Condition(field="new", op="eq", value=True)]))
        engine2.observe(detection("BB"))
        engine2.observe(detection("DD"))
        self.assertEqual({d["key"] for d in engine2.finish()["matched"]}, {"DD"})

    def test_repeat_respects_cooldown(self):
        spec = WorkflowSpec(collect=["ble"], triggers=[Trigger(when=[Condition(field="count", op="ge", value=1)],
                                                               do=[Action(kind="alert")], repeat=True, cooldown_s=0.2)])
        engine, _ = self.engine(spec)
        engine.observe(detection("BB"))
        engine.observe(detection("BB"))
        time.sleep(0.25)
        engine.observe(detection("BB"))
        self.assertEqual(len(engine.finish()["alerts"]), 2)

    def test_record_iq_writes_verifiable_evidence(self):
        spec = WorkflowSpec(collect=["ble"], triggers=[Trigger(when=[Condition(field="label", op="contains", value="airtag")],
                                                               do=[Action(kind="record_iq", seconds=2)])])
        engine, steps = self.engine(spec)
        with patch("rf_platform.agent.radio.capture_iq", return_value=b"\x01\x02" * 4000) as capture:
            engine.observe(detection("BB"))
            out = engine.finish()
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[2], 2_402_000_000)  # the detection's own advertising channel
        action = next(e for e in out["timeline"] if e["kind"] == "action")
        self.assertEqual(action["status"], "done")
        meta = json.loads(Path(action["detail"]).with_suffix(".json").read_text())
        import hashlib
        self.assertEqual(meta["sha256"], hashlib.sha256(Path(action["detail"]).read_bytes()).hexdigest())
        self.assertEqual(meta["trigger_device"]["key"], "BB")

    def test_policy_applies_to_trigger_actions(self):
        spec = WorkflowSpec(collect=["ble"], triggers=[Trigger(when=[Condition(field="count", op="ge", value=1)],
                                                               do=[Action(kind="alert"), Action(kind="record_iq")])])
        agent = FakeAgent(mode="observe")
        engine, steps = self.engine(spec, agent)
        with patch("rf_platform.agent.radio.capture_iq") as capture:
            engine.observe(detection("BB"))
            out = engine.finish()
        capture.assert_not_called()
        statuses = {e["action"]: e["status"] for e in out["timeline"] if e["kind"] == "action"}
        self.assertEqual(statuses, {"alert": "done", "record_iq": "denied"})
        # approval-required: the capture waits for a named operator
        broker = ApprovalBroker()
        agent = FakeAgent(mode="approval-required", broker=broker)
        engine, _ = self.engine(spec, agent)

        def operator():
            for _ in range(300):
                for p in broker.pending("run-wf"):
                    broker.resolve(p["action_id"], True, "ops-lead", "ok")
                    return
                time.sleep(0.01)
        threading.Thread(target=operator, daemon=True).start()
        with patch("rf_platform.agent.radio.capture_iq", return_value=b"\0\0" * 100):
            engine.observe(detection("CC"))
            out = engine.finish()
        self.assertIn(("record_iq", "approved"), [(a["tool"], a["decision"]) for a in agent.gateway.audit])


class WorkflowRunTest(unittest.TestCase):
    def test_end_to_end_with_fake_plugin(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp}):
            plugin = Path(temp) / "ble_scanner"
            plugin.write_text(FAKE_PLUGIN)
            plugin.chmod(plugin.stat().st_mode | stat.S_IEXEC)
            agent = core.Agent("http://iq.invalid", "")
            spec = plan_deterministic("find ble devices for 20 seconds. when a device is seen 2 times, alert me "
                                      "'test beacon' and tag it as beacon. when anything is seen 3 times, stop")
            events = []
            with patch.object(core, "plugin_path", return_value=str(plugin)), patch.object(core, "WINDOW_S", 1.0), \
                    patch.object(agent.radio, "choose", return_value=DEVICE), \
                    patch.object(agent.radio, "devices", return_value=[DEVICE]):
                started = time.monotonic()
                result = run_workflow(agent, spec, on_step=lambda m: None, on_event=events.append, gain_db=46)
        workflow = result["workflow"]
        self.assertLess(time.monotonic() - started, 12)  # the stop trigger ended a 20 s run early
        self.assertIn("workflow trigger", result["stop_reason"])
        self.assertEqual(len(workflow["alerts"]), 3)       # once per device, three test beacons
        self.assertTrue(all(d.get("tags") == ["beacon"] for d in result["devices"]))
        self.assertIn("trigger", {e["kind"] for e in events})


class PlanEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.client = BackendValidationTest, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_plan_endpoint(self):
        ok = self.client.post("/api/agent/plan", json={"text": TRACKERS, "planner": "deterministic"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.get_json()["summary"]["triggers"]), 2)
        self.assertEqual(self.client.post("/api/agent/plan", json={"text": "jam it"}).status_code, 400)
        bad = {"collect": ["ble"], "duration_s": 99999}
        self.assertEqual(self.client.post("/api/agent/run", json={"workflow": bad}).status_code, 400)  # re-validated


if __name__ == "__main__":
    unittest.main()


class SimulationTest(unittest.TestCase):
    """Simulated receivers drive the real agent, workflow engine, gateway and reports, and say so."""

    def test_simulated_tracker_and_rogue_cell_scenarios(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp}):
            agent = core.Agent(simulate=True)
            spec = plan_deterministic("find ble devices for 12 seconds. if an airtag is seen for more than 4 seconds, "
                                      "send an urgent alert and tag it as follower")
            result = run_workflow(agent, spec, on_step=lambda m: None)
            self.assertTrue(result["simulated"])
            self.assertEqual(result["receiver"], "sim:0")
            alert = result["workflow"]["alerts"][0]
            self.assertEqual((alert["severity"], alert["device"]["label"]), ("critical", "AirTag"))
            from rf_platform.agent.report import _render
            self.assertIn("SIMULATED RUN", _render(result))
            cells = run_workflow(core.Agent(simulate=True), plan_deterministic(
                "survey cell towers for 14 seconds. send an urgent alert if an unknown network appears or if a 2G cell shows up"),
                on_step=lambda m: None)
            alerts = cells["workflow"]["alerts"]
            self.assertEqual(len(alerts), 1)  # one rogue cell, two matching rules: one merged alert
            self.assertEqual(len(alerts[0]["reasons"]), 2)
            self.assertEqual(alerts[0]["device"]["key"], "2G GSM:0F00001")

    def test_companion_gets_the_second_simulated_receiver(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp}):
            result = core.Agent(simulate=True).run("find bluetooth devices for 5 seconds", on_step=lambda m: None)
        self.assertEqual((result["receiver"], result["companion"]["receiver"]), ("sim:0", "sim:1"))
        self.assertEqual({d["protocol"] for d in result["devices"]}, {"ble", "classic"})


class MixedSourceTest(unittest.TestCase):
    MIXED = ("watch for trackers and rogue cell towers for 14 seconds. if an airtag is seen for more than 4 seconds, "
             "send an urgent alert. send an urgent alert if an unknown network appears or if a 2G cell shows up")

    def test_planning_and_bounds(self):
        spec = plan_deterministic(self.MIXED)
        self.assertEqual(spec.collect, ["ble", "cellular"])
        self.assertEqual([t.when[0].field for t in spec.triggers], ["label", "operator", "generation"])
        self.assertTrue(all(t.do[0].severity == "critical" for t in spec.triggers))
        self.assertEqual(plan_deterministic("find zigbee and lte cells for 60 seconds").collect, ["zigbee", "cellular"])
        with self.assertRaises(ValueError):
            WorkflowSpec(collect=["zigbee", "tpms", "cellular"])  # still one SDR group

    def test_filters_are_scoped_per_source(self):
        spec = WorkflowSpec(collect=["ble", "cellular"], filters=[
            Condition(field="device_class", op="eq", value="tracker"), Condition(field="rssi_dbfs", op="ge", value=-60),
            Condition(field="operator", op="contains", value="Verizon")])
        engine = WorkflowEngine(spec, FakeAgent(), lambda m: None, evidence_dir=Path(tempfile.mkdtemp()))
        engine.observe(detection("AT", label="AirTag", rssi=-50))                       # tracker, strong: kept
        engine.observe(detection("PH", label="Pixel", rssi=-50))                        # not a tracker: dropped
        engine.observe({"agent_key": "LTE:1", "agent_profile": "cellular", "identity": "Verizon · LTE",
                        "operator": "Verizon", "rat": "LTE", "rssi_dbfs": -95.0})       # dBm cell, Verizon: kept
        engine.observe({"agent_key": "LTE:2", "agent_profile": "cellular", "identity": "AT&T · LTE",
                        "operator": "AT&T", "rat": "LTE", "rssi_dbfs": -80.0})          # not Verizon: dropped
        self.assertEqual(engine.matched, {"AT", "LTE:1"})

    def test_simulated_mixed_run(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp}):
            result = run_workflow(core.Agent(simulate=True), plan_deterministic(self.MIXED), on_step=lambda m: None)
        self.assertEqual(result["cellular"]["receiver"], "sim:cellmodem")
        keys = {a["device"]["key"] for a in result["workflow"]["alerts"]}
        self.assertEqual(keys, {"5A:11:00:00:00:A7", "2G GSM:0F00001"})
        self.assertEqual({d.get("protocol") for d in result["devices"]}, {"ble", "cellular"})


class HistoryAndErrorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_saved_runs_appear_in_history_and_reports_are_served(self):
        from rf_platform.agent.report import save
        result = run_workflow(core.Agent(simulate=True), plan_deterministic("find ble devices for 5 seconds"),
                              on_step=lambda m: None)
        folder = save(result, self.backend.RF_SENTINEL_LOG_DIR / "agent-runs")
        jobs = self.client.get("/api/agent/jobs").get_json()
        saved = next(j for j in jobs if j.get("run_id") == result["run_id"])
        self.assertTrue(saved["saved"] and saved["simulated"])
        self.assertEqual(saved["summary"]["find"], "BLE for 5 s")
        report = self.client.get(f"/api/agent/jobs/{saved['job_id']}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("SIMULATED RUN", report.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/agent/jobs/saved-RUN-..%2F..%2Fx/report").status_code, 404)
        self.assertTrue(folder.exists())

    def test_iq_service_down_is_explained(self):
        from rf_platform.agent.radio import Radio
        with self.assertRaisesRegex(RuntimeError, "IQ service at http://127.0.0.1:9 is not running"):
            Radio("http://127.0.0.1:9", "").devices()


class AlertDeliveryTest(unittest.TestCase):
    """Alerts go to an operator-configured webhook (off by default), through the gateway, recorded in the timeline."""

    def test_webhook_delivery(self):
        import http.server
        import json as _json
        received: list[dict] = []

        class Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(_json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/hook"
        try:
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": temp,
                                                                              "RF_SENTINEL_ALERT_WEBHOOK": url}):
                spec = plan_deterministic("find ble devices for 6 seconds. send an urgent alert if an airtag appears")
                result = run_workflow(core.Agent(simulate=True), spec, on_step=lambda m: None)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(received), 1)
        self.assertIn("[SIMULATED] RF Sentinel CRITICAL", received[0]["text"])
        self.assertIn("AirTag", received[0]["text"])
        deliveries = [e for e in result["workflow"]["timeline"] if e["kind"] == "delivery"]
        self.assertEqual([d["status"] for d in deliveries], ["delivered"])
        self.assertIn("deliver_alert", [a["tool"] for a in result["audit"]])

    def test_off_by_default_and_severity_floor(self):
        from rf_platform.agent import notify
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RF_SENTINEL_ALERT_WEBHOOK", None)
            self.assertIsNone(notify.deliver({"severity": "critical", "message": "x"}, "RUN", False))
            self.assertFalse(notify.describe()["enabled"])
        with patch.dict(os.environ, {"RF_SENTINEL_ALERT_WEBHOOK": "http://127.0.0.1:9/x",
                                     "RF_SENTINEL_ALERT_MIN_SEVERITY": "critical"}):
            self.assertIsNone(notify.deliver({"severity": "warning", "message": "x"}, "RUN", False))
            self.assertEqual(notify.describe()["host"], "127.0.0.1:9")
        with patch.dict(os.environ, {"RF_SENTINEL_ALERT_WEBHOOK": "file:///etc/passwd"}):
            self.assertIsNone(notify.webhook_url())
