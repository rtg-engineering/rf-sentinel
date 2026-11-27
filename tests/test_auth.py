"""Operator authentication, identity-bound approvals, atomic run admission, and restart history for watches."""
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

from rf_platform.auth import OperatorStore, mode, needed_role


class StoreAndPolicyTest(unittest.TestCase):
    def test_tokens_are_hashed_and_revocable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = OperatorStore(Path(temp))
            token = store.add("alice", "approver")
            text = store.path.read_text()
            self.assertNotIn(token, text)  # only the hash is stored
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertEqual(store.identify(token), {"name": "alice", "role": "approver"})
            self.assertIsNone(store.identify(token + "x"))
            self.assertIsNone(store.identify(""))
            new = store.add("alice", "viewer")  # re-keying invalidates the old token
            self.assertIsNone(store.identify(token))
            self.assertEqual(store.identify(new)["role"], "viewer")
            self.assertTrue(store.remove("alice"))
            self.assertIsNone(store.identify(new))
            with self.assertRaises(ValueError):
                store.add("../etc", "viewer")
            with self.assertRaises(ValueError):
                store.add("bob", "root")

    def test_mode_follows_exposure(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RF_SENTINEL_AUTH": "auto"}):
            store = OperatorStore(Path(temp))
            self.assertEqual(mode(store, "127.0.0.1", demo=False), "off")
            self.assertEqual(mode(store, "0.0.0.0", demo=False), "required")  # listening beyond this machine
            self.assertEqual(mode(store, "0.0.0.0", demo=True), "off")  # demo: simulated data only
            store.add("alice", "operator")
            self.assertEqual(mode(store, "127.0.0.1", demo=False), "required")  # operators exist: always enforce
            with patch.dict(os.environ, {"RF_SENTINEL_AUTH": "off"}):
                self.assertEqual(mode(store, "0.0.0.0", demo=False), "off")

    def test_roles_per_request(self):
        self.assertIsNone(needed_role("GET", "/api/health"))
        self.assertEqual(needed_role("GET", "/api/agent/jobs"), "viewer")
        self.assertEqual(needed_role("POST", "/api/agent/run"), "operator")
        self.assertEqual(needed_role("POST", "/api/scan/start"), "operator")
        self.assertEqual(needed_role("POST", "/api/agent/jobs/run-1/approve"), "approver")
        self.assertEqual(needed_role("POST", "/api/agent/jobs/run-1/deny"), "approver")


class BackendAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def setUp(self):
        self.env = patch.dict(os.environ, {"RF_SENTINEL_AUTH": "required"})
        self.env.start()
        self.store = self.backend._auth_store()
        self.tokens = {name: self.store.add(name, role) for name, role in
                       (("vera", "viewer"), ("otto", "operator"), ("alice", "approver"), ("bob", "approver"))}
        self.client.post("/api/auth/logout")
        with self.backend.agent_jobs_lock:
            self.backend.agent_jobs.clear()

    def tearDown(self):
        for name in list(self.tokens):
            self.store.remove(name)
        with self.backend.agent_jobs_lock:
            for job in self.backend.agent_jobs.values():
                if job.get("agent"):
                    job["agent"].stop()
            self.backend.agent_jobs.clear()
        self.env.stop()

    def as_(self, name):
        return {"Authorization": f"Bearer {self.tokens[name]}"}

    def test_roles_and_open_paths(self):
        denied = self.client.get("/api/agent/jobs")
        self.assertEqual(denied.status_code, 401)
        self.assertIn("Sign in", denied.get_json()["error"])
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)  # the page loads; its API calls need a sign-in
        self.assertEqual(self.client.get("/api/agent/jobs", headers=self.as_("vera")).status_code, 200)
        plan = {"text": "find ble devices for 5 seconds", "planner": "deterministic"}
        self.assertEqual(self.client.post("/api/agent/plan", json=plan, headers=self.as_("vera")).status_code, 403)
        self.assertEqual(self.client.post("/api/agent/plan", json=plan, headers=self.as_("otto")).status_code, 200)
        self.assertEqual(self.client.post("/api/scan/stop", json={}, headers=self.as_("vera")).status_code, 403)
        bad = {"Authorization": "Bearer rfs_not-a-real-token"}
        self.assertEqual(self.client.get("/api/agent/jobs", headers=bad).status_code, 401)

    def test_browser_session_needs_the_page_header_to_change_anything(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"token": "nope"}).status_code, 401)
        me = self.client.post("/api/auth/login", json={"token": self.tokens["otto"]}).get_json()
        self.assertEqual(me["operator"], {"name": "otto", "role": "operator"})
        self.assertEqual(self.client.get("/api/agent/jobs").status_code, 200)
        plan = {"text": "find ble devices for 5 seconds", "planner": "deterministic"}
        self.assertEqual(self.client.post("/api/agent/plan", json=plan).status_code, 403)  # no X-RF-Sentinel: CSRF
        self.assertEqual(self.client.post("/api/agent/plan", json=plan, headers={"X-RF-Sentinel": "1"}).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").get_json()["operator"]["name"], "otto")
        self.store.remove("otto")  # revoked on the host: the open session stops working at once
        self.assertEqual(self.client.get("/api/agent/jobs").status_code, 401)
        self.tokens.pop("otto")

    def _approval_run(self, starter):
        from rf_platform.agent.playbook import plan_playbook_deterministic
        book = plan_playbook_deterministic("find ble devices for 5 seconds. then find ble devices for 5 seconds")
        started = self.client.post("/api/agent/run", json={"playbook": book.model_dump(), "simulate": True,
                                                           "mode": "approval-required"}, headers=self.as_(starter))
        self.assertEqual(started.status_code, 200, started.get_json())
        job_id = started.get_json()["job_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            pending = self.client.get(f"/api/agent/jobs/{job_id}", headers=self.as_("vera")).get_json()["pending_approvals"]
            if pending:
                return job_id, pending[0]["action_id"]
            time.sleep(0.2)
        self.fail("no approval was requested")

    def test_approvals_are_recorded_under_the_signed_in_identity(self):
        job_id, action = self._approval_run("otto")
        body = {"action_id": action, "operator": "mallory"}
        self.assertEqual(self.client.post(f"/api/agent/jobs/{job_id}/approve", json=body,
                                          headers=self.as_("otto")).status_code, 403)  # operators cannot approve
        decided = self.client.post(f"/api/agent/jobs/{job_id}/approve", json=body, headers=self.as_("alice")).get_json()
        self.assertEqual((decided["decision"], decided["operator"]), ("approved", "alice"))  # not "mallory"
        job = self.backend.agent_jobs[job_id]
        self.assertEqual(job["requested_by"], "otto")
        self.assertEqual(job["agent"].gateway.audit[0]["operator"], "otto")

    def test_separation_of_duties(self):
        with patch.dict(os.environ, {"RF_SENTINEL_APPROVAL_SEPARATION": "1"}):
            job_id, action = self._approval_run("alice")
            own = self.client.post(f"/api/agent/jobs/{job_id}/approve", json={"action_id": action}, headers=self.as_("alice"))
            self.assertEqual(own.status_code, 403)
            self.assertIn("another operator", own.get_json()["error"])
            other = self.client.post(f"/api/agent/jobs/{job_id}/approve", json={"action_id": action}, headers=self.as_("bob"))
            self.assertEqual(other.get_json()["operator"], "bob")


class AdmissionTest(unittest.TestCase):
    """Two run requests at once: exactly one is admitted."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend = BackendValidationTest, BackendValidationTest.backend

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_concurrent_runs_admit_one(self):
        backend = self.backend
        with backend.agent_jobs_lock:
            backend.agent_jobs.clear()

        def slow_launch(spec, book, command, *args, **kwargs):
            time.sleep(0.2)  # widen the window between "is a run active?" and the job being registered
            job = {"job_id": f"run-{threading.get_ident()}", "status": "running", "command": command}
            with backend.agent_jobs_lock:
                backend.agent_jobs[job["job_id"]] = job
            return job

        codes = []
        barrier = threading.Barrier(6)

        def request():
            client = backend.app.test_client()
            barrier.wait()
            codes.append(client.post("/api/agent/run", json={"command": "find ble devices for 5 seconds",
                                                             "simulate": True}).status_code)

        with patch.object(backend, "_launch_agent_job", slow_launch), \
                patch.object(backend, "_agent_public", lambda job: {"job_id": job["job_id"]}):
            threads = [threading.Thread(target=request) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        with backend.agent_jobs_lock:
            backend.agent_jobs.clear()
        self.assertEqual(sorted(codes), [200, 409, 409, 409, 409, 409])


class RestartHistoryTest(unittest.TestCase):
    """A watch interrupted by a restart says which cycle was cut off, which were missed, and what ran before."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def entry(self, **extra):
        from rf_platform.agent.playbook import plan_playbook_deterministic
        book = plan_playbook_deterministic("find ble devices for 5 seconds. repeat every 10 minutes, 6 times")
        origin = time.time() - 3600
        return {"job_id": "run-test", "playbook": book.model_dump(), "origin": origin, "mode": "passive-auto",
                "planner": {"planner": "deterministic"}, "simulate": True, "command": "watch", "source": "dashboard",
                "gain_db": None, "requested_by": "alice", "in_progress": None,
                "cycles_done": [{"cycle": 0, "finished_at": origin + 6, "seen": 4, "new": 4, "alerts": 1,
                                 "alert_messages": ["new tracker"]},
                                {"cycle": 1, "finished_at": origin + 606, "seen": 5, "new": 1, "alerts": 0}], **extra}

    def resume(self, entry, minutes):
        backend = self.backend
        backend._active_dir().mkdir(parents=True, exist_ok=True)
        (backend._active_dir() / "run-test.json").write_text(json.dumps(entry))
        with backend.agent_jobs_lock:
            backend.agent_jobs.clear()
        jobs = backend.resume_agent_watches(now=entry["origin"] + minutes * 60)
        for job in jobs:
            job["agent"].stop()
        return jobs

    def test_interrupted_and_missed_cycles_are_recorded(self):
        entry = self.entry(in_progress={"cycle": 2, "started_at": 0})
        # Down from 21 min (cycle 3 running) until 35 min: cycle 4 (due at 30 min) was missed; 5-6 remain.
        job = self.resume(entry, 35)[0]
        self.assertEqual(job["requested_by"], "alice")
        active = json.loads((self.backend._active_dir() / f"{job['job_id']}.json").read_text()) \
            if (self.backend._active_dir() / f"{job['job_id']}.json").exists() else None
        lines = self.backend._restart_lines(entry["cycles_done"], [{"cycle": 2, "restart_at": entry["origin"] + 2100}], [3])
        self.assertIn("2 cycle(s) ran before a dashboard restart (1–2): 1 alert(s)", lines[0])
        self.assertIn("Cycle 3 was interrupted", lines[1])
        self.assertIn("Cycle(s) 4 did not run", lines[2])
        if active is not None:  # the resumed job carries the history forward for any later restart
            self.assertEqual([c["cycle"] for c in active["interrupted"]], [2])
            self.assertEqual(active["missed"], [3])
            self.assertEqual(len(active["cycles_done"]), 2)

    def test_resumed_report_continues_numbering_and_shows_history(self):
        from rf_platform.agent.playbook import plan_playbook_deterministic, render, run_playbook
        from rf_platform.agent import core
        book = plan_playbook_deterministic("find ble devices for 5 seconds")
        with tempfile.TemporaryDirectory() as temp:
            agent = core.Agent("http://127.0.0.1:9", "", simulate=True)
            result = run_playbook(agent, book, base_dir=Path(temp))
        entry = self.entry()
        self.backend._add_restart_history(result, {"offset": 3, "total": 6, "before": entry["cycles_done"],
                                                   "interrupted": [{"cycle": 2, "restart_at": entry["origin"] + 1300}],
                                                   "missed": []})
        html = render(result)
        self.assertIn("Before the restart", html)
        self.assertIn("interrupted", html)
        self.assertIn("new tracker", html)
        self.assertTrue(any("Cycle 3 was interrupted" in line for line in result["narrative"]))

    def test_watch_that_ended_while_down_still_gets_a_report(self):
        entry = self.entry(in_progress={"cycle": 5, "started_at": 0})
        self.assertEqual(self.resume(entry, 120), [])  # all six cycles were due long ago
        reports = list((self.backend.RF_SENTINEL_LOG_DIR / "agent-runs").glob("RUN-*/report.html"))
        text = "\n".join(p.read_text() for p in reports)
        self.assertIn("Before the restart", text)
        self.assertIn("Cycle 6 was interrupted", text)
        self.assertIn("Cycle(s) 3–5 did not run", text)


if __name__ == "__main__":
    unittest.main()
