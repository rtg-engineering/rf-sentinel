"""Agent tool gateway: modes, ceiling, approvals, and enforcement independent of the interface."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rf_platform.agent import core
from rf_platform.agent.gateway import (NEVER, TOOLS, AgentGateway, ApprovalBroker, PermissionDenied, decide,
                                       effective_mode)
from tests.test_agent import DEVICE, FAKE_PLUGIN


class PolicyTest(unittest.TestCase):
    def test_ceiling_caps_requested_mode(self):
        self.assertEqual(effective_mode("passive-auto", "approval-required"), "approval-required")
        self.assertEqual(effective_mode("observe", "passive-auto"), "observe")
        self.assertEqual(effective_mode(None, "approval-required"), "approval-required")
        with self.assertRaises(PermissionDenied):
            effective_mode("root", "passive-auto")
        with patch.dict(os.environ, {"RF_SENTINEL_AGENT_MODE": "nonsense"}):
            self.assertEqual(effective_mode(None), "observe")  # unknown ceiling fails closed

    def test_permission_matrix(self):
        for tool, kind in TOOLS.items():
            for mode in ("observe", "approval-required", "passive-auto"):
                expected = "allow" if kind in ("read", "control") else \
                    {"observe": "deny", "approval-required": "require_approval", "passive-auto": "allow"}[mode]
                self.assertEqual(decide(tool, mode), expected, (tool, mode))
        for tool in NEVER | {"made_up_tool"}:
            self.assertEqual({decide(tool, m) for m in ("observe", "approval-required", "passive-auto")}, {"deny"})


class ApprovalTest(unittest.TestCase):
    def gateway(self, ttl=5):
        return AgentGateway("approval-required", "run-1", ApprovalBroker(), approval_ttl_s=ttl)

    def approve_when_pending(self, gw, approve=True, operator="ops-lead"):
        def worker():
            for _ in range(200):
                pending = gw.broker.pending("run-1")
                if pending:
                    gw.broker.resolve(pending[0]["action_id"], approve, operator, "test")
                    return
                time.sleep(0.01)
        threading.Thread(target=worker, daemon=True).start()

    def test_approve_deny_expire_cancel(self):
        gw = self.gateway()
        self.approve_when_pending(gw)
        gw.authorize("start_scan", {"device_id": "bladerf:0"})  # returns once approved
        gw = self.gateway()
        self.approve_when_pending(gw, approve=False)
        with self.assertRaisesRegex(PermissionDenied, "denied by ops-lead"):
            gw.authorize("start_scan", {})
        gw = self.gateway(ttl=0.1)
        with self.assertRaisesRegex(PermissionDenied, "expired"):
            gw.authorize("start_scan", {})
        gw = self.gateway()
        threading.Timer(0.1, gw.stop.set).start()
        with self.assertRaisesRegex(PermissionDenied, "cancelled"):
            gw.authorize("start_scan", {})
        decisions = [a["decision"] for a in gw.audit]
        self.assertEqual(decisions, ["approval_requested", "cancelled"])

    def test_named_operator_single_use(self):
        broker = ApprovalBroker()
        item = broker.request("run-1", "start_scan", {}, 60)
        with self.assertRaises(PermissionDenied):
            broker.resolve(item.action_id, True, "  ")
        broker.resolve(item.action_id, True, "ops-lead")
        with self.assertRaises(PermissionDenied):
            broker.resolve(item.action_id, True, "ops-lead")  # already decided

    def test_observe_and_never_are_denied_without_waiting(self):
        gw = AgentGateway("observe", "run-1")
        with self.assertRaises(PermissionDenied):
            gw.authorize("start_scan", {})
        with self.assertRaisesRegex(PermissionDenied, "never available"):
            AgentGateway("passive-auto", "run-1").authorize("page_stimulus", {})
        gw.authorize("list_receivers", {})
        gw.authorize("stop_scan", {})


class AgentModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = Path(self.temp.name) / "ble_scanner"
        self.marker = Path(self.temp.name) / "plugin-ran"
        self.plugin.write_text(FAKE_PLUGIN.replace("import json, sys, time",
                                                   f"import json, sys, time\nopen({str(self.marker)!r}, 'w').write('x')"))
        self.plugin.chmod(self.plugin.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.temp.cleanup()

    def run_agent(self, gateway, command="find bluetooth devices for 5 seconds"):
        agent = core.Agent("http://iq.invalid", "", gateway=gateway, run_id=gateway.run_id)
        with patch.object(core, "plugin_path", return_value=str(self.plugin)), patch.object(core, "WINDOW_S", 1.0), \
                patch.object(agent.radio, "choose", return_value=DEVICE), \
                patch.object(agent.radio, "devices", return_value=[DEVICE]):
            return agent.run(command, on_step=lambda m: None, gain_db=40)

    def test_observe_never_starts_the_receiver(self):
        with self.assertRaises(PermissionDenied):
            self.run_agent(AgentGateway("observe", "run-o"))
        self.assertFalse(self.marker.exists())

    def test_approval_mode_completes_and_denied_gain_change_keeps_scanning(self):
        broker = ApprovalBroker()
        gw = AgentGateway("approval-required", "run-a", broker)

        def operator():
            deadline = time.time() + 20
            while time.time() < deadline:
                for p in broker.pending("run-a"):
                    broker.resolve(p["action_id"], p["tool"] != "adjust_gain", "ops-lead", "keep gain")
                time.sleep(0.02)
        threading.Thread(target=operator, daemon=True).start()
        result = self.run_agent(gw)
        self.assertTrue(self.marker.exists())
        self.assertEqual(result["mode"], "approval-required")
        self.assertEqual(result["gains_db"], [40.0])  # the denied change was not made
        decisions = [(a["tool"], a["decision"]) for a in result["audit"] if a["decision"] != "approval_requested"]
        self.assertIn(("start_scan", "approved"), decisions)
        self.assertIn(("adjust_gain", "denied"), decisions)
        self.assertTrue(any("gain change not made" in s["message"] for s in result["steps"]))


class BackendEndpointTest(unittest.TestCase):
    """Policy is enforced by the backend API itself, not by the UI or CLI."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness = BackendValidationTest
        cls.client = BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def post(self, path, body):
        response = self.client.post(path, json=body)
        return response.status_code, response.get_json()

    def test_policy_and_direct_tool_rejections(self):
        policy = self.client.get("/api/agent/policy").get_json()
        self.assertEqual(policy["ceiling"], "passive-auto")
        self.assertIn("page_stimulus", policy["never"])
        self.assertEqual(self.post("/api/agent/tools/start_scan", {"mode": "observe"})[0], 403)
        self.assertEqual(self.post("/api/agent/tools/adjust_gain", {"mode": "observe"})[0], 403)
        code, body = self.post("/api/agent/tools/page_stimulus", {"mode": "passive-auto"})
        self.assertEqual((code, "never available" in body["error"]), (403, True))
        self.assertEqual(self.post("/api/agent/tools/shell", {})[0], 403)
        self.assertEqual(self.post("/api/agent/tools/start_scan", {"mode": "passive-auto"})[0], 409)
        self.assertEqual(self.post("/api/agent/tools/start_scan", {"mode": "superuser"})[0], 400)
        # The demo has no radios: runs use simulated receivers and are labelled as such.
        code, job = self.post("/api/agent/run", {"command": "find ble devices for 5 seconds"})
        self.assertEqual((code, job["simulated"]), (200, True))
        self.assertEqual(self.post(f"/api/agent/jobs/{job['job_id']}/stop", {})[0], 200)
        import time
        for _ in range(100):
            if self.client.get(f"/api/agent/jobs/{job['job_id']}").get_json()["status"] not in ("running", "stopping"):
                break
            time.sleep(0.1)
        self.assertEqual(self.post("/api/agent/jobs/run-nope/stop", {})[0], 404)
        self.assertEqual(self.post("/api/agent/jobs/run-nope/approve", {"action_id": "A-x", "operator": "o"})[0], 404)


if __name__ == "__main__":
    unittest.main()
