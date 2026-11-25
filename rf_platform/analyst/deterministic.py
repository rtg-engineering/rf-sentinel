"""Rule-based analyst provider: needs no model, and is the baseline every LLM provider is compared to.

It uses only the ToolGateway (the same allowlisted tools an LLM gets) and states every fact as
(ref, field, value) so the grounding checker can re-read it from evidence.
"""
from __future__ import annotations

from typing import Any

from .evidence import MISSING, field_value
from .tools import STRONG_SIGNAL_DBFS, ToolGateway

LIMITATIONS = [
    "Assessment is built from captured /api/status snapshots. Observations beyond the backend's returned detection window are not citable.",
    "RF observations identify emitters and associations, not people or intent. No finding is evidence of hostile activity.",
    "Not being re-observed between two snapshots is missing evidence, not proof that a transmitter is gone.",
]
SYNTHETIC_LIMITATION = ("Evidence comes from synthetic demo replay. It does not demonstrate live decoder accuracy, "
                        "receiver sensitivity, or performance in an uncontrolled environment.")


def fact(statement: str, ref: str, record: dict[str, Any], path: str) -> dict[str, Any]:
    value = field_value(record, path)
    if value is MISSING or isinstance(value, (dict, list)):
        raise ValueError(f"Fact path {path} is missing or not a scalar")
    return {"statement": statement, "ref": ref, "field": path, "value": value}


def index_of(items: list[dict[str, Any]], key: str) -> int:
    return next(i for i, item in enumerate(items) if item.get("key") == key)


