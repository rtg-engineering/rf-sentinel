"""Playbooks: one request -> ordered workflow steps that gate on and focus on what earlier steps found."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.playbook import (PlaybookSpec, is_multistep, plan_any, plan_playbook_deterministic, render,
                                        run_playbook, split_steps)

OPERATION = ("Sweep for bluetooth trackers for 2 minutes. If you find any, watch them for 5 minutes and send an urgent "
             "alert if one is still there after 3 minutes, and record 5 seconds of IQ. Then survey cell towers for "
             "1 minute and alert me if a 2G cell shows up.")
CORPUS = json.loads((Path(__file__).parent / "fixtures" / "workflow_phrases.json").read_text())["cases"]
PLAYBOOKS = json.loads((Path(__file__).parent / "fixtures" / "playbook_phrases.json").read_text())["cases"]


def schedule_shape(book: PlaybookSpec) -> tuple:
    return (book.repeat.model_dump() if book.repeat else None,
            {k: v for k, v in book.start.model_dump().items() if k != "utc_offset_min"} if book.start else None)


def step_shape(book: PlaybookSpec, index: int) -> dict:
    """One step as tests/fixtures/playbook_phrases.json records it (step references resolved)."""
    from tests.test_workflow_corpus import shape as workflow_shape
    step = book.steps[index]
    ref = lambda value: (index - 1 if value is None else value)  # noqa: E731
    out = {"focus": step.focus, "focus_step": ref(step.focus_step) if step.focus != "all" else None,
           "gate": step.gate.when, "gate_step": ref(step.gate.step) if step.gate.when != "always" else None,
           "gate_conditions": sorted([c.field, c.op, c.value] for c in step.gate.conditions), **workflow_shape(step.workflow)}
    out.pop("device_id")
    return json.loads(json.dumps(out))


def shape(book: PlaybookSpec):
    return [(s.workflow.collect, s.workflow.duration_s, s.focus, s.focus_step, s.gate.when,
             [(c.field, c.value) for c in s.gate.conditions]) for s in book.steps]


class PlannerTest(unittest.TestCase):
    def test_operation(self):
        book = plan_playbook_deterministic(OPERATION)
        self.assertEqual(shape(book), [(["ble"], 120, "all", None, "always", []),
                                       (["ble"], 300, "found", None, "found", []),
                                       (["cellular"], 60, "all", None, "always", [])])
        follow = book.steps[1].workflow
        self.assertEqual(follow.triggers[0].when[0].field, "dwell_s")
        self.assertEqual(follow.triggers[0].when[0].value, 180)
        self.assertEqual([(a.kind, a.severity if a.kind == "alert" else a.seconds) for a in follow.triggers[0].do],
                         [("alert", "critical"), ("record_iq", 5)])
        self.assertEqual(book.steps[2].workflow.triggers[0].when[0].value, "2G")
        summary = book.summary()
        self.assertEqual(summary["total_s"], 480)
        self.assertEqual(summary["steps"][1]["gate"], "only if step 1 found something")
        self.assertEqual(summary["steps"][1]["focus"], "devices from step 1")

    def test_gates_focus_and_inheritance(self):
        book = plan_playbook_deterministic(
            "watch for trackers for 1 minute. if nothing turns up, survey cell towers for 1 minute. otherwise, focus on "
            "them for 3 minutes and alert me when one is seen for more than 2 minutes")
        self.assertEqual(shape(book), [(["ble"], 60, "all", None, "always", []),
                                       (["cellular"], 60, "all", None, "none_found", []),
                                       (["ble"], 180, "found", 0, "found", [])])  # inherits BLE from step 1, not step 2
        self.assertEqual(book.steps[2].gate.step, 0)
        book = plan_playbook_deterministic("find ble devices for 60 seconds, then watch the apple devices you found for 2 minutes")
        self.assertEqual(book.steps[1].focus, "found")
        self.assertEqual([(c.field, c.value) for c in book.steps[1].workflow.filters], [("manufacturer", "Apple")])
        book = plan_playbook_deterministic("find bluetooth devices for 1 minute. if you find any trackers, then watch "
                                           "them for 2 minutes and tag them as suspect")
        self.assertEqual(book.steps[1].gate.conditions[0].value, "tracker")
        self.assertEqual(book.steps[1].workflow.triggers[0].do[0].label, "suspect")  # standing action: every focused device

    def test_numbered_list_and_flagged(self):
        book = plan_playbook_deterministic("1. survey cell towers for 2 minutes and alert me if an unknown network appears "
                                           "2. watch for trackers for 3 minutes 3. watch the flagged ones for 2 minutes")
        self.assertEqual([s.workflow.collect for s in book.steps], [["cellular"], ["ble"], ["cellular"]])
        self.assertEqual((book.steps[2].focus, book.steps[2].focus_step), ("alerted", 0))  # the step that could alert

    def test_reviewed_playbook_corpus(self):
        for case in PLAYBOOKS:
            with self.subTest(case["text"]):
                if case.get("refused"):
                    with self.assertRaises(ValueError):
                        plan_any(case["text"], "deterministic")
                    continue
                book = plan_playbook_deterministic(case["text"], utc_offset_min=0)
                self.assertEqual([step_shape(book, i) for i in range(len(book.steps))], case["steps"])
                if "repeat" in case:
                    self.assertEqual(schedule_shape(book), (case["repeat"], case["start"]))

    def test_single_requests_stay_single(self):
        for case in CORPUS:
            self.assertEqual(len(split_steps(case["text"])), 1, case["text"])
            self.assertFalse(is_multistep(case["text"]), case["text"])  # no steps, schedule or repeat read into it
        self.assertFalse(is_multistep("If an AirTag appears, then scan bluetooth classic for 60 seconds"))
        self.assertFalse(is_multistep("find ble devices for 60 seconds, only stronger than -60. When a device is seen "
                                      "5 times, alert me"))
        self.assertIn("spec", plan_any("find ble devices for 1 minute", "deterministic"))
        # A dangling gate with no step after it stays in the request text, separators intact.
        self.assertEqual(split_steps("watch for trackers for 1 minute. if you find any trackers. then alert me"),
                         ["watch for trackers for 1 minute. if you find any trackers. then alert me"])
        self.assertIn("playbook", plan_any(OPERATION, "deterministic"))

    def test_bounds_and_refusals(self):
        with self.assertRaisesRegex(ValueError, "at most 1800 s"):
            plan_playbook_deterministic("find ble devices for 10 minutes. then survey cell towers for 10 minutes. then "
                                        "find ble devices for 10 minutes. then survey cell towers for 5 minutes")
        with self.assertRaisesRegex(ValueError, "only receives"):
            plan_any("watch for trackers for 1 minute. then jam them", "deterministic")
        # WiFi is refused by code for every planner, so a model cannot silently plan only the Bluetooth half.
        class Model:  # would plan something if it were ever asked
            pass
        for planner in ("deterministic", "model"):
            with self.assertRaisesRegex(ValueError, "monitor-mode WiFi"):
                plan_any("look for wifi and bluetooth devices for 20 seconds. then survey cell towers for 30 seconds",
                         planner, model=Model())
            with self.assertRaisesRegex(ValueError, "monitor-mode WiFi"):
                plan_any("scan wifi for 20 seconds", planner, model=Model())
        step = {"workflow": {"collect": ["ble"], "duration_s": 30}}
        with self.assertRaisesRegex(ValueError, "earlier step"):
            PlaybookSpec.model_validate({"steps": [step, {**step, "focus": "found", "focus_step": 1}]})
        with self.assertRaisesRegex(ValueError, "first step"):
            PlaybookSpec.model_validate({"steps": [{**step, "gate": {"when": "found"}}]})

    def test_model_planner_is_validated_and_falls_back(self):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        def good(messages, info):
            book = {"goal": "op", "steps": [
                {"title": "Sweep", "workflow": {"collect": ["ble"], "duration_s": 60}},
                {"title": "Follow", "focus": "found", "gate": {"when": "found"},
                 "workflow": {"collect": ["ble"], "duration_s": 999}}]}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, book)])
        planned = plan_any(OPERATION, "model", model=FunctionModel(good))
        # 999 s is out of bounds: the model is asked again, then the rules planner takes over with the reason.
        self.assertEqual(planned["planner"], "deterministic")
        self.assertTrue(planned["fallback_reason"])

        def fine(messages, info):
            book = {"goal": "op", "steps": [
                {"title": "Sweep", "workflow": {"collect": ["ble"], "duration_s": 60}},
                {"title": "Follow", "focus": "found", "gate": {"when": "found"},
                 "workflow": {"collect": ["ble"], "duration_s": 120}}]}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, book)])
        planned = plan_any(OPERATION, "model", model=FunctionModel(fine))
        self.assertEqual((planned["planner"], len(planned["playbook"]["steps"])), ("model", 2))
        with self.assertRaisesRegex(ValueError, "only receives"):
            plan_any("watch for trackers for 1 minute. then jam them", "model", model=FunctionModel(fine))


class RunTest(unittest.TestCase):
    """Simulated receivers drive the real agent, engine, gateway and report."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"RF_SENTINEL_AGENT_RUNS_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_gate_focus_tags_and_persistence(self):
        book = plan_playbook_deterministic(
            "watch for trackers for 6 seconds and tag them as seen-early. if you find any, watch them for 5 seconds and "
            "send an urgent alert if one is seen 2 times. then survey cell towers for 5 seconds")
        events: list[dict] = []
        result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None, on_event=events.append)
        records = result["steps_run"]
        self.assertEqual([r["status"] for r in records], ["done", "done", "done"])
        found = {d["key"] for d in records[0]["result"]["workflow"]["matched"]}
        self.assertTrue(found)
        self.assertEqual(set(records[1]["focus"]), found)
        self.assertTrue({d["key"] for d in records[1]["result"]["devices"]} <= found)  # focus holds
        self.assertTrue(all(a["step"] == 1 and a["severity"] == "critical" for a in result["alerts"]))
        self.assertTrue(result["alerts"])
        # Tags from step 1 travel with the device into step 2.
        self.assertTrue(all("seen-early" in d["tags"] for d in records[1]["result"]["workflow"]["all_devices"]))
        self.assertTrue(result["persistence"])
        self.assertTrue(all(p["steps"] == [1, 2] and p["every_step"] for p in result["persistence"]))
        self.assertEqual([e["status"] for e in events if e["kind"] == "step"], ["running", "done"] * 3)
        self.assertTrue(all("step" in e for e in events))
        page = render(result)
        self.assertIn("SIMULATED PLAYBOOK", page)
        self.assertIn("Persistent devices", page)

    def test_skips_and_otherwise(self):
        book = plan_playbook_deterministic(
            "find ble devices named nobodyhome for 5 seconds. if you find any, watch them for 5 seconds. "
            "otherwise, survey cell towers for 5 seconds")
        result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None)
        self.assertEqual([r["status"] for r in result["steps_run"]], ["done", "skipped", "done"])
        self.assertIn("found no device", result["steps_run"][1]["reason"])
        self.assertIn("skipped", " ".join(result["narrative"]))

    def test_stop_ends_the_playbook(self):
        book = plan_playbook_deterministic("find ble devices for 30 seconds. then survey cell towers for 30 seconds")
        agent = core.Agent(simulate=True)
        threading.Timer(2.0, agent.stop).start()
        started = time.monotonic()
        result = run_playbook(agent, book, on_step=lambda m: None)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["steps_run"][1]["status"], "not_run")


    def test_one_scenario_clock_and_separate_known_list(self):
        book = plan_playbook_deterministic("find ble devices for 6 seconds and tag them as baseline. then watch for new "
                                           "devices for 8 seconds and alert me when one stronger than -50 appears. finally, "
                                           "watch the flagged ones for 5 seconds and record 1 second of IQ")
        result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None)
        records = result["steps_run"]
        self.assertEqual([r["status"] for r in records], ["done", "done", "done"])
        # The scripted newcomer arrives during step 2 (one clock across steps), is new, alerts, and gets evidence.
        alerted = [a["device"]["key"] for a in result["alerts"]]
        self.assertEqual(len(alerted), 1)
        self.assertTrue(alerted[0].endswith(":B9"))
        self.assertEqual(records[2]["focus"], alerted)
        self.assertEqual(len(result["evidence"]), 1)
        base = Path(self.temp.name)
        self.assertTrue((base / "known_devices.simulated.json").exists())
        self.assertFalse((base / "known_devices.json").exists())  # synthetic devices never become "known" for real runs


    def test_recurring_lines_tell_same_named_devices_apart(self):
        from rf_platform.agent.playbook import recurring
        records = [{"cycle": c, "status": "done"} for c in range(3)]
        devices = [{"key": f"ble:AA:BB:CC:DD:EE:0{i}", "identity": "Apple, Inc.", "cycles": [1, 2, 3]} for i in range(2)]
        devices.append({"key": "ble:11:22", "identity": "Tile, Inc.", "cycles": [1, 2]})
        self.assertEqual([r["label"] for r in recurring(records, devices)],
                         ["Apple, Inc. (…EE:00)", "Apple, Inc. (…EE:01)", "Tile, Inc."])

    def test_standing_watch_repeats_and_finds_recurring_devices(self):
        from rf_platform.agent import playbook
        with patch.object(playbook, "MIN_REPEAT_S", 5):
            book = plan_playbook_deterministic("watch for trackers for 5 seconds and alert me when a new one appears. "
                                               "repeat every 8 seconds, 3 times")
            self.assertEqual((book.repeat.every_s, book.repeat.times), (8, 3))
            events: list[dict] = []
            started = time.monotonic()
            result = run_playbook(core.Agent(simulate=True), book, on_step=lambda m: None, on_event=events.append)
        self.assertGreaterEqual(time.monotonic() - started, 16)  # cycles start every 8 s, not back to back
        self.assertEqual(result["cycles"], {"planned": 3, "run": 3, "every_s": 8})
        self.assertEqual([r["cycle"] for r in result["steps_run"]], [0, 1, 2])
        self.assertTrue(result["recurring"])
        self.assertTrue(all(r["of"] == 3 and len(r["cycles"]) >= 2 for r in result["recurring"]))
        self.assertEqual([e["status"] for e in events if e["kind"] == "cycle"],
                         ["running", "done", "waiting", "running", "done", "waiting", "running", "done"])
        stats = [e["stats"] for e in events if e["kind"] == "cycle" and e["status"] == "done"]
        self.assertEqual(len(stats), 3)
        self.assertTrue(all(s["seen"] >= s["new"] for s in stats))
        # "new" means not seen in an earlier cycle either: the trackers alert in cycle 1 only.
        self.assertTrue(result["alerts"])
        self.assertEqual({a["cycle"] for a in result["alerts"]}, {0})
        self.assertIn("Standing watch: 3 of 3 cycle(s) ran", result["narrative"][0])
        page = render(result)
        self.assertIn("Recurring across cycles", page)
        self.assertIn("<h2>Cycles</h2>", page)

    def test_standing_watch_stops_while_waiting(self):
        from rf_platform.agent import playbook
        with patch.object(playbook, "MIN_REPEAT_S", 5):
            book = plan_playbook_deterministic("find ble devices for 5 seconds every 60 seconds, 3 times")
        agent = core.Agent(simulate=True)
        threading.Timer(9.0, agent.stop).start()
        started = time.monotonic()
        result = run_playbook(agent, book, on_step=lambda m: None)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual((result["cycles"]["run"], result["stopped"]), (1, True))

    def test_repeat_phrasing_and_bounds(self):
        book = plan_playbook_deterministic("every 10 minutes for the next hour, sweep for trackers for 1 minute and "
                                           "alert me when a new one appears")
        self.assertEqual((book.repeat.every_s, book.repeat.times, len(book.steps)), (600, 6, 1))
        book = plan_playbook_deterministic("find ble devices for 30 seconds. then watch them for 1 minute. repeat 4 "
                                           "times, every 5 minutes")
        self.assertEqual((book.repeat.every_s, book.repeat.times, len(book.steps)), (300, 4, 2))
        book = plan_playbook_deterministic("survey cell towers for 1 minute, every hour until I stop it")
        self.assertEqual(book.repeat.times, 12)
        self.assertIn("unless stopped", book.notes[0])
        with self.assertRaisesRegex(ValueError, "repeats every 60 s"):
            plan_playbook_deterministic("watch for trackers for 1 minute every 30 seconds for 5 minutes")
        with self.assertRaisesRegex(ValueError, "at most 48 cycles"):
            plan_playbook_deterministic("watch for trackers for 2 minutes every 10 minutes for 13 hours")
        with self.assertRaisesRegex(ValueError, "shorter than one cycle"):
            plan_playbook_deterministic("watch for trackers for 5 minutes every 2 minutes for 1 hour")


    def test_approval_mode_gates_every_step_and_observe_denies(self):
        from rf_platform.agent.gateway import AgentGateway, ApprovalBroker
        broker = ApprovalBroker()
        agent = core.Agent(simulate=True, gateway=AgentGateway("approval-required", "run-pb", broker))
        book = plan_playbook_deterministic("find ble devices for 5 seconds. then survey cell towers for 5 seconds")
        approved: list[str] = []
        done = threading.Event()

        def operator():
            while not done.is_set():
                for item in broker.pending("run-pb"):
                    approved.append(item["tool"])
                    broker.resolve(item["action_id"], True, "test operator")
                time.sleep(0.1)
        threading.Thread(target=operator, daemon=True).start()
        try:
            result = run_playbook(agent, book, on_step=lambda m: None)
        finally:
            done.set()
        self.assertEqual([r["status"] for r in result["steps_run"]], ["done", "done"])
        self.assertEqual(approved.count("start_scan"), 2)  # each step's collection waited for a named operator
        audit = [a for a in result["audit"] if a["tool"] == "start_scan"]
        self.assertEqual([a["operator"] for a in audit if a["decision"] == "approved"], ["test operator"] * 2)
        # Observe mode: no step may start collection.
        observed = run_playbook(core.Agent(simulate=True, mode="observe"), book, on_step=lambda m: None)
        self.assertTrue(all(r["status"] == "error" and "observe" in r["reason"].lower() for r in observed["steps_run"]))


    def test_scheduled_start(self):
        from rf_platform.agent.playbook import Start
        book = plan_playbook_deterministic("From 10pm to 6am, every 30 minutes, sweep for trackers for 1 minute and send "
                                           "an urgent alert when a new one appears.", utc_offset_min=-300)
        self.assertEqual((book.start.at, book.start.utc_offset_min, book.repeat.every_s, book.repeat.times), ("22:00", -300, 1800, 16))
        # 22:00 at UTC-5 is 03:00 UTC: from 01:00 UTC the start is 2 h away, from 04:00 UTC it is the next day.
        day = 1_800_000_000 - 1_800_000_000 % 86400
        self.assertEqual(Start(at="22:00", utc_offset_min=-300).resolve(day + 3600) - day, 3 * 3600)
        self.assertEqual(Start(at="22:00", utc_offset_min=-300).resolve(day + 4 * 3600) - day, 27 * 3600)
        self.assertEqual(plan_playbook_deterministic("In 20 minutes, survey cell towers for 2 minutes").start.in_s, 1200)
        self.assertEqual(plan_playbook_deterministic("tonight at 10, find ble devices for 1 minute", 0).start.at, "22:00")
        self.assertFalse(is_multistep("watch for trackers for 5 minutes and alert me if one is seen for at least 10 seconds"))
        self.assertFalse(is_multistep("watch for trackers in the next 3 minutes"))
        self.assertFalse(is_multistep("watch for trackers and alert me if one appears in 2 minutes"))
        self.assertEqual(plan_playbook_deterministic("survey cell towers for 2 minutes, starting in 20 minutes").start.in_s, 1200)
        with self.assertRaisesRegex(ValueError, "how often"):
            plan_playbook_deterministic("from 10pm to 6am sweep for trackers for 1 minute")
        with self.assertRaisesRegex(ValueError, "24 h"):
            plan_playbook_deterministic("in 30 hours find ble devices for 1 minute")
        # Waiting for the start is interruptible, and nothing is collected before it.
        agent = core.Agent(simulate=True)
        threading.Timer(1.5, agent.stop).start()
        events: list[dict] = []
        result = run_playbook(agent, plan_playbook_deterministic("in 10 minutes, find ble devices for 5 seconds"),
                              on_step=lambda m: None, on_event=events.append)
        self.assertEqual(result["steps_run"], [])
        self.assertTrue(events[0]["scheduled"] and events[0]["status"] == "waiting")


