"""Versioned envelope for everything a station and the hub exchange (schema ``rf-sentinel.command.envelope/1``).

    {
      "schema": "rf-sentinel.command.envelope/1",
      "kind": "observation",                      # see KINDS
      "id": "9c1f...",                            # 32 hex; the same logical event always has the same id
      "station": {"id": "passive-node-7/aarch64", "hostname": "passive-node-7", "arch": "aarch64"},
      "seq": 41,                                  # per-station sender sequence (resets when the sender restarts)
      "source_time": "2026-11-27T21:40:02.512+00:00",   # when it was observed / created, station clock
      "sent_time": "2026-11-27T21:40:02.530+00:00",
      "clock": {"synchronized": true, "source": "systemd-timesyncd"},
      "ttl_s": null,                              # required for mission requests
      "idempotency_key": null,                    # mission messages: the operator-visible command id
      "payload": {...}
    }

Station identity is the hostname and CPU architecture (``<hostname>/<arch>``). It names a station; it does not prove
one. Authentication (per-station TLS identity, topic ACLs) is the transport's job, and the hub must check that the
authenticated identity matches ``station.id``.

Envelopes carry metadata only: IQ samples stay on the station and are referenced by hash. The receive time is
recorded by the hub, not trusted from the sender. ``validate`` is strict: unknown top-level fields, a station id that
does not match its parts, a mission request without a TTL, oversize payloads and IQ-like payload fields are rejected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import os
import platform
import re
import socket
from typing import Any
import uuid

SCHEMA = "rf-sentinel.command.envelope/1"
KINDS = ("observation", "heartbeat", "capabilities", "mission_request", "mission_ack", "mission_status",
         "mission_result")
MISSION_KINDS = {"mission_request", "mission_ack", "mission_status", "mission_result"}
FIELDS = ("schema", "kind", "id", "station", "seq", "source_time", "sent_time", "clock", "ttl_s", "idempotency_key",
          "payload")
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_TTL_S = 24 * 3600
IQ_FIELDS = {"iq", "iq_b64", "iq_i8_b64", "samples", "iq_samples", "raw_iq"}  # IQ never crosses the network
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
ARCH_RE = re.compile(r"^[a-z0-9_]{1,32}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")

_seq = itertools.count(1)


class EnvelopeError(ValueError):
    pass


def station_identity(hostname: str | None = None, arch: str | None = None) -> dict[str, str]:
    """This station's identity: lower-case hostname (short name) and CPU architecture."""
    host = (hostname or socket.gethostname()).strip().lower().split(".")[0]
    machine = (arch or platform.machine() or "unknown").strip().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if not HOSTNAME_RE.match(host):
        raise EnvelopeError(f"hostname {host!r} is not a valid station name")
    if not ARCH_RE.match(machine):
        raise EnvelopeError(f"architecture {machine!r} is not valid")
    return {"id": f"{host}/{machine}", "hostname": host, "arch": machine}


def clock_state() -> dict[str, Any]:
    """Whether the station clock is network-synchronized: True, False, or None when it cannot be told."""
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return {"synchronized": True, "source": "systemd-timesyncd"}
    if os.path.isdir("/run/systemd/timesync"):
        return {"synchronized": False, "source": "systemd-timesyncd"}
    return {"synchronized": None, "source": "unknown"}


def _iso(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return value


def _parse_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError(f"{field} is not an ISO 8601 time") from exc
    if parsed.tzinfo is None:
        raise EnvelopeError(f"{field} must carry a time zone")
    return parsed


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def event_id(station_id: str, kind: str, source_time: str, payload: dict[str, Any],
             idempotency_key: str | None = None) -> str:
    """Deterministic for the same logical event, so a redelivered or re-sent envelope deduplicates at the hub."""
    basis = [station_id, kind, idempotency_key] if idempotency_key else [station_id, kind, source_time, payload]
    return hashlib.sha256(_canonical(basis).encode()).hexdigest()[:32]


def make(kind: str, payload: dict[str, Any], *, source_time: datetime | str | None = None, ttl_s: int | None = None,
         idempotency_key: str | None = None, station: dict[str, str] | None = None,
         clock: dict[str, Any] | None = None, seq: int | None = None) -> dict[str, Any]:
    station = station or station_identity()
    source = _iso(source_time)
    if kind in MISSION_KINDS and not idempotency_key:
        idempotency_key = uuid.uuid4().hex
    env = {"schema": SCHEMA, "kind": kind,
           "id": event_id(station["id"], kind, source, payload, idempotency_key),
           "station": dict(station), "seq": next(_seq) if seq is None else int(seq),
           "source_time": source, "sent_time": _iso(None), "clock": clock or clock_state(),
           "ttl_s": ttl_s, "idempotency_key": idempotency_key, "payload": payload}
    validate(env)
    return env


def observation(event: Any, **kwargs) -> dict[str, Any]:
    """An ``RFEvent`` (or its dict) as an observation envelope; its own timestamp is the source time."""
    data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
    return make("observation", data, source_time=data.get("timestamp"), **kwargs)


def _iq_fields(value: Any, path: str = "payload") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in IQ_FIELDS:
                found.append(f"{path}.{key}")
            found += _iq_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value[:1000]):
            found += _iq_fields(item, f"{path}[{i}]")
    elif isinstance(value, (bytes, bytearray)):
        found.append(path)
    return found


