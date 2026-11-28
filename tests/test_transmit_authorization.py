"""Transmitting needs a named operator's authorization: agent tools, workflow actions, dashboard settings."""
from __future__ import annotations

import io
import threading
import time
import unittest
from unittest.mock import patch

from rf_platform.agent import transmit
from rf_platform.agent.gateway import AgentGateway, ApprovalBroker, PermissionDenied, decide


class GatewayTransmitTest(unittest.TestCase):
    def test_transmit_tools_always_need_approval(self):
        for tool in ("bt_inquiry", "bt_remote_name"):
            self.assertEqual(decide(tool, "observe"), "deny")
            self.assertEqual(decide(tool, "approval-required"), "require_approval")
            self.assertEqual(decide(tool, "passive-auto"), "require_approval")  # even fully automatic runs ask
        for never in ("jam", "spoof", "replay", "shell", "transmit"):
            self.assertEqual(decide(never, "passive-auto"), "deny")

    def test_approval_names_the_operator_and_denial_stops_it(self):
        broker = ApprovalBroker()
        gw = AgentGateway("passive-auto", "run-t", broker)

        def operator(approve):
            deadline = time.time() + 5
            while time.time() < deadline:
                for p in broker.pending("run-t"):
                    broker.resolve(p["action_id"], approve, "alice")
                    return
                time.sleep(0.01)
        threading.Thread(target=operator, args=(True,), daemon=True).start()
        gw.authorize("bt_inquiry", {"seconds": 10})
        self.assertEqual([(a["decision"], a.get("operator")) for a in gw.audit][-1], ("approved", "alice"))
        threading.Thread(target=operator, args=(False,), daemon=True).start()
        with self.assertRaisesRegex(PermissionDenied, "denied by alice"):
            gw.authorize("bt_remote_name", {"address": "AA:BB:CC:DD:EE:FF"})


class TransmitToolsTest(unittest.TestCase):
    def test_inquiry_is_bounded_and_turns_scanning_off(self):
        class Proc:
            def __init__(self, *a, **k):
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 0

        procs = []
        out = transmit.inquiry(600, popen=lambda *a, **k: procs.append(Proc()) or procs[-1], sleep=lambda s: None)
        self.assertEqual(out["seconds"], transmit.MAX_INQUIRY_S)
        self.assertIn("scan bredr on", procs[0].stdin.getvalue())
        self.assertTrue(procs[0].stdin.getvalue().rstrip().endswith("scan off\nquit"))

    def test_remote_name_needs_a_full_address(self):
        with self.assertRaises(ValueError):
            transmit.remote_name("LAP 9E8B33", run=lambda *a, **k: None)
        result = type("R", (), {"stdout": "Headset\n"})()
        self.assertEqual(transmit.remote_name("aa:bb:cc:dd:ee:ff", run=lambda *a, **k: result), "Headset")


class WorkflowTransmitTest(unittest.TestCase):
    def spec(self, text):
        from rf_platform.agent.playbook import plan_any
        from rf_platform.agent.workflow import WorkflowSpec
        return WorkflowSpec(**plan_any(text, planner="deterministic")["spec"])

    def run_with(self, mode, approve):
        from rf_platform.agent.core import Agent
        from rf_platform.agent.workflow import run_workflow
        agent = Agent(simulate=True, mode=mode, run_id="run-w")
        broker = agent.gateway.broker
        asked = []

        def operator():
            deadline = time.time() + 30
            while time.time() < deadline:
                for p in broker.pending():
                    asked.append(p["tool"])
                    broker.resolve(p["action_id"], approve, "alice")
                time.sleep(0.02)
        threading.Thread(target=operator, daemon=True).start()
        out = run_workflow(agent, self.spec("find bluetooth classic devices for 6 seconds and run an inquiry scan for 2 seconds"),
                           on_step=lambda m: None)
        return out, asked

    def test_inquiry_waits_for_the_operator_and_runs_once(self):
        out, asked = self.run_with("passive-auto", True)
        self.assertEqual(asked.count("bt_inquiry"), 1)
        inquiry = [a for a in out["audit"] if a["tool"] == "bt_inquiry"]
        self.assertEqual([a["decision"] for a in inquiry], ["approval_requested", "approved"])
        self.assertEqual(inquiry[-1]["operator"], "alice")

    def test_denied_inquiry_does_not_run(self):
        out, asked = self.run_with("passive-auto", False)
        self.assertEqual([a["decision"] for a in out["audit"] if a["tool"] == "bt_inquiry"], ["approval_requested", "denied"])

    def test_observe_mode_refuses_transmitting(self):
        self.assertEqual(decide("bt_inquiry", "observe"), "deny")


class DashboardTransmitSettingTest(unittest.TestCase):
    """Old scanner options that transmit cannot be switched on without an explicit, recorded authorization."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend, cls.client = BackendValidationTest, BackendValidationTest.backend, BackendValidationTest.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def write(self, bc, **extra):
        b = self.backend
        with b.app.test_request_context("/api/config", method="POST", json={"bluetooth_classic": bc, **extra}):
            from flask import g
            g.auth_mode, g.operator = "off", None
            return b._write_ui_config({"ble"}, set(), bluetooth_classic=bc)

    def test_switching_on_needs_authorization_and_off_clears_it(self):
        b = self.backend
        self.write({"periodic_page_scan": False})
        with self.assertRaises(b.TransmitNotAuthorized):
            self.write({"periodic_page_scan": True})
        self.assertFalse(b._read_ui_config()["bluetooth_classic"]["periodic_page_scan"])  # nothing was written
        saved = self.write({"periodic_page_scan": True}, authorize_transmit=True)
        self.assertIn("periodic_page_scan", saved["transmit_authorization"])
        self.assertTrue(b._authorized_bluetooth_classic(saved["bluetooth_classic"], saved["transmit_authorization"])["periodic_page_scan"])
        self.write({"periodic_page_scan": True})  # already authorized: later saves need no new confirmation
        cleared = self.write({"periodic_page_scan": False})
        self.assertEqual(cleared["transmit_authorization"], {})
        with self.assertRaises(b.TransmitNotAuthorized):
            self.write({"periodic_page_scan": True})  # on again: asks again

    def test_a_setting_without_authorization_never_runs(self):
        b = self.backend
        allowed = b._authorized_bluetooth_classic({"periodic_page_scan": True, "remote_name_lookup": True, "band_hop": True}, {})
        self.assertEqual((allowed["periodic_page_scan"], allowed["remote_name_lookup"], allowed["band_hop"]), (False, False, True))

    def test_discoverable_test_endpoint_needs_authorization(self):
        with patch.object(self.backend, "_enable_discoverable_controller", side_effect=RuntimeError("no controller")):
            refused = self.client.post("/api/test/discoverable-dongle", json={})
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.get_json()["transmit_authorization_required"][0]["option"], "discoverable_test")
            allowed = self.client.post("/api/test/discoverable-dongle", json={"authorize_transmit": True})
            self.assertEqual(allowed.status_code, 500)  # authorized; then fails only because there is no controller


if __name__ == "__main__":
    unittest.main()