class CliTest(unittest.TestCase):
    def test_plan_prints_steps_without_a_service(self):
        import contextlib
        import io
        from rf_platform.agent.__main__ import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["plan", OPERATION, "--planner", "deterministic", "--server", "http://127.0.0.1:9"]), 0)
        text = out.getvalue()
        self.assertIn("Playbook (deterministic)", text)
        self.assertIn("2. Follow up on what was found (BLE)", text)
        self.assertIn("IF      only if step 1 found something", text)


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_plan_edit_run_and_history(self):
        planned = self.client.post("/api/agent/plan", json={"text": OPERATION, "planner": "deterministic"}).get_json()
        self.assertEqual(len(planned["summary"]["steps"]), 3)
        book = planned["playbook"]
        book["steps"][1]["workflow"]["duration_s"] = 99999
        self.assertEqual(self.client.post("/api/agent/plan", json={"playbook": book}).status_code, 400)
        small = plan_playbook_deterministic("find ble devices for 5 seconds. then survey cell towers for 5 seconds")
        started = self.client.post("/api/agent/run", json={"playbook": small.model_dump(), "simulate": True,
                                                           "mode": "passive-auto"}).get_json()
        self.assertEqual(started["kind"], "playbook")
        deadline = time.time() + 60
        job = started
        while time.time() < deadline and job["status"] not in ("done", "error", "stopped"):
            time.sleep(0.5)
            job = self.client.get(f"/api/agent/jobs/{started['job_id']}").get_json()
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual([s["status"] for s in job["step_status"]], ["done", "done"])
        report = self.client.get(f"/api/agent/jobs/{job['job_id']}/report").get_data(as_text=True)
        self.assertIn("RECEIVE-ONLY PLAYBOOK", report)
        with self.backend.agent_jobs_lock:
            self.backend.agent_jobs.clear()
        saved = next(j for j in self.client.get("/api/agent/jobs").get_json() if j.get("run_id") == job["run_id"])
        self.assertEqual((saved["kind"], len(saved["step_status"])), ("playbook", 2))


