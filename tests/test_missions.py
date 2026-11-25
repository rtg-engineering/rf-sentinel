"""Phase 4: bounded missions. Exit gate: finish, pause, and cancel predictably; nothing restricted runs unapproved."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from rf_platform.analyst.evidence import EvidenceStore, Snapshot
from rf_platform.analyst.investigate import run_investigation
from rf_platform.analyst.memory import EnvironmentMemory
from rf_platform.analyst.missions import MissionError, Missions, fold, propose
from rf_platform.analyst.policy import PolicyEngine, proposal_hash, validate_scope

CHAIN = Path(__file__).parent / "fixtures" / "analyst_memory"
SCENARIOS = Path(__file__).parent / "fixtures" / "analyst"
SCOPE = {"allowed_actions": ["start_passive_capture"], "assess_interval_s": 60, "max_assessments": 3,
         "duration_s": 3600, "approval_ttl_s": 600}


def day(name):
    return Snapshot.from_dict(json.loads((CHAIN / f"{name}.json").read_text()))


class SpyExecutor:
    name = "spy"

    def __init__(self):
        self.calls = []

    def execute(self, proposal):
        self.calls.append(proposal["proposal_id"])
        return {"executor": "spy", "simulated": True}


class MissionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.spy = SpyExecutor()
        self.memory = EnvironmentMemory(Path(self.temp.name) / "m.sqlite")
        self.m = Missions(self.memory, executor=self.spy, clock=lambda: self.now[0])
        self.queue = [day("day2"), day("day3"), day("day4")]

    def tearDown(self):
        self.temp.cleanup()

    def started(self, **scope):
        mid = self.m.create("Lab watch", "Report changes in the lab environment", {**SCOPE, **scope})
        self.m.submit(mid)
        self.m.approve_scope(mid, "ops-lead")
        self.m.start(mid, day("day1"), "ops-lead")
        return mid

    def tick(self, mid, seconds=61):
        self.now[0] += seconds
        return self.m.tick(mid, lambda: self.queue.pop(0) if self.queue else None)

    def to_pending(self, **scope):
        mid = self.started(**scope)
        self.assertEqual(self.tick(mid), "MONITORING")          # day2: nothing changed
        self.assertEqual(self.tick(mid), "AWAITING_APPROVAL")   # day3: AP outage -> capture proposal
        return mid, self.m.state(mid)["pending"]

    # Lifecycle -----------------------------------------------------------------------------------
    def test_full_lifecycle_completes_with_outcome(self):
        mid, pending = self.to_pending()
        self.assertEqual((pending["action"], pending["params"]["sensor_id"]), ("start_passive_capture", "wlan-demo0"))
        self.assertTrue(self.m.approve(mid, pending["proposal_id"], "ops-lead")["executed"])
        self.assertEqual(self.spy.calls, [pending["proposal_id"]])
        self.assertEqual(self.tick(mid), "COMPLETED")           # day4: AP back; budget of 3 assessments used
        report = self.m.report(mid)
        outcome = next(e for e in report["events"] if e["type"] == "outcome_recorded")
        self.assertEqual(outcome["data"]["outcome"], "no longer reported")
        final = report["events"][-1]["data"]["final_report"]
        self.assertEqual((final["assessments"], final["actions_executed"], final["approvals_requested"]), (3, 1, 1))

    def test_scope_must_be_reviewed_and_valid(self):
        with self.assertRaises(MissionError):
            self.m.create("x", "objective", {"allowed_actions": ["transmit_jammer"]})
        with self.assertRaises(MissionError):
            self.m.create("x", "", {})
        mid = self.m.create("x", "objective", SCOPE)
        with self.assertRaisesRegex(MissionError, "APPROVED"):
            self.m.start(mid, day("day1"), "ops")
        self.m.submit(mid)
        self.m.reject_scope(mid, "ops-lead", "too broad")
        self.assertEqual(self.m.state(mid)["state"], "DRAFT")
        self.assertEqual(self.tick(mid), "DRAFT")  # nothing runs before scope approval

    def test_out_of_scope_action_is_never_requested(self):
        mid = self.started(allowed_actions=[])
        self.tick(mid)
        self.assertEqual(self.tick(mid), "MONITORING")
        events = self.m.report(mid)["events"]
        ranked = next(e for e in events if e["type"] == "proposals_ranked")["data"]["proposals"]
        self.assertEqual({p["policy"] for p in ranked}, {"deny"})
        self.assertIn("finding_reported", [e["type"] for e in events])
        self.assertNotIn("approval_requested", [e["type"] for e in events])

    # Approval gating -------------------------------------------------------------------------
    def test_restricted_action_never_runs_without_valid_approval(self):
        mid, pending = self.to_pending()
        clean = {k: v for k, v in pending.items() if k not in ("requested_at", "proposal_hash")}
        good = {"approval_id": "A-1", "proposal_id": clean["proposal_id"], "proposal_hash": proposal_hash(clean),
                "expires_at": self.now[0] + 60}
        cases = {"no approval": None,
                 "wrong proposal": {**good, "proposal_hash": "000000000000"},
                 "expired": {**good, "expires_at": self.now[0] - 1}}
        for name, approval in cases.items():
            with self.subTest(case=name):
                self.assertFalse(self.m._execute(mid, clean, approval)["executed"])
        tampered = {**clean, "params": {**clean["params"], "duration_s": 600}}
        self.assertFalse(self.m._execute(mid, tampered, good)["executed"])  # approval is bound to content
        self.assertEqual(self.spy.calls, [])
        refusals = [e for e in self.m.report(mid)["events"] if e["type"] == "execution_refused"]
        self.assertEqual(len(refusals), 4)

    def test_approval_is_single_use(self):
        mid, pending = self.to_pending()
        self.m.approve(mid, pending["proposal_id"], "ops-lead")
        used = next(e for e in self.m.report(mid)["events"] if e["type"] == "approved")["data"]["approval"]
        self.assertFalse(self.m._execute(mid, {k: v for k, v in pending.items()
                                               if k not in ("requested_at", "proposal_hash")}, used)["executed"])
        self.assertEqual(len(self.spy.calls), 1)

    def test_deny_and_expiry(self):
        mid, pending = self.to_pending()
        self.m.deny(mid, pending["proposal_id"], "ops-lead", "not during business hours")
        self.assertEqual(self.m.state(mid)["state"], "MONITORING")
        with self.assertRaises(MissionError):
            self.m.approve(mid, pending["proposal_id"], "ops-lead")
        mid2, pending2 = self.to_pending_new()
        self.now[0] += SCOPE["approval_ttl_s"] + 1
        self.m.tick(mid2, lambda: None)
        types = [e["type"] for e in self.m.report(mid2)["events"]]
        self.assertIn("approval_expired", types)
        with self.assertRaises(MissionError):
            self.m.approve(mid2, pending2["proposal_id"], "ops-lead")
        self.assertEqual(self.spy.calls, [])

    def to_pending_new(self):
        """A second, independent mission (fresh memory) brought to AWAITING_APPROVAL."""
        self.queue = [day("day2"), day("day3"), day("day4")]
        self.m = Missions(EnvironmentMemory(Path(self.temp.name) / "m2.sqlite"), executor=self.spy, clock=lambda: self.now[0])
        return self.to_pending()

    # Pause, cancel, timeout, failure ----------------------------------------------------------
    def test_pause_stops_work_and_resume_restores_state(self):
        mid, pending = self.to_pending()
        self.m.pause(mid, "ops-lead", "site visit")
        before = self.m.state(mid)["assessments"]
        for _ in range(3):
            self.assertEqual(self.tick(mid), "PAUSED")
        self.assertEqual(self.m.state(mid)["assessments"], before)
        with self.assertRaises(MissionError):
            self.m.approve(mid, pending["proposal_id"], "ops-lead")  # approvals are void while paused
        self.m.resume(mid, "ops-lead")
        self.assertEqual(self.m.state(mid)["state"], "AWAITING_APPROVAL")
        self.assertTrue(self.m.approve(mid, pending["proposal_id"], "ops-lead")["executed"])

    def test_cancel_voids_pending_and_stops(self):
        mid, pending = self.to_pending()
        self.m.cancel(mid, "ops-lead", "objective met elsewhere")
        state = self.m.state(mid)
        self.assertEqual((state["state"], state["pending"]), ("CANCELLED", None))
        self.assertEqual(self.tick(mid), "CANCELLED")
        with self.assertRaises(MissionError):
            self.m.approve(mid, pending["proposal_id"], "ops-lead")
        with self.assertRaises(MissionError):
            self.m.cancel(mid, "ops-lead")
        self.assertEqual(self.spy.calls, [])
        cancelled = next(e for e in self.m.report(mid)["events"] if e["type"] == "cancelled")
        self.assertEqual(cancelled["data"]["voided_proposal"], pending["proposal_id"])

    def test_timeout_ends_with_final_report(self):
        mid, pending = self.to_pending()
        self.assertEqual(self.tick(mid, seconds=SCOPE["duration_s"]), "TIMED_OUT")
        final = self.m.report(mid)["events"][-1]["data"]["final_report"]
        self.assertEqual(final["voided_pending"], pending["proposal_id"])
        self.assertEqual(final["executor"], "spy")

    def test_failure_is_recorded_not_hidden(self):
        mid = self.started()
        self.now[0] += 61

        def broken():
            raise ConnectionError("collector offline")
        self.assertEqual(self.m.tick(mid, broken), "FAILED")
        failed = self.m.report(mid)["events"][-1]
        self.assertEqual(failed["type"], "failed")
        self.assertIn("collector offline", failed["data"]["reason"])

    def test_not_yet_due_and_no_capture(self):
        mid = self.started()
        self.assertEqual(self.tick(mid, seconds=10), "MONITORING")  # interval not reached
        self.assertEqual(self.m.state(mid)["assessments"], 0)
        self.queue = []
        self.assertEqual(self.tick(mid), "MONITORING")
        self.assertEqual(self.m.report(mid)["events"][-1]["type"], "assessment_skipped")

    # Audit ---------------------------------------------------------------------------------------
    def test_state_is_derived_from_append_only_events(self):
        mid, _ = self.to_pending()
        events = self.memory.mission_events(mid)
        self.assertEqual(fold(events)["state"], "AWAITING_APPROVAL")
        db = sqlite3.connect(self.memory.path)
        try:
            for statement in ("UPDATE mission_events SET type = 'action_executed'", "DELETE FROM mission_events",
                              "UPDATE missions SET spec = '{}'"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    db.execute(statement)
        finally:
            db.close()


class PolicyAndRankingTest(unittest.TestCase):
    def test_policy_limits(self):
        policy = PolicyEngine({"allowed_actions": ["start_passive_capture"], "sensors": ["wlan-demo0"],
                               "max_action_duration_s": 300, "max_actions": 1})
        base = {"action": "start_passive_capture", "params": {"sensor_id": "wlan-demo0", "duration_s": 300}}
        self.assertEqual(policy.evaluate(base, "NEXT_ACTION_DECISION", 0).outcome, "require_approval")
        for name, proposal, executed, state in (
                ("other sensor", {**base, "params": {"sensor_id": "demo-hackrf:0", "duration_s": 300}}, 0, "MONITORING"),
                ("too long", {**base, "params": {"sensor_id": "wlan-demo0", "duration_s": 3600}}, 0, "MONITORING"),
                ("budget used", base, 1, "MONITORING"),
                ("inactive", base, 0, "PAUSED"),
                ("unknown action", {"action": "transmit", "params": {}}, 0, "MONITORING")):
            with self.subTest(case=name):
                self.assertEqual(policy.evaluate(proposal, state, executed).outcome, "deny")
        self.assertEqual(policy.evaluate({"action": "get_entity_history", "params": {}}, "MONITORING", 0).outcome, "allow")
        self.assertEqual(policy.evaluate({"action": "get_entity_history", "params": {}}, "CANCELLED", 0).outcome, "deny")
        self.assertTrue(validate_scope({"sensors": "all", "allowed_actions": ["start_passive_capture"], "max_action_duration_s": 1,
                                        "max_actions": 1, "approval_ttl_s": 1, "assess_interval_s": 1, "duration_s": 1,
                                        "max_assessments": 0}))

    def test_read_only_follow_up_outranks_restricted_for_sensor_gap(self):
        store = EvidenceStore([Snapshot.from_dict(json.loads((SCENARIOS / "wifi_sensor_outage" / f"{n}.json").read_text()))
                               for n in ("baseline", "current")])
        inv = run_investigation(store)
        ranked = propose("M-test", inv, store, {"allowed_actions": ["modify_scan_schedule"]})
        self.assertEqual([p["action"] for p in ranked], ["get_sensor_health", "modify_scan_schedule"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[1]["params"]["sensor_id"], "wlan-demo0")


if __name__ == "__main__":
    unittest.main()
