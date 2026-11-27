"""Detector events -> RF Sentinel detections.

Input: the PASSIVE-SHIELD station event schemas (passive_shield.station_detection.v1 and
passive_shield.station_link_occupancy.v1), as JSON objects. Output: one flat detection per event, with `kind`
saying how strong the evidence is, because the two are different claims:
- drone_detection: the detector confirmed a drone link (its own train-morphology confirmation passed);
- drone_link: a wideband link with drone-like occupancy was seen, not confirmed as a drone.
Unknown event types are dropped. Fields are copied, never upgraded: an unconfirmed link stays unconfirmed.
"""
from __future__ import annotations

from typing import Any

KINDS = {
    "passive_shield.station.detection": "drone_detection",
    "passive_shield.station.link_occupancy": "drone_link",
}
COPY = ("time_utc", "station_id", "track_id", "track_state", "frequency_hz", "occupied_bandwidth_hz", "confidence",
        "candidate_family", "protocol_family", "decode_status", "rf_state_label", "operational_state_label",
        "operational_state_confidence", "endpoint_hypothesis", "role_summary", "radio_band", "operator_summary")
BANDS = {"ism_2g4": "2.4 GHz", "ism_5g8": "5.8 GHz"}


def to_detection(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = KINDS.get(str(event.get("event_type") or ""))
    if kind is None:
        return None
    out: dict[str, Any] = {"kind": kind, "protocol": "drone_rf"}
    out.update({k: event[k] for k in COPY if event.get(k) not in (None, "")})
    freq = float(event.get("frequency_hz") or event.get("center_hz") or 0.0)
    band = BANDS.get(str(event.get("radio_band") or ""), f"{freq / 1e9:.3f} GHz" if freq else "unknown band")
    family = event.get("candidate_family") or event.get("protocol_family") or "drone"
    what = "Drone link (confirmed)" if kind == "drone_detection" else "Drone-like link (not confirmed)"
    out["identity"] = f"{what}: {family}, {band}"
    out["device_type"] = "confirmed drone" if kind == "drone_detection" else "drone-like link"
    out["key"] = event.get("track_id") or f"{kind}:{event.get('radio_band') or ''}:{int(freq)}"
    out["level_db"] = _first_number(event, ("score_db", "best_score_db"))
    if out["level_db"] is None:
        out.pop("level_db")
    return out


def _first_number(event: dict[str, Any], names: tuple[str, ...]) -> float | None:
    details = event.get("detector_details") if isinstance(event.get("detector_details"), dict) else {}
    for name in names:
        for source in (event, details):
            value = source.get(name)
            if isinstance(value, (int, float)):
                return float(value)
    return None
