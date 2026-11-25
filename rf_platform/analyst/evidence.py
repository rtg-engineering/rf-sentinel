"""Immutable evidence snapshots and citable evidence references.

Reference formats (all strings):
  snapshot:<label>:<hash12>          collection state of one captured /api/status response
  entity:<entity key>@<snapshot id>  one discovery-table row in that snapshot
  event:<event id>                   one normalized detection (latest snapshot containing it)
  sensor:<device id>@<snapshot id>   one scanner assignment in that snapshot
  computed:<hash12>                  one read-only tool result registered during an investigation
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import urllib.request

# Detections returned by /api/status are capped by the backend; evidence beyond this is not citable.
STATUS_DETECTION_CAP = 120
# Validation fixtures carry test truth. The analyst must never see it.
TRUTH_PREFIX = "scenario_"
_STATUS_KEYS = ("running", "worker_alive", "mode", "discovery_table", "detections", "scanner_assignments")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def short_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:12]


def strip_truth(value: Any) -> Any:
    """Recursively remove scenario/test-truth fields so evaluation cannot leak into analysis."""
    if isinstance(value, dict):
        return {k: strip_truth(v) for k, v in value.items() if not str(k).startswith(TRUTH_PREFIX)}
    if isinstance(value, list):
        return [strip_truth(v) for v in value]
    return value


def event_id(detection: dict[str, Any]) -> str:
    """Source event ID when present (demo/validation); otherwise a content hash of the detection."""
    return str(detection.get("validation_event_id") or f"obs-{short_hash(detection)}")


def identifiers(record: dict[str, Any]) -> set[str]:
    """Normalized identifiers used to link detections to discovery-table entities."""
    values = set()
    for key in ("mac", "address", "source_address", "lap"):
        text = str(record.get(key) or "").strip().upper()
        if text:
            values.add(text)
            values.add(text.removeprefix("LAP "))
    return values


def trim_status(status: dict[str, Any]) -> dict[str, Any]:
    """Keep only what the analyst uses, with test truth and per-event receipts removed."""
    trimmed = {k: copy.deepcopy(status.get(k)) for k in _STATUS_KEYS}
    metrics = status.get("validation_metrics", status.get("collection_metrics"))  # idempotent on trimmed input
    if isinstance(metrics, dict):
        trimmed["collection_metrics"] = {k: copy.deepcopy(v) for k, v in metrics.items() if k != "receipts"}
    return strip_truth(trimmed)


def capture_status(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/status?receipts_after=0", timeout=timeout) as response:
        return trim_status(json.load(response))


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    label: str
    captured_at: str
    status: dict[str, Any]
    entities: dict[str, dict[str, Any]] = field(repr=False)
    observations: dict[str, dict[str, Any]] = field(repr=False)
    sensors: dict[str, dict[str, Any]] = field(repr=False)

    @classmethod
    def from_status(cls, status: dict[str, Any], label: str, captured_at: str | None = None) -> "Snapshot":
        status = trim_status(status)
        label = "".join(ch for ch in label if ch.isalnum() or ch in "-_") or "snapshot"
        entities = {str(row["key"]): row for row in status.get("discovery_table") or [] if row.get("key")}
        observations = {event_id(d): d for d in status.get("detections") or [] if isinstance(d, dict)}
        sensors = {str(k): v for k, v in (status.get("scanner_assignments") or {}).items() if isinstance(v, dict)}
        return cls(snapshot_id=f"snapshot:{label}:{short_hash(status)}", label=label,
                   captured_at=captured_at or datetime.now(timezone.utc).isoformat(), status=status,
                   entities=entities, observations=observations, sensors=sensors)

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "label": self.label, "captured_at": self.captured_at,
                "status": self.status}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Snapshot":
        snapshot = cls.from_status(payload["status"], payload["label"], payload.get("captured_at"))
        if payload.get("snapshot_id") and payload["snapshot_id"] != snapshot.snapshot_id:
            raise ValueError("Snapshot content does not match its recorded snapshot_id")
        return snapshot

    def collection(self) -> dict[str, Any]:
        metrics = self.status.get("collection_metrics") or {}
        return {"snapshot_id": self.snapshot_id, "label": self.label, "captured_at": self.captured_at,
                "running": self.status.get("running"), "worker_alive": self.status.get("worker_alive"),
                "events_accepted": metrics.get("events_accepted"), "events_rejected": metrics.get("events_rejected"),
                "events_dropped": metrics.get("events_dropped"), "replay_complete": metrics.get("complete"),
                "entity_count": len(self.entities), "detections_returned": len(self.observations),
                "detections_cap": STATUS_DETECTION_CAP,
                "detections_may_be_truncated": len(self.observations) >= STATUS_DETECTION_CAP}


class EvidenceStore:
    """Read-only view over ordered snapshots plus computed tool results registered during a run."""

    def __init__(self, snapshots: list[Snapshot]):
        if not snapshots:
            raise ValueError("At least one snapshot is required")
        ids = [s.snapshot_id for s in snapshots]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate snapshot IDs")
        self.snapshots = {s.snapshot_id: s for s in snapshots}
        self.order = ids
        self.computed: dict[str, dict[str, Any]] = {}

    def snapshot(self, snapshot_id: str) -> Snapshot:
        if snapshot_id not in self.snapshots:
            raise KeyError(f"Unknown snapshot: {snapshot_id}")
        return self.snapshots[snapshot_id]

    def register_computed(self, tool: str, args: dict[str, Any], data: dict[str, Any]) -> str:
        result_id = f"computed:{short_hash({'tool': tool, 'args': args, 'data': data})}"
        self.computed[result_id] = {"tool": tool, "args": copy.deepcopy(args), **copy.deepcopy(data)}
        return result_id

    def resolve(self, ref: str) -> dict[str, Any] | None:
        """Return a copy of the referenced record, or None when the reference does not exist."""
        ref = str(ref)
        try:
            if ref.startswith("computed:"):
                record = self.computed.get(ref)
            elif ref.startswith("snapshot:"):
                record = self.snapshots[ref].collection() if ref in self.snapshots else None
            elif ref.startswith("event:"):
                wanted = ref.removeprefix("event:")
                record = next((s.observations[wanted] for s in reversed([self.snapshots[i] for i in self.order])
                               if wanted in s.observations), None)
            elif ref.startswith("entity:") or ref.startswith("sensor:"):
                body, _, snapshot_id = ref.partition("@")
                snapshot = self.snapshots.get(snapshot_id)
                kind, _, key = body.partition(":")
                pool = (snapshot.entities if kind == "entity" else snapshot.sensors) if snapshot else {}
                record = pool.get(key)
            else:
                record = None
        except (KeyError, AttributeError):
            record = None
        return copy.deepcopy(record) if record is not None else None


MISSING = object()


def field_value(record: Any, path: str) -> Any:
    """Read a dotted path (``a.b.0.c``) from a record; MISSING when absent."""
    current = record
    for part in str(path).split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit() and -len(current) <= int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current
