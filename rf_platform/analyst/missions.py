"""Bounded investigation missions (roadmap Phase 4).

An operator defines an objective and a scope. After the operator approves the scope, the mission
monitors on a schedule: it runs read-only assessments unattended, turns change findings into ranked
follow-up proposals, and gates every proposal through the independent PolicyEngine. Restricted actions
run only with an operator approval bound to the exact proposal, and only through an Executor. The only
executor shipped is SimulatedExecutor.

State is never stored directly. It is derived by replaying the append-only mission_events table, so
the full history of every mission is auditable.

States: DRAFT -> SCOPE_REVIEW -> APPROVED -> BASELINING -> MONITORING
        MONITORING -> CHANGE_DETECTED -> HYPOTHESIS_REVIEW -> NEXT_ACTION_DECISION
        NEXT_ACTION_DECISION -> AWAITING_APPROVAL | MONITORING (read-only follow-up or no action)
        AWAITING_APPROVAL -> MONITORING (executed, denied, or expired)
        any non-terminal -> PAUSED -> (previous state); CANCELLED; FAILED
        COMPLETED (assessment budget used) and TIMED_OUT (mission duration elapsed) both end with a final report.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from .evidence import EvidenceStore, Snapshot, short_hash
from .memory import EnvironmentMemory, MemoryStoreError, assess
from .policy import PolicyEngine, SimulatedExecutor, default_scope, proposal_hash, validate_scope
from .tools import ToolGateway, _sensor_activity

TERMINAL = {"COMPLETED", "TIMED_OUT", "CANCELLED", "FAILED"}
CHANGE_CATEGORIES = {"new_entity", "relationship_change", "signal_change", "missing_entity", "sensor_gap"}
STATE_AFTER = {"created": "DRAFT", "submitted": "SCOPE_REVIEW", "scope_approved": "APPROVED",
               "scope_rejected": "DRAFT", "started": "BASELINING", "baseline_set": "MONITORING",
               "change_detected": "CHANGE_DETECTED", "hypotheses_reviewed": "HYPOTHESIS_REVIEW",
               "proposals_ranked": "NEXT_ACTION_DECISION", "approval_requested": "AWAITING_APPROVAL",
               "readonly_followup": "MONITORING", "finding_reported": "MONITORING",
               "action_executed": "MONITORING", "execution_refused": "MONITORING", "denied": "MONITORING",
               "approval_expired": "MONITORING", "cancelled": "CANCELLED", "failed": "FAILED",
               "completed": "COMPLETED", "timed_out": "TIMED_OUT"}


class MissionError(ValueError):
    pass


def fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive mission state from its events. Pure function of the event log."""
    s: dict[str, Any] = {"state": None, "before_pause": None, "assessments": 0, "actions_executed": 0,
                         "pending": None, "used_approvals": set(), "pending_outcomes": [], "started_at": None,
                         "last_assessment_at": None, "baseline_id": None, "events": len(events)}
    for e in events:
        t, d = e["type"], e["data"]
        if t == "paused":
            s["before_pause"], s["state"] = s["state"], "PAUSED"
            continue
        if t == "resumed":
            s["state"], s["before_pause"] = s["before_pause"], None
            continue
        if t == "baseline_set":
            s["started_at"], s["baseline_id"], s["last_assessment_at"] = e["at"], d["baseline_id"], e["at"]
        elif t == "assessment":
            s["assessments"] += 1
            s["last_assessment_at"] = e["at"]
        elif t == "approval_requested":
            s["pending"] = {**d["proposal"], "requested_at": e["at"], "proposal_hash": d["proposal_hash"]}
        elif t in ("denied", "approval_expired", "execution_refused", "cancelled"):
            s["pending"] = None
        elif t == "action_executed":
            s["actions_executed"] += 1
            s["used_approvals"].add(d["approval_id"])
            s["pending_outcomes"].append(d["proposal"])
            s["pending"] = None
        elif t == "outcome_recorded":
            s["pending_outcomes"] = [p for p in s["pending_outcomes"] if p["proposal_id"] != d["proposal_id"]]
        elif t == "assessment_skipped":
            s["last_assessment_at"] = e["at"]
        if t in STATE_AFTER:
            s["state"] = STATE_AFTER[t]
        if t == "assessment" and s["pending"]:
            s["state"] = "AWAITING_APPROVAL"  # monitoring continues while a proposal awaits a decision
    return s


