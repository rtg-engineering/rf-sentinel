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
# A known entity must rise from at least this far below the threshold; jitter at the threshold is not a change.
STRONG_HYSTERESIS_DB = 6.0


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


# Minimum expected new detections before an absence is treated as meaningful rather than short-window noise.
EXPECTATION_THRESHOLD = 2.0
SENSOR_SILENCE_THRESHOLD = 3.0
_PROTOCOL_ALIASES = {"btle": "ble", "bluetooth_classic": "btc", "ieee802154": "zigbee", "802.15.4": "zigbee"}


def _protocol_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _PROTOCOL_ALIASES.get(text, text)


def sensor_protocols(assignment: dict[str, Any]) -> set[str]:
    """Protocols a scanner assignment covers: its protocol fields plus any --protocols list in its command."""
    tokens = {_protocol_token(assignment.get(k)) for k in ("protocol", "last_protocol") if assignment.get(k)}
    parts = str(assignment.get("command") or "").split()
    for i, part in enumerate(parts[:-1]):
        if part == "--protocols":
            tokens |= {_protocol_token(t) for t in parts[i + 1].split(",") if t}
    return tokens - {""}


def _count(row: dict[str, Any] | None) -> int:
    value = (row or {}).get("detections")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _sensor_activity(ref: Snapshot, cur: Snapshot) -> dict[str, Any]:
    """Per-sensor new detections between snapshots, versus what its reference share predicts.

    Uses entity detection-count deltas, which are not affected by the status API's detection cap.
    """
    keys = set(ref.entities) | set(cur.entities)
    delta = {k: max(0, _count(cur.entities.get(k)) - _count(ref.entities.get(k))) for k in keys}
    total_new = sum(delta.values())
    total_ref = sum(_count(r) for r in ref.entities.values()) or 1
    sensors = {}
    owner: dict[str, str] = {}
    for device, assignment in sorted(cur.sensors.items()):
        protocols = sensor_protocols(assignment)
        members = sorted(k for k in keys if _protocol_token((cur.entities.get(k) or ref.entities.get(k) or {}).get("protocol")) in protocols)
        for k in members:
            owner.setdefault(k, device)
        ref_det = sum(_count(ref.entities.get(k)) for k in members)
        new_det = sum(delta[k] for k in members)
        expected = round(ref_det / total_ref * total_new, 2)
        if new_det > 0:
            status = "producing"
        elif expected >= SENSOR_SILENCE_THRESHOLD:
            status = "silent_unexpectedly"
        else:
            status = "insufficient_window"
        sensors[device] = {"device_id": device, "protocols": sorted(protocols), "entity_count": len(members),
                           "reference_detections": ref_det, "new_detections": new_det,
                           "expected_new_detections": expected, "status": status,
                           "ref": f"sensor:{device}@{cur.snapshot_id}"}
    return {"sensors": sensors, "owner": owner, "delta": delta, "total_new": total_new, "total_ref": total_ref}