def validate(env: Any) -> dict[str, Any]:
    """Return the envelope if it is well formed; raise EnvelopeError naming the first problem."""
    if not isinstance(env, dict):
        raise EnvelopeError("an envelope is a JSON object")
    unknown = sorted(set(env) - set(FIELDS))
    missing = [f for f in FIELDS if f not in env]
    if unknown:
        raise EnvelopeError(f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise EnvelopeError(f"missing fields: {', '.join(missing)}")
    if env["schema"] != SCHEMA:
        raise EnvelopeError(f"unsupported schema {env['schema']!r} (this build reads {SCHEMA})")
    if env["kind"] not in KINDS:
        raise EnvelopeError(f"unknown kind {env['kind']!r}")
    if not isinstance(env["id"], str) or not ID_RE.match(env["id"]):
        raise EnvelopeError("id must be 32 lower-case hex characters")
    station = env["station"]
    if not isinstance(station, dict) or set(station) != {"id", "hostname", "arch"}:
        raise EnvelopeError("station must have exactly id, hostname and arch")
    if not HOSTNAME_RE.match(str(station["hostname"])) or not ARCH_RE.match(str(station["arch"])):
        raise EnvelopeError("station hostname or arch is not valid")
    if station["id"] != f"{station['hostname']}/{station['arch']}":
        raise EnvelopeError("station id must be <hostname>/<arch>")
    if not isinstance(env["seq"], int) or isinstance(env["seq"], bool) or env["seq"] < 0:
        raise EnvelopeError("seq must be a non-negative integer")
    _parse_time(env["source_time"], "source_time")
    _parse_time(env["sent_time"], "sent_time")
    clock = env["clock"]
    if not isinstance(clock, dict) or set(clock) != {"synchronized", "source"} \
            or clock["synchronized"] not in (True, False, None):
        raise EnvelopeError("clock must be {synchronized: true|false|null, source}")
    ttl = env["ttl_s"]
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or not 0 < ttl <= MAX_TTL_S):
        raise EnvelopeError(f"ttl_s must be 1..{MAX_TTL_S} seconds")
    if env["kind"] == "mission_request" and ttl is None:
        raise EnvelopeError("a mission request must carry ttl_s, so a stale command cannot run later")
    if env["kind"] in MISSION_KINDS and not env["idempotency_key"]:
        raise EnvelopeError("mission messages must carry an idempotency_key")
    if env["idempotency_key"] is not None and not (isinstance(env["idempotency_key"], str)
                                                   and 0 < len(env["idempotency_key"]) <= 128):
        raise EnvelopeError("idempotency_key must be a string of 1..128 characters")
    if not isinstance(env["payload"], dict):
        raise EnvelopeError("payload must be a JSON object")
    iq = _iq_fields(env["payload"])
    if iq:
        raise EnvelopeError(f"IQ samples never travel in an envelope (found {iq[0]}); reference them by hash")
    if len(_canonical(env["payload"]).encode()) > MAX_PAYLOAD_BYTES:
        raise EnvelopeError(f"payload is larger than {MAX_PAYLOAD_BYTES} bytes")
    return env


def expired(env: dict[str, Any], now: datetime | None = None) -> bool:
    """True when a message with a TTL is past it (measured from its source time)."""
    if env.get("ttl_s") is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now > _parse_time(env["source_time"], "source_time") + timedelta(seconds=int(env["ttl_s"]))
