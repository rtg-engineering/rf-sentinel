from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SUPPORTED_PROTOCOLS = {
    "ble",
    "bluetooth_classic",
    "wifi",
    "zigbee",
    "802.15.4",
    "tpms",
    "drone_uas",
    "unknown_rf",
}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class RFEvent:
    """Normalized passive RF observation emitted by a collector or decoder."""

    protocol: str
    subtype: str
    timestamp: str = field(default_factory=utc_now_iso)
    frequency_hz: int | None = None
    channel: str | int | None = None
    rssi_dbm: float | None = None
    confidence: float = 1.0
    device_id: str | None = None
    partial_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_sensor: str = "local"

    def __post_init__(self) -> None:
        normalized_protocol = self.protocol.strip().lower()
        object.__setattr__(self, "protocol", normalized_protocol)
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "protocol": self.protocol,
            "subtype": self.subtype,
            "frequency_hz": self.frequency_hz,
            "channel": self.channel,
            "rssi_dbm": self.rssi_dbm,
            "confidence": self.confidence,
            "device_id": self.device_id,
            "partial_id": self.partial_id,
            "metadata": dict(self.metadata),
            "source_sensor": self.source_sensor,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RFEvent":
        return cls(
            timestamp=str(payload.get("timestamp") or utc_now_iso()),
            protocol=str(payload.get("protocol") or "unknown_rf"),
            subtype=str(payload.get("subtype") or "observation"),
            frequency_hz=_optional_int(payload.get("frequency_hz")),
            channel=payload.get("channel"),
            rssi_dbm=_optional_float(payload.get("rssi_dbm")),
            confidence=float(payload.get("confidence", 1.0)),
            device_id=_optional_str(payload.get("device_id")),
            partial_id=_optional_str(payload.get("partial_id")),
            metadata=dict(payload.get("metadata") or {}),
            source_sensor=str(payload.get("source_sensor") or "local"),
        )


@dataclass(frozen=True)
class EntityKey:
    """Stable key used by entity resolution before richer merging logic exists."""

    protocol: str
    key: str
    confidence: float = 1.0

    @classmethod
    def from_event(cls, event: RFEvent) -> "EntityKey | None":
        identifier = event.device_id or event.partial_id
        if not identifier:
            return None
        return cls(protocol=event.protocol, key=str(identifier), confidence=event.confidence)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