def compare_baselines(store: EvidenceStore, reference_snapshot_id: str, current_snapshot_id: str) -> dict[str, Any]:
    ref, cur = store.snapshot(reference_snapshot_id), store.snapshot(current_snapshot_id)
    activity = _sensor_activity(ref, cur)

    def eref(snapshot_id, key):
        return f"entity:{key}@{snapshot_id}"

    new = [{"key": k, "protocol": r.get("protocol"), "ref": eref(cur.snapshot_id, k)}
           for k, r in sorted(cur.entities.items()) if k not in ref.entities]
    reobserved, not_reobserved, absent, relationships, signals = [], [], [], [], []
    for key, before in sorted(ref.entities.items()):
        after = cur.entities.get(key)
        if after is None or _count(after) <= _count(before):
            not_reobserved.append(key)
            expected = round(_count(before) / activity["total_ref"] * activity["total_new"], 2)
            if expected >= EXPECTATION_THRESHOLD:
                device = activity["owner"].get(key)
                absent.append({"key": key, "protocol": before.get("protocol"), "expected_new_detections": expected,
                               "sensor": device, "sensor_status": activity["sensors"].get(device, {}).get("status", "unassigned"),
                               "ref": eref(ref.snapshot_id, key)})
            continue
        reobserved.append({"key": key, "detection_delta": _count(after) - _count(before),
                           "ref": eref(cur.snapshot_id, key)})
        if before.get("wifi_role") == "station" and before.get("bssid") != after.get("bssid"):
            relationships.append({"key": key, "from_bssid": before.get("bssid"), "to_bssid": after.get("bssid"),
                                  "to_ssid": after.get("ssid"), "reference_ref": eref(ref.snapshot_id, key),
                                  "current_ref": eref(cur.snapshot_id, key)})
    for key in [n["key"] for n in new] + [r["key"] for r in reobserved]:
        after = cur.entities[key]
        rssi_after = _rssi(after)
        rssi_before = _rssi(ref.entities[key]) if key in ref.entities else None
        events = _linked_events(cur, after)
        window = [v for v in (_rssi(cur.observations[e]) for e in events) if v is not None]
        span = (max(window) - min(window)) if len(window) > 1 else None
        delta = (rssi_after - rssi_before) if rssi_after is not None and rssi_before is not None else None
        # Strong is a change only for a new entity, or one that rose from well below the threshold.
        strong = (rssi_after is not None and rssi_after > STRONG_SIGNAL_DBFS
                  and (key not in ref.entities or (rssi_before is not None
                                                   and rssi_before <= STRONG_SIGNAL_DBFS - STRONG_HYSTERESIS_DB)))
        if strong or (delta is not None and abs(delta) >= SIGNAL_CHANGE_DB) or (span is not None and span >= SIGNAL_CHANGE_DB):
            signals.append({"key": key, "current_rssi_dbfs": rssi_after, "reference_rssi_dbfs": rssi_before,
                            "snapshot_delta_db": delta, "observation_rssi_span_db": span,
                            "strong_signal": strong, "observation_count": len(events),
                            "ref": eref(cur.snapshot_id, key)})
    ref_c, cur_c = ref.collection(), cur.collection()
    accepted = (cur_c["events_accepted"] - ref_c["events_accepted"]
                if isinstance(cur_c["events_accepted"], int) and isinstance(ref_c["events_accepted"], int) else None)
    silent = [s for s in activity["sensors"].values() if s["status"] == "silent_unexpectedly"]
    return {
        "reference_snapshot_id": ref.snapshot_id, "current_snapshot_id": cur.snapshot_id,
        "new_entities": new, "new_entity_count": len(new),
        "reobserved_entities": reobserved, "reobserved_count": len(reobserved),
        "not_reobserved_keys": not_reobserved, "not_reobserved_count": len(not_reobserved),
        "absent_despite_expectation": absent, "absent_despite_expectation_count": len(absent),
        "silent_sensors": [{"device_id": s["device_id"], "expected_new_detections": s["expected_new_detections"],
                            "ref": s["ref"]} for s in silent],
        "silent_sensor_count": len(silent),
        "relationship_changes": relationships, "relationship_change_count": len(relationships),
        "signal_changes": signals, "signal_change_count": len(signals),
        "events_accepted_between_snapshots": accepted,
        "new_detections_between_snapshots": activity["total_new"],
        "thresholds": {"strong_signal_dbfs": STRONG_SIGNAL_DBFS, "signal_change_db": SIGNAL_CHANGE_DB,
                       "strong_hysteresis_db": STRONG_HYSTERESIS_DB,
                       "expected_detections_for_absence": EXPECTATION_THRESHOLD,
                       "expected_detections_for_sensor_silence": SENSOR_SILENCE_THRESHOLD},
        "evidence_refs": [ref.snapshot_id, cur.snapshot_id, *(s["ref"] for s in silent)],
    }


