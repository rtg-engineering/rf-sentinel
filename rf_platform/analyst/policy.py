"""Independent policy engine for mission actions. No model is involved in any decision here.

Every proposed action is evaluated against the mission's operator-approved scope:
  allow             read-only analysis inside the mission's budget
  require_approval  a restricted action the scope permits; it runs only with an operator approval
                    bound to this exact proposal (content hash), unexpired and unused
  deny              anything outside scope, over a limit, or while the mission is not active
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence import short_hash
from .tools import READ_ONLY_TOOLS, RESTRICTED_TOOLS

# Restricted actions a mission may propose. Each needs explicit approval and is executed only by an
# Executor; the only executor shipped is SimulatedExecutor, which touches no hardware.
MISSION_ACTIONS = {
    "start_passive_capture": "Bounded passive capture on one sensor",
    "modify_scan_schedule": "Change one sensor's scan schedule for a bounded time",
}
ACTIVE_STATES = {"MONITORING", "NEXT_ACTION_DECISION", "AWAITING_APPROVAL"}


@dataclass(frozen=True)
class Decision:
    outcome: str  # allow | require_approval | deny
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)


def proposal_hash(proposal: dict[str, Any]) -> str:
    """What an approval is bound to: the action, its parameters, and its mission, not prose."""
    return short_hash({k: proposal.get(k) for k in ("mission_id", "proposal_id", "action", "params")})


def default_scope() -> dict[str, Any]:
    return {"sensors": "all", "allowed_actions": [], "max_action_duration_s": 600, "max_actions": 3,
            "approval_ttl_s": 3600, "assess_interval_s": 300, "duration_s": 86400, "max_assessments": 48,
            "baseline_name": "default"}


def validate_scope(scope: dict[str, Any]) -> list[str]:
    problems = []
    for key in ("max_action_duration_s", "max_actions", "approval_ttl_s", "assess_interval_s", "duration_s", "max_assessments"):
        value = scope.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            problems.append(f"{key} must be a positive number")
    unknown = [a for a in scope.get("allowed_actions", []) if a not in MISSION_ACTIONS]
    if unknown:
        problems.append(f"allowed_actions contains unsupported actions: {', '.join(unknown)}")
    sensors = scope.get("sensors")
    if sensors != "all" and not (isinstance(sensors, list) and all(isinstance(s, str) and s for s in sensors)):
        problems.append('sensors must be "all" or a list of sensor IDs')
    return problems


class PolicyEngine:
    def __init__(self, scope: dict[str, Any]):
        self.scope = {**default_scope(), **scope}

    def evaluate(self, proposal: dict[str, Any], state: str, actions_executed: int) -> Decision:
        action, params = proposal.get("action"), proposal.get("params") or {}
        if action in READ_ONLY_TOOLS:
            ok = state in ACTIVE_STATES
            return Decision("allow" if ok else "deny",
                            "read-only analysis" if ok else f"mission is {state}", {"mission_active": ok})
        if action not in MISSION_ACTIONS and action not in RESTRICTED_TOOLS:
            return Decision("deny", f"unknown action {action!r}", {"known_action": False})
        sensors = self.scope["sensors"]
        duration = params.get("duration_s")
        checks = {
            "mission_active": state in ACTIVE_STATES,
            "action_in_scope": action in self.scope["allowed_actions"],
            "sensor_in_scope": sensors == "all" or params.get("sensor_id") in sensors,
            "duration_bounded": isinstance(duration, (int, float)) and not isinstance(duration, bool)
                                and 0 < duration <= self.scope["max_action_duration_s"],
            "action_budget_left": actions_executed < self.scope["max_actions"],
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            return Decision("deny", "failed: " + ", ".join(failed), checks)
        return Decision("require_approval", "restricted action within scope; operator approval required", checks)

    def check_approval(self, proposal: dict[str, Any], approval: dict[str, Any] | None, now: float,
                       used_approval_ids: set[str]) -> tuple[bool, str]:
        """An approval is valid only for this proposal's content hash, before expiry, and once."""
        if not approval:
            return False, "no approval"
        if approval.get("approval_id") in used_approval_ids:
            return False, "approval already used"
        if approval.get("proposal_hash") != proposal_hash(proposal):
            return False, "approval does not match this proposal"
        if now > float(approval.get("expires_at", 0)):
            return False, "approval expired"
        return True, "approved"


class SimulatedExecutor:
    """Records what an approved action would do. Touches no hardware and changes no collection."""
    name = "simulated"

    def execute(self, proposal: dict[str, Any]) -> dict[str, Any]:
        return {"executor": self.name, "simulated": True, "action": proposal["action"], "params": proposal["params"],
                "note": "Simulated: no receiver, schedule, or collection setting was changed."}