class DeterministicAnalyst:
    name = "deterministic"
    model = None

    def investigate(self, gateway: ToolGateway, scope: dict[str, Any]) -> dict[str, Any]:
        ref_id, cur_id = scope["reference_snapshot_id"], scope["current_snapshot_id"]
        ref_env = gateway.call("get_environment_summary", {"snapshot_id": ref_id})
        cur_env = gateway.call("get_environment_summary", {"snapshot_id": cur_id})
        cmp_ = gateway.call("compare_baselines", {"reference_snapshot_id": ref_id, "current_snapshot_id": cur_id})
        if not (ref_env["ok"] and cur_env["ok"] and cmp_["ok"]):
            raise RuntimeError("Required read-only tools failed: " + "; ".join(
                r.get("error", "") for r in (ref_env, cur_env, cmp_) if not r["ok"]))
        comparison, cid = cmp_["data"], cmp_["result_id"]
        store = gateway.store
        findings: list[dict[str, Any]] = []
        histories: dict[str, dict[str, Any]] = {}

        def history(key):
            if key not in histories:
                result = gateway.call("get_entity_history", {"entity_key": key})
                histories[key] = result if result["ok"] else None
            return histories[key]

        def entity(ref):
            return store.resolve(ref) or {}

        for item in comparison["new_entities"]:
            key, eref = item["key"], item["ref"]
            row = entity(eref)
            i = index_of(comparison["new_entities"], key)
            facts = [fact(f"{row.get('identity') or key} is present in the current snapshot", eref, row, "key"),
                     fact("Detections recorded for this entity", eref, row, "detections")]
            hyps = [
                {"hypothesis_id": "h1", "statement": "A device that was not transmitting during the reference window began transmitting.",
                 "kind": "benign", "status": "open", "supporting_refs": [eref], "weakening_refs": []},
                {"hypothesis_id": "h2", "statement": "The device was present before but was not received during the reference window (coverage or collection gap).",
                 "kind": "sensor_or_collection", "status": "open", "supporting_refs": [], "weakening_refs": [ref_id]},
            ]
            if row.get("address_type") == "random" or str(row.get("protocol")).upper() == "BTLE":
                hyps.append({"hypothesis_id": "h3", "statement": "A known device rotated to a new random BLE address; the new identifier may not be a new device.",
                             "kind": "configuration_change", "status": "open", "supporting_refs": [eref], "weakening_refs": []})
            findings.append({
                "finding_id": f"F-new-{len(findings) + 1}", "category": "new_entity",
                "title": f"New {row.get('protocol') or 'RF'} entity: {row.get('identity') or key}",
                "observed_facts": facts,
                "computed_results": [fact("Not present in the reference snapshot (baseline comparison)", cid, comparison, f"new_entities.{i}.key")],
                "hypotheses": hyps,
                "assessment": "New relative to the reference snapshot. Origin is not established from one window of evidence.",
                "confidence": "medium",
            })

        for item in comparison["relationship_changes"]:
            key = item["key"]
            before, after = entity(item["reference_ref"]), entity(item["current_ref"])
            i = index_of(comparison["relationship_changes"], key)
            findings.append({
                "finding_id": f"F-rel-{len(findings) + 1}", "category": "relationship_change",
                "title": f"WiFi station {after.get('identity') or key} changed access point",
                "observed_facts": [fact("Associated BSSID in the reference snapshot", item["reference_ref"], before, "bssid"),
                                   fact("Associated BSSID in the current snapshot", item["current_ref"], after, "bssid"),
                                   fact("Network name of the current association", item["current_ref"], after, "ssid")],
                "computed_results": [fact("Baseline comparison: previous BSSID", cid, comparison, f"relationship_changes.{i}.from_bssid"),
                                     fact("Baseline comparison: new BSSID", cid, comparison, f"relationship_changes.{i}.to_bssid")],
                "hypotheses": [
                    {"hypothesis_id": "h1", "statement": "Normal roaming or re-association to a different access point.",
                     "kind": "benign", "status": "open", "supporting_refs": [item["current_ref"]], "weakening_refs": []},
                    {"hypothesis_id": "h2", "statement": "Network reconfiguration moved the station.",
                     "kind": "configuration_change", "status": "open", "supporting_refs": [], "weakening_refs": []},
                    {"hypothesis_id": "h3", "statement": "Passive frame-address inference misattributed the association.",
                     "kind": "sensor_or_collection", "status": "open", "supporting_refs": [], "weakening_refs": []},
                ],
                "assessment": "The inferred association changed between snapshots. Passive inference shows which BSSID the station exchanged frames with, not why.",
                "confidence": "medium",
            })

        for item in comparison["signal_changes"]:
            key, eref = item["key"], item["ref"]
            row = entity(eref)
            i = index_of(comparison["signal_changes"], key)
            hist = history(key)
            computed = [fact("Baseline comparison: current RSSI (dBFS)", cid, comparison, f"signal_changes.{i}.current_rssi_dbfs"),
                        fact("Proximity-review threshold (dBFS)", cid, comparison, "thresholds.strong_signal_dbfs"),
                        fact("Linked observations in the current snapshot", cid, comparison, f"signal_changes.{i}.observation_count")]
            if item["observation_rssi_span_db"] is not None:
                computed.append(fact("RSSI span across linked observations (dB)", cid, comparison, f"signal_changes.{i}.observation_rssi_span_db"))
            obs_facts = []
            if hist:
                for obs in hist["data"]["observations"][:4]:
                    if obs["rssi_dbfs"] is not None:
                        obs_facts.append(fact("Observed RSSI (dBFS) in a linked detection", obs["ref"],
                                              entity(obs["ref"]), "rssi_dbfs"))
            few = item["observation_count"] <= 2
            findings.append({
                "finding_id": f"F-sig-{len(findings) + 1}", "category": "signal_change",
                "title": f"{'Strong' if item['strong_signal'] else 'Changed'} signal from {row.get('identity') or key}",
                "observed_facts": [fact("Latest RSSI (dBFS) on the entity", eref, row, "last_rssi_dbfs"), *obs_facts],
                "computed_results": computed,
                "hypotheses": [
                    {"hypothesis_id": "h1", "statement": "The emitter is physically closer to the receiver than before.",
                     "kind": "environmental", "status": "open", "supporting_refs": [eref], "weakening_refs": []},
                    {"hypothesis_id": "h2", "statement": "Transmit power or antenna orientation changed.",
                     "kind": "configuration_change", "status": "open", "supporting_refs": [], "weakening_refs": []},
                    {"hypothesis_id": "h3", "statement": "A single strong reading is a measurement artifact.",
                     "kind": "sensor_or_collection", "status": "supported" if few else "weakened",
                     "supporting_refs": [cid] if few else [], "weakening_refs": [] if few else [cid]},
                ],
                "assessment": (f"Above the {STRONG_SIGNAL_DBFS:g} dBFS proximity-review threshold. "
                               if item["strong_signal"] else "Signal changed beyond the review threshold. ")
                              + ("Based on very few observations; corroborate before acting." if few else ""),
                "confidence": "low" if few else "medium",
            })

        health_call = gateway.call("get_sensor_health", {"reference_snapshot_id": ref_id, "current_snapshot_id": cur_id})
        health, hid = (health_call["data"], health_call["result_id"]) if health_call["ok"] else ({"sensors": {}}, None)

        for gap in comparison["silent_sensors"]:
            device = gap["device_id"]
            sensor = health["sensors"].get(device, {})
            sref = gap["ref"]
            computed = [fact("Sensor status (sensor health)", hid, health, f"sensors.{device}.status"),
                        fact("New detections from this sensor's protocols", hid, health, f"sensors.{device}.new_detections"),
                        fact("New detections expected from its reference share", hid, health, f"sensors.{device}.expected_new_detections"),
                        fact("Entities covered by this sensor", hid, health, f"sensors.{device}.entity_count"),
                        fact("Other sensors still producing", hid, health, "producing_sensor_count")] if hid else []
            findings.append({
                "finding_id": f"F-sensor-{len(findings) + 1}", "category": "sensor_gap",
                "title": f"Sensor {device} produced no new observations",
                "observed_facts": [fact("Sensor assignment present in the current snapshot", sref, store.resolve(sref), "device_id"),
                                   fact("Events accepted by the backend at the current snapshot", cur_id, store.resolve(cur_id), "events_accepted")],
                "computed_results": computed,
                "hypotheses": [
                    {"hypothesis_id": "h1", "statement": "The receiver, its assignment, or its ingestion path stopped producing observations.",
                     "kind": "sensor_or_collection", "status": "supported" if hid else "open",
                     "supporting_refs": [hid] if hid else [], "weakening_refs": []},
                    {"hypothesis_id": "h2", "statement": f"Every device covered by {device} stopped transmitting at the same time.",
                     "kind": "needs_review", "status": "weakened" if hid else "open",
                     "supporting_refs": [], "weakening_refs": [hid] if hid else []},
                    {"hypothesis_id": "h3", "statement": "Band-wide interference or an RF environment change affected this sensor's protocols.",
                     "kind": "environmental", "status": "open", "supporting_refs": [], "weakening_refs": []},
                ],
                "assessment": (f"{sensor.get('entity_count', 'Its')} entities on this sensor were not re-observed while other sensors kept producing. "
                               "Treat these devices as unobserved, not departed, until the sensor is verified."),
                "confidence": "high" if hid else "medium",
            })

        for i, item in enumerate(comparison["absent_despite_expectation"]):
            if item["sensor_status"] != "producing":
                continue  # explained by a sensor_gap finding, or not attributable to a sensor
            key, device = item["key"], item["sensor"]
            before = store.resolve(item["ref"]) or {}
            current_ref = f"entity:{key}@{cur_id}"
            current = store.resolve(current_ref)
            observed = [fact(f"{before.get('identity') or key} detections in the reference snapshot", item["ref"], before, "detections")]
            if current is not None:
                observed.append(fact("Detections in the current snapshot (unchanged)", current_ref, current, "detections"))
            computed = [fact("New detections expected from its reference rate", cid, comparison,
                             f"absent_despite_expectation.{i}.expected_new_detections")]
            if hid:
                computed += [fact("Status of the sensor covering it", hid, health, f"sensors.{device}.status"),
                             fact("New detections that sensor produced from other devices", hid, health, f"sensors.{device}.new_detections")]
            wifi = str(before.get("protocol")).upper() == "WIFI"
            findings.append({
                "finding_id": f"F-missing-{len(findings) + 1}", "category": "missing_entity",
                "title": f"Expected {before.get('protocol') or 'RF'} entity not re-observed: {before.get('identity') or key}",
                "observed_facts": observed,
                "computed_results": computed,
                "hypotheses": [
                    {"hypothesis_id": "h1", "statement": "The device powered off, left the area, or stopped transmitting.",
                     "kind": "benign", "status": "open", "supporting_refs": [], "weakening_refs": []},
                    {"hypothesis_id": "h2", "statement": ("The access point changed channel or BSSID, or the station associated elsewhere."
                                                          if wifi else "The device changed channel, mode, or identifier."),
                     "kind": "configuration_change", "status": "open", "supporting_refs": [], "weakening_refs": []},
                    {"hypothesis_id": "h3", "statement": "The receiver failed to hear it.",
                     "kind": "sensor_or_collection", "status": "weakened" if hid else "open",
                     "supporting_refs": [], "weakening_refs": [hid] if hid else []},
                    {"hypothesis_id": "h4", "statement": "Path loss or interference specific to this device's location.",
                     "kind": "environmental", "status": "open", "supporting_refs": [], "weakening_refs": []},
                ],
                "assessment": "Absent at a rate its history predicts, while its sensor kept producing. Absence is still not proof of departure.",
                "confidence": "medium",
            })

        n_missing = comparison["not_reobserved_count"]
        if n_missing:
            explained = comparison["absent_despite_expectation_count"] or comparison["silent_sensor_count"]
            findings.append({
                "finding_id": f"F-gap-{len(findings) + 1}", "category": "not_reobserved",
                "title": f"{n_missing} reference entities were not re-observed in the current window",
                "observed_facts": [fact("Events accepted by the backend at the current snapshot", cur_id, store.resolve(cur_id), "events_accepted"),
                                   fact("Events accepted by the backend at the reference snapshot", ref_id, store.resolve(ref_id), "events_accepted")],
                "computed_results": [fact("Entities with no new detections since the reference", cid, comparison, "not_reobserved_count"),
                                     fact("Of these, absent despite an expected detection rate", cid, comparison, "absent_despite_expectation_count"),
                                     fact("Sensors silent despite expected traffic", cid, comparison, "silent_sensor_count")],
                "hypotheses": [
                    {"hypothesis_id": "h1", "statement": "The current window is too short to expect every recurring entity to transmit again.",
                     "kind": "sensor_or_collection", "status": "open" if explained else "supported",
                     "supporting_refs": [] if explained else [cid], "weakening_refs": []},
                    {"hypothesis_id": "h2", "statement": "Some entities stopped transmitting or left the area.",
                     "kind": "needs_review", "status": "open", "supporting_refs": [], "weakening_refs": []},
                ],
                "assessment": ("Entities expected at a meaningful rate, and silent sensors, are reported as separate findings. "
                               "Absence is missing evidence, not proof of departure." if explained else
                               "No absent entity was expected at a meaningful rate in this window. Absence across one short window is not evidence of departure."),
                "confidence": "high",
            })

        cur = cur_env["data"]["collection"]
        cur_rid = cur_env["result_id"]
        findings.append({
            "finding_id": f"F-col-{len(findings) + 1}", "category": "collection_quality",
            "title": "Collection health at the current snapshot",
            "observed_facts": [fact("Events rejected by normalization", cur_id, store.resolve(cur_id), "events_rejected"),
                               fact("Events dropped", cur_id, store.resolve(cur_id), "events_dropped"),
                               fact("Detections returned by the status API", cur_id, store.resolve(cur_id), "detections_returned")],
            "computed_results": [fact("Entities in the current snapshot (environment summary)", cur_rid, cur_env["data"], "entity_count")],
            "hypotheses": [],
            "assessment": ("Detections may be truncated at the API cap; older events are not citable."
                           if cur["detections_may_be_truncated"] else
                           "All returned detections are within the API cap. Rejections and drops are as reported by the backend's own accounting."),
            "confidence": "high",
        })

        recommendations = [{
            "recommendation_id": "R1", "action": "Capture another snapshot after a window that covers each recurring entity's normal cadence, then rerun compare_baselines.",
            "rationale": "Separates true departures from short-window non-observation.", "tool": "compare_baselines",
            "permission": "read_only", "executed": False}]
        for gap in comparison["silent_sensors"]:
            recommendations.append({
                "recommendation_id": f"R{len(recommendations) + 1}",
                "action": f"Check {gap['device_id']} process, assignment, and ingestion logs, then capture a new snapshot.",
                "rationale": "Confirms or rules out a collection fault before any device absence is assessed.",
                "tool": "get_sensor_health", "permission": "read_only", "executed": False})
        signal_keys = [s["key"] for s in comparison["signal_changes"]]
        if signal_keys:
            recommendations.append({
                "recommendation_id": f"R{len(recommendations) + 1}",
                "action": f"Request operator approval for a bounded passive capture to corroborate the signal on {signal_keys[0]}.",
                "rationale": "Strong-signal findings rest on few observations; more passive samples would confirm or weaken the proximity hypothesis.",
                "tool": "start_passive_capture", "permission": "requires_approval", "executed": False})
        limitations = list(LIMITATIONS)
        if any(s.get("mode") == "demo" for s in cur_env["data"]["sensors"].values()):
            limitations.insert(0, SYNTHETIC_LIMITATION)
        return {"findings": findings, "recommendations": recommendations, "limitations": limitations}
