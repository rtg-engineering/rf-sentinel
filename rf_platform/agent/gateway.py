"""Agent tool gateway: every agent operation is authorized here, in code, whatever its source.

Modes (least to most privileged):
  observe            read-only tools; collection cannot start or change
  approval-required  every hardware-changing operation waits for a named operator's approval,
                     bound to that exact operation (content hash), single-use, with expiry
  passive-auto       bounded receive-only collection and gain changes run automatically

The server sets a ceiling (RF_SENTINEL_AGENT_MODE, default passive-auto). A run may request the same or
a more restrictive mode, never a more privileged one. Transmit-capable operations are not tools at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import threading
import time
from typing import Any
import uuid

MODES = ("observe", "approval-required", "passive-auto")
PRIVILEGE = {m: i for i, m in enumerate(MODES)}
DEFAULT_APPROVAL_TTL_S = 300

# Typed tools and their permission class. Anything not listed here does not exist for the agent.
TOOLS: dict[str, str] = {
    "list_receivers": "read",
    "list_runs": "read",
    "get_run": "read",
    "get_report": "read",
    "query_detections": "read",
    "optimize_gain": "collect",     # bounded level measurements on one receiver
    "start_scan": "collect",        # bounded receive-only decoder run
    "adjust_gain": "collect",       # change gain of an owned scan
    "stop_scan": "control",         # stop a scan the run owns; always permitted
    # Workflow actions (rf_platform.agent.workflow)
    "raise_alert": "control",       # put an alert on the operator's screen
    "tag_device": "control",        # label a detected device in this run's results
    "record_iq": "collect",         # bounded receive-only IQ evidence capture on a free receiver
    "extend_scan": "collect",       # lengthen the run's own collection, within the duration limit
    "deliver_alert": "control",     # send an alert's text to the operator-configured webhook (rf_platform.agent.notify)
}
# Named explicitly so a request for them gets a clear refusal rather than "unknown tool".
NEVER = {"transmit", "page_stimulus", "replay", "jam", "spoof", "raw_driver", "shell", "write_register"}


class PermissionDenied(PermissionError):
    pass


def server_ceiling() -> str:
    mode = os.getenv("RF_SENTINEL_AGENT_MODE", "passive-auto").strip().lower()
    return mode if mode in MODES else "observe"  # an unrecognized setting fails closed


def effective_mode(requested: str | None, ceiling: str | None = None) -> str:
    ceiling = ceiling or server_ceiling()
    requested = (requested or ceiling).strip().lower()
    if requested not in MODES:
        raise PermissionDenied(f"Unknown mode {requested!r}; choose one of {', '.join(MODES)}")
    return requested if PRIVILEGE[requested] <= PRIVILEGE[ceiling] else ceiling


def decide(tool: str, mode: str) -> str:
    """allow | require_approval | deny"""
    if tool in NEVER:
        return "deny"
    kind = TOOLS.get(tool)
    if kind is None:
        return "deny"
    if kind in ("read", "control"):
        return "allow"
    return {"observe": "deny", "approval-required": "require_approval", "passive-auto": "allow"}[mode]


def action_hash(run_id: str, tool: str, params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"run": run_id, "tool": tool, "params": params}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


@dataclass
class Pending:
    action_id: str
    run_id: str
    tool: str
    params: dict[str, Any]
    hash: str
    requested_at: float
    expires_at: float
    decision: str | None = None
    operator: str | None = None
    reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "run_id": self.run_id, "tool": self.tool, "params": self.params,
                "requested_at": self.requested_at, "expires_at": self.expires_at}


class ApprovalBroker:
    """Holds pending operations until a named operator approves or denies them."""

    def __init__(self):
        self._lock = threading.Condition()
        self._pending: dict[str, Pending] = {}

    def request(self, run_id: str, tool: str, params: dict[str, Any], ttl_s: float) -> Pending:
        now = time.time()
        item = Pending(f"A-{uuid.uuid4().hex[:10]}", run_id, tool, dict(params), action_hash(run_id, tool, params),
                       now, now + ttl_s)
        with self._lock:
            self._pending[item.action_id] = item
        return item

    def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [p.public() for p in self._pending.values()
                    if p.decision is None and (run_id is None or p.run_id == run_id)]

    def resolve(self, action_id: str, approve: bool, operator: str, reason: str = "") -> Pending:
        if not str(operator).strip():
            raise PermissionDenied("A named operator is required")
        with self._lock:
            item = self._pending.get(action_id)
            if item is None or item.decision is not None:
                raise PermissionDenied(f"No pending action {action_id}")
            if time.time() > item.expires_at:
                item.decision = "expired"
            else:
                item.decision, item.operator, item.reason = ("approved" if approve else "denied"), operator, reason
            self._lock.notify_all()
            return item

    def wait(self, item: Pending, stop: threading.Event) -> Pending:
        with self._lock:
            while item.decision is None:
                if stop.is_set():
                    item.decision = "cancelled"
                elif time.time() > item.expires_at:
                    item.decision = "expired"
                else:
                    self._lock.wait(0.25)
            self._pending.pop(item.action_id, None)
            return item

    def cancel_run(self, run_id: str) -> None:
        with self._lock:
            for item in self._pending.values():
                if item.run_id == run_id and item.decision is None:
                    item.decision = "cancelled"
            self._lock.notify_all()


class AgentGateway:
    """Per-run policy enforcement point with an audit trail."""

    def __init__(self, mode: str, run_id: str, broker: ApprovalBroker | None = None,
                 stop: threading.Event | None = None, approval_ttl_s: float = DEFAULT_APPROVAL_TTL_S,
                 on_pending=None):
        self.mode, self.run_id = mode, run_id
        self.broker = broker or ApprovalBroker()
        self.stop = stop or threading.Event()
        self.approval_ttl_s = approval_ttl_s
        self.on_pending = on_pending
        self.audit: list[dict[str, Any]] = []

    def record_request(self, operator: str | None, source: str) -> None:
        """The first audit entry of a run: who asked for it (None when the dashboard runs without authentication)."""
        self._log("run", {"source": source}, "requested", operator=operator or "unauthenticated")

    def _log(self, tool: str, params: dict[str, Any], decision: str, **extra) -> None:
        self.audit.append({"t": time.time(), "tool": tool, "params": params, "mode": self.mode,
                           "decision": decision, **extra})

    def authorize(self, tool: str, params: dict[str, Any]) -> None:
        """Return if permitted (waiting for approval when required); raise PermissionDenied otherwise."""
        outcome = decide(tool, self.mode)
        if outcome == "allow":
            self._log(tool, params, "allowed")
            return
        if outcome == "deny":
            why = "transmit-capable operations are never available to the agent" if tool in NEVER else \
                f"{tool} is not permitted in {self.mode} mode" if tool in TOOLS else f"{tool} is not an agent tool"
            self._log(tool, params, "denied", reason=why)
            raise PermissionDenied(why)
        item = self.broker.request(self.run_id, tool, params, self.approval_ttl_s)
        self._log(tool, params, "approval_requested", action_id=item.action_id)
        if self.on_pending:
            self.on_pending(item.public())
        item = self.broker.wait(item, self.stop)
        self._log(tool, params, item.decision, action_id=item.action_id, operator=item.operator, reason=item.reason)
        if item.decision != "approved":
            raise PermissionDenied(f"{tool} was {item.decision}" + (f" by {item.operator}" if item.operator else "")
                                   + (f": {item.reason}" if item.reason else ""))
