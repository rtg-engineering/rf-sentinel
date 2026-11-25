"""Read-only investigation tools and the independent policy gateway that runs them.

Tools are pure functions over an EvidenceStore. They cannot reach ingestion, detection state,
hardware, the network, or the filesystem. Every call, including refusals, is audited.
"""
from __future__ import annotations

from collections import Counter
import copy
import time
from typing import Any

from .evidence import EvidenceStore, Snapshot, identifiers

STRONG_SIGNAL_DBFS = -42.0  # Same threshold the dashboard uses for "Strong local signal; review proximity".
SIGNAL_CHANGE_DB = 15.0


def _rssi(record: dict[str, Any]) -> float | None:
    for key in ("last_rssi_dbfs", "rssi_dbfs", "rssi_dbm"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _linked_events(snapshot: Snapshot, row: dict[str, Any]) -> list[str]:
    ids = identifiers(row)
    return sorted(eid for eid, det in snapshot.observations.items() if ids & identifiers(det))


def get_environment_summary(store: EvidenceStore, snapshot_id: str) -> dict[str, Any]:
    snap = store.snapshot(snapshot_id)
    rows = list(snap.entities.values())
    sensors = {device: {"protocol": a.get("protocol"), "mode": a.get("mode"), "band": a.get("band"),
                        "ref": f"sensor:{device}@{snapshot_id}"} for device, a in sorted(snap.sensors.items())}
    collection = snap.collection()
    notes = []
    if collection["detections_may_be_truncated"]:
        notes.append(f"Only the most recent {collection['detections_cap']} detections are available; older events are not citable.")
    if not sensors:
        notes.append("No sensor assignments were reported; sensor health cannot be assessed.")
    return {
        "snapshot_id": snapshot_id,
        "entity_count": len(rows),
        "entities_by_protocol": dict(sorted(Counter(str(r.get("protocol")) for r in rows).items())),
        "wifi_ap_count": sum(r.get("wifi_role") == "ap" for r in rows),
        "wifi_station_count": sum(r.get("wifi_role") == "station" for r in rows),
        "collection": collection,
        "sensors": sensors,
        "data_quality_notes": notes,
        "evidence_refs": [snapshot_id, *(s["ref"] for s in sensors.values())],
    }


def get_entity_history(store: EvidenceStore, entity_key: str) -> dict[str, Any]:
    history = []
    for snapshot_id in store.order:
        snap = store.snapshots[snapshot_id]
        row = snap.entities.get(entity_key)
        entry = {"snapshot_id": snapshot_id, "present": row is not None}
        if row is not None:
            entry.update(ref=f"entity:{entity_key}@{snapshot_id}", detections=row.get("detections"),
                         last_rssi_dbfs=_rssi(row), wifi_role=row.get("wifi_role"), bssid=row.get("bssid"),
                         ssid=row.get("ssid"), seen_span_days=row.get("seen_span_days"),
                         weekly_cadence=row.get("seen_weekly_cadence"), weekend_only=row.get("seen_weekend_only"))
        history.append(entry)
    if not any(h["present"] for h in history):
        raise KeyError(f"Unknown entity: {entity_key}")
    latest = store.snapshots[next(h["snapshot_id"] for h in reversed(history) if h["present"])]
    events = _linked_events(latest, latest.entities[entity_key])
    observations = [{"ref": f"event:{eid}", "rssi_dbfs": _rssi(latest.observations[eid]),
                     "kind": latest.observations[eid].get("kind")} for eid in events]
    rssi_values = [o["rssi_dbfs"] for o in observations if o["rssi_dbfs"] is not None]
    return {"entity_key": entity_key, "history": history, "observations": observations,
            "observation_count": len(observations),
            "rssi_span_db": (max(rssi_values) - min(rssi_values)) if len(rssi_values) > 1 else None,
            "evidence_refs": [h["ref"] for h in history if h.get("ref")] + [o["ref"] for o in observations]}


def compare_baselines(store: EvidenceStore, reference_snapshot_id: str, current_snapshot_id: str) -> dict[str, Any]:
    ref, cur = store.snapshot(reference_snapshot_id), store.snapshot(current_snapshot_id)

    def eref(snapshot_id, key):
        return f"entity:{key}@{snapshot_id}"

    def count(row):
        value = row.get("detections")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    new = [{"key": k, "protocol": r.get("protocol"), "ref": eref(cur.snapshot_id, k)}
           for k, r in sorted(cur.entities.items()) if k not in ref.entities]
    reobserved, not_reobserved, relationships, signals = [], [], [], []
    for key, before in sorted(ref.entities.items()):
        after = cur.entities.get(key)
        if after is None or count(after) <= count(before):
            not_reobserved.append(key)
            continue
        reobserved.append({"key": key, "detection_delta": count(after) - count(before),
                           "ref": eref(cur.snapshot_id, key)})
        if before.get("wifi_role") == "station" and before.get("bssid") != after.get("bssid"):
            relationships.append({"key": key, "from_bssid": before.get("bssid"), "to_bssid": after.get("bssid"),
                                  "to_ssid": after.get("ssid"), "reference_ref": eref(ref.snapshot_id, key),
                                  "current_ref": eref(cur.snapshot_id, key)})
    changed_keys = [n["key"] for n in new] + [r["key"] for r in reobserved]
    for key in changed_keys:
        after = cur.entities[key]
        rssi_after = _rssi(after)
        rssi_before = _rssi(ref.entities[key]) if key in ref.entities else None
        events = _linked_events(cur, after)
        window = [v for v in (_rssi(cur.observations[e]) for e in events) if v is not None]
        span = (max(window) - min(window)) if len(window) > 1 else None
        delta = (rssi_after - rssi_before) if rssi_after is not None and rssi_before is not None else None
        strong = rssi_after is not None and rssi_after > STRONG_SIGNAL_DBFS
        if strong or (delta is not None and abs(delta) >= SIGNAL_CHANGE_DB) or (span is not None and span >= SIGNAL_CHANGE_DB):
            signals.append({"key": key, "current_rssi_dbfs": rssi_after, "reference_rssi_dbfs": rssi_before,
                            "snapshot_delta_db": delta, "observation_rssi_span_db": span,
                            "strong_signal": strong, "observation_count": len(events),
                            "ref": eref(cur.snapshot_id, key)})
    ref_c, cur_c = ref.collection(), cur.collection()
    accepted = (cur_c["events_accepted"] - ref_c["events_accepted"]
                if isinstance(cur_c["events_accepted"], int) and isinstance(ref_c["events_accepted"], int) else None)
    return {
        "reference_snapshot_id": ref.snapshot_id, "current_snapshot_id": cur.snapshot_id,
        "new_entities": new, "new_entity_count": len(new),
        "reobserved_entities": reobserved, "reobserved_count": len(reobserved),
        "not_reobserved_keys": not_reobserved, "not_reobserved_count": len(not_reobserved),
        "relationship_changes": relationships, "relationship_change_count": len(relationships),
        "signal_changes": signals, "signal_change_count": len(signals),
        "events_accepted_between_snapshots": accepted,
        "thresholds": {"strong_signal_dbfs": STRONG_SIGNAL_DBFS, "signal_change_db": SIGNAL_CHANGE_DB},
        "evidence_refs": [ref.snapshot_id, cur.snapshot_id],
    }


ARG_STRING = {"type": "string", "minLength": 1}
READ_ONLY_TOOLS: dict[str, dict[str, Any]] = {
    "get_environment_summary": {
        "fn": get_environment_summary,
        "description": "Summarize one captured snapshot: entity counts by protocol, WiFi roles, collection health, sensors, data-quality notes.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["snapshot_id"],
                         "properties": {"snapshot_id": ARG_STRING}},
    },
    "get_entity_history": {
        "fn": get_entity_history,
        "description": "Return one entity's presence, detection counts, signal, WiFi association, recurrence across snapshots, and its citable observations.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["entity_key"],
                         "properties": {"entity_key": ARG_STRING}},
    },
    "compare_baselines": {
        "fn": compare_baselines,
        "description": "Compare a reference snapshot with a current one: new entities, re-observed and not re-observed entities, WiFi relationship changes, signal changes.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["reference_snapshot_id", "current_snapshot_id"],
                         "properties": {"reference_snapshot_id": ARG_STRING, "current_snapshot_id": ARG_STRING}},
    },
}