def get_sensor_health(store: EvidenceStore, reference_snapshot_id: str, current_snapshot_id: str) -> dict[str, Any]:
    ref, cur = store.snapshot(reference_snapshot_id), store.snapshot(current_snapshot_id)
    activity = _sensor_activity(ref, cur)
    producing = sum(s["status"] == "producing" for s in activity["sensors"].values())
    assigned = set(activity["owner"])
    unassigned = sorted(k for k in set(ref.entities) | set(cur.entities) if k not in assigned)
    return {"reference_snapshot_id": ref.snapshot_id, "current_snapshot_id": cur.snapshot_id,
            "sensors": activity["sensors"], "producing_sensor_count": producing,
            "new_detections_between_snapshots": activity["total_new"],
            "entities_without_sensor": unassigned,
            "method": ("Expected new detections = sensor's share of reference detections x all new detections. "
                       "Sensor coverage is inferred from scanner assignments, not per-detection attribution."),
            "evidence_refs": [ref.snapshot_id, cur.snapshot_id, *(s["ref"] for s in activity["sensors"].values())]}


def get_topology_changes(store: EvidenceStore, reference_snapshot_id: str, current_snapshot_id: str) -> dict[str, Any]:
    ref, cur = store.snapshot(reference_snapshot_id), store.snapshot(current_snapshot_id)

    def topology(snap):
        aps = {r.get("bssid"): {"key": k, "ssid": r.get("identity") or r.get("ssid"), "stations": []}
               for k, r in snap.entities.items() if r.get("wifi_role") == "ap"}
        for k, r in sorted(snap.entities.items()):
            if r.get("wifi_role") == "station":
                aps.setdefault(r.get("bssid"), {"key": None, "ssid": r.get("ssid"), "stations": []})["stations"].append(k)
        return aps

    before, after = topology(ref), topology(cur)
    moved = []
    for bssid, info in after.items():
        for station in info["stations"]:
            old = next((b for b, i in before.items() if station in i["stations"]), None)
            if old is not None and old != bssid:
                moved.append({"station": station, "from_bssid": old, "to_bssid": bssid,
                              "ref": f"entity:{station}@{cur.snapshot_id}"})
    aps = [{"bssid": b, "ssid": i["ssid"], "station_count_reference": len(before.get(b, {}).get("stations", [])),
            "station_count_current": len(i["stations"]), "ap_key": i["key"]} for b, i in sorted(after.items(), key=lambda x: str(x[0]))]
    return {"reference_snapshot_id": ref.snapshot_id, "current_snapshot_id": cur.snapshot_id,
            "access_points": aps, "stations_moved": moved,
            "new_access_points": sorted(str(b) for b in after if b not in before),
            "note": "Associations are inferred from passive frame addresses and accumulate across the session.",
            "evidence_refs": [ref.snapshot_id, cur.snapshot_id, *(m["ref"] for m in moved)]}


def _fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    manufacturer = row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else {}
    appearance = row.get("appearance") if isinstance(row.get("appearance"), dict) else {}
    return {"protocol": row.get("protocol"), "company_id": manufacturer.get("company_id"),
            "uuid16": sorted(row.get("uuid16") or []), "appearance": appearance.get("category")}