def _gain(finding: dict[str, Any]) -> float:
    """Information-gain heuristic: log2 of the explanations still in play (not weakened). Not calibrated."""
    live = [h for h in finding.get("hypotheses", []) if h.get("status") != "weakened"]
    return round(math.log2(len(live)), 3) if len(live) > 1 else 0.0


def _target_entities(finding: dict[str, Any]) -> list[str]:
    keys = []
    for fact in finding.get("observed_facts", []):
        ref = str(fact.get("ref", ""))
        if ref.startswith("entity:") and "@snapshot:" in ref:
            key = ref[7:ref.rindex("@snapshot:")]
            if key not in keys:
                keys.append(key)
    return keys


def propose(mission_id: str, investigation: dict[str, Any], store: EvidenceStore, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn change findings into follow-up proposals, ranked by expected information gain per unit cost."""
    scope = {**default_scope(), **scope}
    ref, cur = (store.snapshot(investigation["scope"][k]) for k in ("reference_snapshot_id", "current_snapshot_id"))
    owner = _sensor_activity(ref, cur)["owner"]
    capture_s = min(300, scope["max_action_duration_s"])
    proposals = []

    def add(finding, action, params, rationale):
        cost = 1.0 if action.startswith("get_") else 2.0 + params.get("duration_s", 0) / 300
        gain = _gain(finding)
        proposal = {"mission_id": mission_id, "action": action, "params": params, "finding_id": finding["finding_id"],
                    "finding_category": finding["category"], "target": params.get("entity_key") or params.get("sensor_id"),
                    "rationale": rationale, "expected_gain": gain, "cost": round(cost, 3),
                    "score": round(gain / cost, 3)}
        proposal["proposal_id"] = f"P-{short_hash({k: proposal[k] for k in ('mission_id', 'action', 'params', 'finding_id')})}"
        proposals.append(proposal)

    for f in investigation.get("findings", []):
        cat, keys = f["category"], _target_entities(f)
        if cat == "missing_entity" and keys and owner.get(keys[0]):
            add(f, "start_passive_capture", {"sensor_id": owner[keys[0]], "duration_s": capture_s, "entity_key": keys[0]},
                "A bounded passive capture on its sensor separates 'stopped transmitting' from 'moved channel or identifier'.")
        elif cat == "signal_change" and keys and owner.get(keys[0]) and f.get("confidence") == "low":
            add(f, "start_passive_capture", {"sensor_id": owner[keys[0]], "duration_s": capture_s, "entity_key": keys[0]},
                "More samples confirm or weaken the proximity explanation for a reading based on few observations.")
        elif cat == "sensor_gap":
            sensor = next((fact["value"] for fact in f["observed_facts"] if fact["field"] == "device_id"), None)
            if sensor:
                add(f, "get_sensor_health", {"reference_snapshot_id": ref.snapshot_id, "current_snapshot_id": cur.snapshot_id,
                                             "sensor_id": sensor},
                    "Re-check the sensor's activity before any action on it.")
                add(f, "modify_scan_schedule", {"sensor_id": sensor, "duration_s": capture_s, "change": "restart assignment"},
                    "Restarting the sensor's assignment tests the collection-fault explanation directly.")
        elif cat in ("new_entity", "relationship_change") and keys:
            add(f, "get_entity_history", {"entity_key": keys[0]},
                "Entity history across snapshots adds context at no collection cost.")
    return sorted(proposals, key=lambda p: (-p["score"], p["proposal_id"]))


def _hypothesis_states(investigation: dict[str, Any], proposal: dict[str, Any]) -> dict[str, str] | None:
    for f in investigation.get("findings", []):
        if f["category"] == proposal["finding_category"] and proposal["target"] in str(f):
            return {h["statement"]: h["status"] for h in f["hypotheses"]}
    return None


class Missions:
    def __init__(self, memory: EnvironmentMemory, executor=None, clock: Callable[[], float] = time.time):
        self.memory = memory
        self.executor = executor or SimulatedExecutor()
        self.clock = clock

    # Event log ----------------------------------------------------------------------------------
    def _event(self, mid: str, type_: str, actor: str = "mission", **data) -> None:
        self.memory.append_mission_event(mid, self.clock(), type_, actor, data)

    def state(self, mid: str) -> dict[str, Any]:
        return fold(self.memory.mission_events(mid))

    def _require(self, mid: str, *states: str) -> dict[str, Any]:
        s = self.state(mid)
        if s["state"] not in states:
            raise MissionError(f"Mission is {s['state']}; this needs {' or '.join(states)}")
        return s

    def _spec(self, mid: str) -> dict[str, Any]:
        return self.memory.mission_spec(mid)

    # Operator commands -------------------------------------------------------------------------
    def create(self, name: str, objective: str, scope: dict[str, Any], created_by: str = "operator") -> str:
        scope = {**default_scope(), **scope}
        problems = validate_scope(scope)
        if problems or not str(objective).strip():
            raise MissionError("; ".join(problems or ["objective is required"]))
        spec = {"name": name, "objective": objective, "scope": scope}
        mid = f"M-{short_hash({**spec, 'created': self.clock()})}"
        self.memory.add_mission(mid, spec, created_by)
        self._event(mid, "created", created_by, spec=spec)
        return mid

    def submit(self, mid: str, actor: str = "operator") -> None:
        self._require(mid, "DRAFT")
        self._event(mid, "submitted", actor)

    def approve_scope(self, mid: str, operator: str) -> None:
        self._require(mid, "SCOPE_REVIEW")
        self._event(mid, "scope_approved", operator, scope=self._spec(mid)["scope"])

    def reject_scope(self, mid: str, operator: str, reason: str) -> None:
        self._require(mid, "SCOPE_REVIEW")
        self._event(mid, "scope_rejected", operator, reason=reason)

    def start(self, mid: str, baseline: Snapshot, operator: str) -> None:
        self._require(mid, "APPROVED")
        self._event(mid, "started", operator)
        record = self.memory.set_baseline(baseline, name=f"mission:{mid}", author=operator,
                                          reason=f"Mission {mid} baseline")
        self._event(mid, "baseline_set", operator, baseline_id=record["baseline_id"], snapshot_id=baseline.snapshot_id)

    def pause(self, mid: str, operator: str, reason: str = "") -> None:
        s = self.state(mid)
        if s["state"] in TERMINAL or s["state"] in ("PAUSED", "DRAFT", "SCOPE_REVIEW"):
            raise MissionError(f"Cannot pause a mission that is {s['state']}")
        self._event(mid, "paused", operator, reason=reason)

    def resume(self, mid: str, operator: str) -> None:
        self._require(mid, "PAUSED")
        self._event(mid, "resumed", operator)

    def cancel(self, mid: str, operator: str, reason: str = "") -> None:
        s = self.state(mid)
        if s["state"] in TERMINAL:
            raise MissionError(f"Mission already {s['state']}")
        self._event(mid, "cancelled", operator, reason=reason,
                    voided_proposal=s["pending"]["proposal_id"] if s["pending"] else None)

    def approve(self, mid: str, proposal_id: str, operator: str) -> dict[str, Any]:
        """Approve the pending proposal; it is then executed at once through policy and the executor."""
        s = self._require(mid, "AWAITING_APPROVAL")
        pending = s["pending"]
        if pending["proposal_id"] != proposal_id:
            raise MissionError(f"{proposal_id} is not the pending proposal")
        ttl = self._spec(mid)["scope"]["approval_ttl_s"]
        approval = {"approval_id": f"A-{short_hash({'p': proposal_id, 'by': operator, 'at': self.clock()})}",
                    "proposal_id": proposal_id, "proposal_hash": proposal_hash(pending),
                    "approved_by": operator, "expires_at": self.clock() + ttl}
        self._event(mid, "approved", operator, approval=approval)
        return self._execute(mid, pending, approval)

    def deny(self, mid: str, proposal_id: str, operator: str, reason: str) -> None:
        s = self._require(mid, "AWAITING_APPROVAL")
        if s["pending"]["proposal_id"] != proposal_id:
            raise MissionError(f"{proposal_id} is not the pending proposal")
        self._event(mid, "denied", operator, proposal_id=proposal_id, reason=reason)

    # Execution: the only path to an Executor --------------------------------------------------
    def _execute(self, mid: str, proposal: dict[str, Any], approval: dict[str, Any] | None) -> dict[str, Any]:
        s = self.state(mid)
        policy = PolicyEngine(self._spec(mid)["scope"])
        decision = policy.evaluate(proposal, s["state"], s["actions_executed"])
        ok, reason = (False, decision.reason) if decision.outcome == "deny" else \
            policy.check_approval(proposal, approval, self.clock(), s["used_approvals"])
        if decision.outcome == "allow":
            ok, reason = False, "read-only actions are not executed through the executor"
        if not ok:
            self._event(mid, "execution_refused", "policy", proposal_id=proposal["proposal_id"], reason=reason)
            return {"executed": False, "reason": reason}
        result = self.executor.execute(proposal)
        clean = {k: v for k, v in proposal.items() if k not in ("requested_at", "proposal_hash")}
        self._event(mid, "action_executed", "executor", proposal=clean, approval_id=approval["approval_id"], result=result)
        return {"executed": True, "result": result}

    # Scheduled work ----------------------------------------------------------------------------
    def tick(self, mid: str, capture: Callable[[], Snapshot | None]) -> str:
        """Advance the mission by at most one scheduled step. Returns the resulting state."""
        s = self.state(mid)
        if s["state"] in TERMINAL or s["state"] in ("PAUSED", "DRAFT", "SCOPE_REVIEW", "APPROVED", "BASELINING"):
            return s["state"]
        scope = self._spec(mid)["scope"]
        now = self.clock()
        try:
            if now >= s["started_at"] + scope["duration_s"]:
                return self._finish(mid, "timed_out", "Mission duration elapsed")
            if s["pending"] and now > s["pending"]["requested_at"] + scope["approval_ttl_s"]:
                self._event(mid, "approval_expired", "policy", proposal_id=s["pending"]["proposal_id"])
                s = self.state(mid)
            if now < s["last_assessment_at"] + scope["assess_interval_s"]:
                return s["state"]
            snapshot = capture()
            if snapshot is None:
                self._event(mid, "assessment_skipped", reason="no capture available")
                return self.state(mid)["state"]
            try:
                inv = assess(self.memory, snapshot, against="last", baseline_name=f"mission:{mid}")
            except MemoryStoreError as exc:
                self._event(mid, "assessment_skipped", reason=str(exc))
                return self.state(mid)["state"]
            changes = [f for f in inv["findings"] if f["category"] in CHANGE_CATEGORIES]
            self._event(mid, "assessment", assessment_seq=inv["memory"]["assessment_seq"],
                        investigation_id=inv["investigation_id"], grounded=inv["grounding"]["passed"],
                        change_findings=[f["finding_id"] for f in changes])
            for prior in s["pending_outcomes"]:
                before = prior.get("hypotheses_before")
                after = _hypothesis_states(inv, prior)
                self._event(mid, "outcome_recorded", proposal_id=prior["proposal_id"],
                            outcome="no longer reported" if after is None else ("explanations changed" if after != before else "unchanged"),
                            before=before, after=after, investigation_id=inv["investigation_id"])
            s = self.state(mid)
            if changes and not s["pending"]:
                self._decide(mid, inv, scope)
            if self.state(mid)["assessments"] >= scope["max_assessments"]:
                return self._finish(mid, "completed", "Assessment budget used")
        except Exception as exc:  # A failure ends the mission visibly rather than leaving it half-run.
            self._event(mid, "failed", reason=f"{type(exc).__name__}: {exc}"[:500])
        return self.state(mid)["state"]

    def _decide(self, mid: str, inv: dict[str, Any], scope: dict[str, Any]) -> None:
        self._event(mid, "change_detected", investigation_id=inv["investigation_id"],
                    findings=[f["finding_id"] for f in inv["findings"] if f["category"] in CHANGE_CATEGORIES])
        self._event(mid, "hypotheses_reviewed", hypotheses={f["finding_id"]: [(h["kind"], h["status"]) for h in f["hypotheses"]]
                                                            for f in inv["findings"]})
        scope_ = inv["scope"]
        store = EvidenceStore([self.memory.snapshot(scope_["reference_snapshot_id"]), self.memory.snapshot(scope_["current_snapshot_id"])])
        policy = PolicyEngine(scope)
        s = self.state(mid)
        ranked = []
        for p in propose(mid, inv, store, scope):
            finding = next(f for f in inv["findings"] if f["finding_id"] == p["finding_id"])
            p["hypotheses_before"] = {h["statement"]: h["status"] for h in finding["hypotheses"]}
            decision = policy.evaluate(p, "NEXT_ACTION_DECISION", s["actions_executed"])
            ranked.append({**p, "policy": decision.outcome, "policy_reason": decision.reason})
        self._event(mid, "proposals_ranked", proposals=ranked)
        actionable = [p for p in ranked if p["policy"] != "deny"]
        if not actionable:
            self._event(mid, "finding_reported", investigation_id=inv["investigation_id"],
                        note="No follow-up within scope; findings reported.")
            return
        top = actionable[0]
        if top["policy"] == "allow":
            gateway = ToolGateway(store, max_calls=4)
            args = {k: v for k, v in top["params"].items() if k != "sensor_id"}
            result = gateway.call(top["action"], args)
            self._event(mid, "readonly_followup", proposal_id=top["proposal_id"], action=top["action"],
                        ok=result["ok"], result_id=result.get("result_id"), error=result.get("error"))
        else:
            self._event(mid, "approval_requested", proposal=top, proposal_hash=proposal_hash(top))

    def _finish(self, mid: str, kind: str, reason: str) -> str:
        events = self.memory.mission_events(mid)
        s = fold(events)
        summary = {"reason": reason, "assessments": s["assessments"], "actions_executed": s["actions_executed"],
                   "proposals": sum(len(e["data"].get("proposals", [])) for e in events if e["type"] == "proposals_ranked"),
                   "approvals_requested": sum(e["type"] == "approval_requested" for e in events),
                   "denied": sum(e["type"] == "denied" for e in events),
                   "refused_by_policy": sum(e["type"] == "execution_refused" for e in events),
                   "outcomes": [{"proposal_id": e["data"]["proposal_id"], "outcome": e["data"]["outcome"]}
                                for e in events if e["type"] == "outcome_recorded"],
                   "voided_pending": s["pending"]["proposal_id"] if s["pending"] else None,
                   "executor": self.executor.name}
        self._event(mid, kind, final_report=summary)
        return self.state(mid)["state"]

    def report(self, mid: str) -> dict[str, Any]:
        events = self.memory.mission_events(mid)
        s = fold(events)
        return {"mission_id": mid, "spec": self._spec(mid), "state": s["state"], "assessments": s["assessments"],
                "actions_executed": s["actions_executed"], "pending": s["pending"],
                "events": [{"seq": e["seq"], "at": e["at"], "type": e["type"], "actor": e["actor"], "data": e["data"]}
                           for e in events]}