# Known actions that Phase 1 never executes. Naming them gives a specific, auditable refusal.
RESTRICTED_TOOLS = {
    "export_evidence_bundle": "policy_checked",
    "start_passive_capture": "explicit_approval",
    "modify_scan_schedule": "explicit_approval",
    "share_report": "explicit_approval",
}


class ToolGateway:
    """Independent, non-LLM policy enforcement: allowlist, argument validation, budget, audit."""

    def __init__(self, store: EvidenceStore, max_calls: int = 16):
        self.store = store
        self.max_calls = max_calls
        self.audit: list[dict[str, Any]] = []
        self._ok_calls = 0

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [{"name": name, "description": spec["description"], "input_schema": copy.deepcopy(spec["input_schema"])}
                for name, spec in READ_ONLY_TOOLS.items()]

    def _record(self, tool: str, args: Any, status: str, started: float, **extra) -> dict[str, Any]:
        entry = {"sequence": len(self.audit) + 1, "tool": str(tool)[:80], "arguments": copy.deepcopy(args),
                 "status": status, "duration_ms": round((time.perf_counter() - started) * 1000, 3), **extra}
        self.audit.append(entry)
        return entry

    def call(self, tool: str, args: Any) -> dict[str, Any]:
        """Run an allowlisted read-only tool. Returns {ok, result_id?, data?, error?}; never raises."""
        from .schemas import validate
        started = time.perf_counter()
        if tool in RESTRICTED_TOOLS:
            reason = f"'{tool}' requires {RESTRICTED_TOOLS[tool].replace('_', ' ')}; Phase 1 executes no restricted actions"
            self._record(tool, args, "refused", started, reason=reason)
            return {"ok": False, "error": reason}
        spec = READ_ONLY_TOOLS.get(tool)
        if spec is None:
            self._record(tool, args, "refused", started, reason="not in allowlist")
            return {"ok": False, "error": f"Unknown tool '{tool}'. Allowed: {', '.join(READ_ONLY_TOOLS)}"}
        if self._ok_calls >= self.max_calls:
            self._record(tool, args, "refused", started, reason="tool-call budget exhausted")
            return {"ok": False, "error": f"Tool-call budget of {self.max_calls} exhausted; submit findings now."}
        problems = validate(args, spec["input_schema"])
        if problems:
            self._record(tool, args, "error", started, reason="; ".join(problems))
            return {"ok": False, "error": "Invalid arguments: " + "; ".join(problems)}
        try:
            data = spec["fn"](self.store, **args)
        except KeyError as exc:
            self._record(tool, args, "error", started, reason=str(exc))
            return {"ok": False, "error": str(exc).strip("'\"")}
        self._ok_calls += 1
        result_id = self.store.register_computed(tool, args, data)
        self._record(tool, args, "ok", started, result_id=result_id, evidence_ref_count=len(data["evidence_refs"]))
        return {"ok": True, "result_id": result_id, "data": data}