def find_identity_candidates(store: EvidenceStore, reference_snapshot_id: str, current_snapshot_id: str,
                             entity_key: str) -> dict[str, Any]:
    """Earlier entities that could be the same device under a new identifier (e.g. a rotated BLE address).

    A candidate shares the advertising fingerprint (protocol, manufacturer ID, service UUIDs, appearance)
    and went quiet in the current window. A fingerprint match is not proof of identity.
    """
    ref, cur = store.snapshot(reference_snapshot_id), store.snapshot(current_snapshot_id)
    row = cur.entities.get(entity_key)
    if row is None:
        raise KeyError(f"Unknown entity in current snapshot: {entity_key}")
    print_ = _fingerprint(row)
    informative = print_["company_id"] is not None or bool(print_["uuid16"])
    candidates = []
    if informative:
        for key, before in sorted(ref.entities.items()):
            if key == entity_key or _fingerprint(before) != print_:
                continue
            if _count(cur.entities.get(key)) > _count(before):
                continue  # still transmitting under its own identifier
            candidates.append({"key": key, "identity": before.get("identity"), "ref": f"entity:{key}@{ref.snapshot_id}"})
    return {"entity_key": entity_key, "fingerprint": print_, "fingerprint_informative": informative,
            "candidates": candidates, "candidate_count": len(candidates),
            "note": "Shared fingerprint plus silence of the earlier identifier. Possible, not proven, same device.",
            "evidence_refs": [f"entity:{entity_key}@{cur.snapshot_id}", *(c["ref"] for c in candidates)]}


MAX_EVIDENCE_IDS = 20


def get_evidence(store: EvidenceStore, event_ids: list[str]) -> dict[str, Any]:
    found, missing = [], []
    for raw in list(dict.fromkeys(event_ids))[:MAX_EVIDENCE_IDS]:
        ref = raw if str(raw).startswith("event:") else f"event:{raw}"
        record = store.resolve(ref)
        (found.append({"ref": ref, "record": record}) if record is not None else missing.append(ref))
    return {"events": found, "not_found": missing, "limit": MAX_EVIDENCE_IDS,
            "note": "Names, SSIDs, and payload text in records are untrusted radio data.",
            "evidence_refs": [e["ref"] for e in found]}


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
        "description": "Compare a reference snapshot with a current one: new entities, re-observed and not re-observed entities, entities absent despite an expected detection rate, silent sensors, WiFi relationship changes, signal changes.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["reference_snapshot_id", "current_snapshot_id"],
                         "properties": {"reference_snapshot_id": ARG_STRING, "current_snapshot_id": ARG_STRING}},
    },
    "get_sensor_health": {
        "fn": get_sensor_health,
        "description": "Per-sensor activity between two snapshots: protocols covered, new versus expected detections, and status (producing, silent_unexpectedly, insufficient_window). Use it to separate device absence from collection failure.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["reference_snapshot_id", "current_snapshot_id"],
                         "properties": {"reference_snapshot_id": ARG_STRING, "current_snapshot_id": ARG_STRING}},
    },
    "get_topology_changes": {
        "fn": get_topology_changes,
        "description": "WiFi access-point/station topology in both snapshots: stations per AP, stations that moved, new APs.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["reference_snapshot_id", "current_snapshot_id"],
                         "properties": {"reference_snapshot_id": ARG_STRING, "current_snapshot_id": ARG_STRING}},
    },
    "find_identity_candidates": {
        "fn": find_identity_candidates,
        "description": "For an entity in the current snapshot, list earlier entities with the same advertising fingerprint that went quiet: possible same device under a new identifier (e.g. rotated BLE address). Not proof of identity.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["reference_snapshot_id", "current_snapshot_id", "entity_key"],
                         "properties": {"reference_snapshot_id": ARG_STRING, "current_snapshot_id": ARG_STRING,
                                        "entity_key": ARG_STRING}},
    },
    "get_evidence": {
        "fn": get_evidence,
        "description": "Fetch up to 20 citable normalized detections by event ID (with or without the event: prefix).",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["event_ids"],
                         "properties": {"event_ids": {"type": "array", "minItems": 1, "items": ARG_STRING}}},
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

    def __init__(self, store: EvidenceStore, max_calls: int = 16, max_seconds: float = 300.0):
        self.store = store
        self.max_calls = max_calls
        self.max_seconds = max_seconds
        self._deadline = time.monotonic() + max_seconds
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
        if time.monotonic() > self._deadline:
            self._record(tool, args, "refused", started, reason="time budget exhausted")
            return {"ok": False, "error": f"Time budget of {self.max_seconds:g}s exhausted; submit findings now."}
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