if __name__ == "__main__":
    unittest.main()


class ResumeTest(unittest.TestCase):
    """A standing watch survives a dashboard restart: it continues from its next scheduled cycle."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_active_watch_is_recorded_and_resumed(self):
        backend = self.backend
        book = plan_playbook_deterministic("find ble devices for 5 seconds. repeat every 10 minutes, 6 times")
        started = self.client.post("/api/agent/run", json={"playbook": book.model_dump(), "simulate": True,
                                                           "mode": "passive-auto"}).get_json()
        active = backend._active_dir() / f"{started['job_id']}.json"
        self.assertTrue(active.exists())
        entry = json.loads(active.read_text())
        self.client.post(f"/api/agent/jobs/{started['job_id']}/stop")
        deadline = time.time() + 30
        while time.time() < deadline and active.exists():
            time.sleep(0.3)
        self.assertFalse(active.exists())  # a run that ends normally is no longer "active"
        # Simulate a restart 25 minutes into the watch: cycles 1-3 have started, so 4-6 remain.
        active.write_text(json.dumps(entry))
        with backend.agent_jobs_lock:
            backend.agent_jobs.clear()
        jobs = backend.resume_agent_watches(now=entry["origin"] + 25 * 60)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        try:
            self.assertEqual(job["source"], "resumed")
            self.assertIn("cycles 4–6 of 6", job["resumed"])
            self.assertEqual(job["playbook"]["repeat"]["times"], 3)
            self.assertEqual(job["playbook"]["start"]["in_s"], 300)  # cycle 4 is due at 30 min; the restart is at 25 min
        finally:
            job["agent"].stop()
        # Nothing left to resume after the last cycle's start time.
        (backend._active_dir() / "run-old.json").write_text(json.dumps(entry))
        with backend.agent_jobs_lock:
            backend.agent_jobs.clear()
        time.sleep(1)
        self.assertEqual(backend.resume_agent_watches(now=entry["origin"] + 3 * 3600), [])
        self.assertFalse((backend._active_dir() / "run-old.json").exists())


class JobPayloadTest(unittest.TestCase):
    """A long watch keeps the recent timeline in memory; totals stay exact."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_events_are_capped(self):
        with patch.object(self.backend, "AGENT_EVENTS_KEPT", 5):
            with self.backend.agent_jobs_lock:
                self.backend.agent_jobs.clear()
            spec = {"collect": ["ble"], "duration_s": 6, "triggers": [{"when": [{"field": "count", "op": "ge", "value": 1}],
                                                                        "do": [{"kind": "tag", "label": "x"}]}]}
            job = self.client.post("/api/agent/run", json={"workflow": spec, "simulate": True, "mode": "passive-auto"}).get_json()
            deadline = time.time() + 40
            while time.time() < deadline and job["status"] not in ("done", "error", "stopped"):
                time.sleep(0.5)
                job = self.client.get(f"/api/agent/jobs/{job['job_id']}").get_json()
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertLessEqual(len(job["events"]), 5)
        self.assertGreater(sum(job["event_counts"].values()), 5)
