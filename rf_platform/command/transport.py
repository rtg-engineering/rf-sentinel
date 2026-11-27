"""MQTT links between stations and the hub (mutual TLS, QoS 1, envelopes only).

StationLink
- publishes observation / heartbeat / capabilities / mission_* envelopes under its own topics. The station identity
  must equal the common name of its certificate, so a misconfigured station fails at start, not at the broker;
- while disconnected, publishes wait in a bounded queue and go out on reconnect. A redelivered envelope keeps its id,
  so the hub keeps one copy;
- receives mission requests and screens each one before anything else sees it: valid envelope, addressed to this
  station, from the hub topic, not expired (TTL from its source time), not seen before (idempotency key). Every
  request is answered with a mission_ack saying accepted or why it was rejected. Running an accepted mission is the
  caller's job (the station's own policy gateway still decides).

Hub
- subscribes to every station topic and keeps, per message: the validated envelope, the topic it came on and the
  hub's own receive time. Envelopes whose station or kind do not match their topic, malformed envelopes and
  duplicates (same id) are counted and dropped, never stored;
- tracks station health from heartbeats: online, stale (no heartbeat within the stale window) or offline (the broker's
  last will). A station that stops reporting is shown as not reporting, which is not evidence that nothing is there.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import threading
import time
from typing import Any, Callable

from . import envelope as E
from .topics import FROM_STATION, HUB_IDENTITY, parse, station_topic, subscriptions_for_hub

QOS = 1
MAX_QUEUED = 5000


@dataclass
class TLSFiles:
    ca: Path
    cert: Path
    key: Path


def _client(client_id: str, tls: TLSFiles, will: tuple[str, bytes] | None = None):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False,
                         protocol=mqtt.MQTTv311)
    client.tls_set(ca_certs=str(tls.ca), certfile=str(tls.cert), keyfile=str(tls.key),
                   cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.max_queued_messages_set(MAX_QUEUED)
    client.reconnect_delay_set(1, 30)
    if will:
        client.will_set(will[0], will[1], qos=QOS, retain=False)
    return client


class _Link:
    def __init__(self, host: str, port: int, client_id: str, tls: TLSFiles, will: tuple[str, bytes] | None = None):
        self.host, self.port = host, port
        self.connected = threading.Event()
        self.client = _client(client_id, tls, will)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = lambda *a, **k: self.connected.clear()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            self.connected.set()
            self.on_connected()

    def on_connected(self) -> None:
        pass

    def start(self, wait_s: float = 10.0) -> bool:
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        return self.connected.wait(wait_s)

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


class StationLink(_Link):
    def __init__(self, host: str, port: int, tls: TLSFiles, site: str, station: dict[str, str] | None = None,
                 on_mission: Callable[[dict[str, Any]], str | None] | None = None, seen_limit: int = 10000):
        from .pki import common_name
        self.station = station or E.station_identity()
        cn = common_name(tls.cert)
        if cn != self.station["id"]:
            raise ValueError(f"certificate is for {cn!r} but this station is {self.station['id']!r}")
        self.site = site
        self.on_mission = on_mission
        self.seen: OrderedDict[str, str] = OrderedDict()  # idempotency key -> outcome
        self.seen_limit = seen_limit
        offline = E.make("heartbeat", {"status": "offline", "reason": "connection lost"}, station=self.station)
        super().__init__(host, port, f"station:{self.station['id']}", tls,
                         will=(station_topic(site, self.station["id"], "heartbeat"), json.dumps(offline).encode()))
        self.client.on_message = self._on_message

    def on_connected(self) -> None:
        self.client.subscribe(station_topic(self.site, self.station["id"], "mission_request"), qos=QOS)

    def publish(self, kind: str, payload: dict[str, Any], **kwargs) -> dict[str, Any]:
        env = E.make(kind, payload, station=self.station, **kwargs)
        return self.publish_envelope(env)

    def publish_envelope(self, env: dict[str, Any]) -> dict[str, Any]:
        E.validate(env)
        if env["station"]["id"] != self.station["id"] or env["kind"] not in FROM_STATION:
            raise ValueError("a station publishes only its own station-to-hub envelopes")
        self.client.publish(station_topic(self.site, self.station["id"], env["kind"]), json.dumps(env), qos=QOS)
        return env

    def screen(self, topic: str, raw: bytes, now: datetime | None = None) -> tuple[dict[str, Any] | None, str]:
        """(envelope, "accepted") or (envelope or None, reason it is rejected)."""
        where = parse(topic)
        if where is None or where[1] != self.station["id"] or where[2] != "mission_request":
            return None, "rejected: not a mission request for this station"
        try:
            env = E.validate(json.loads(raw))
        except (ValueError, E.EnvelopeError) as exc:
            return None, f"rejected: invalid envelope ({exc})"
        if env["kind"] != "mission_request":
            return env, "rejected: not a mission request"
        key = env["idempotency_key"]
        if key in self.seen:
            return env, f"rejected: duplicate of {key} (already {self.seen[key]})"
        if E.expired(env, now):
            return env, "rejected: expired before it arrived"
        return env, "accepted"

    def _on_message(self, client, userdata, msg) -> None:
        env, outcome = self.screen(msg.topic, msg.payload)
        if outcome == "accepted" and self.on_mission:
            try:
                note = self.on_mission(env)
                if note:
                    outcome = f"accepted: {note}"
            except Exception as exc:  # the station's own checks refused it
                outcome = f"rejected: {exc}"
        if env is not None and env.get("idempotency_key"):
            key = env["idempotency_key"]
            duplicate = outcome.startswith("rejected: duplicate")
            if not duplicate:
                self.seen[key] = outcome.split(":")[0]
                while len(self.seen) > self.seen_limit:
                    self.seen.popitem(last=False)
            # One ack per (command, outcome): the ack's own key must differ from the request's, or the hub would
            # take the "duplicate" ack for a copy of the "accepted" one.
            category = "duplicate" if duplicate else outcome.split(":")[0]
            self.publish("mission_ack", {"request_id": env["id"], "request_key": key, "outcome": outcome},
                         idempotency_key=f"{key}:{category}"[:128])


@dataclass
class StationHealth:
    station_id: str
    site: str
    last_heartbeat: float | None = None
    last_message: float | None = None
    status: str = "unknown"
    clock: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)


class Hub(_Link):
    def __init__(self, host: str, port: int, tls: TLSFiles, site: str = "+", keep: int = 20000, stale_s: float = 45.0,
                 on_envelope: Callable[[dict[str, Any]], None] | None = None):
        from .pki import common_name
        if common_name(tls.cert) != HUB_IDENTITY:
            raise ValueError(f"the hub needs the {HUB_IDENTITY!r} certificate")
        super().__init__(host, port, HUB_IDENTITY, tls)
        self.site, self.stale_s, self.on_envelope = site, stale_s, on_envelope
        self.lock = threading.Lock()
        self.messages: deque[dict[str, Any]] = deque(maxlen=keep)
        self.ids: OrderedDict[str, None] = OrderedDict()
        self.keep = keep
        self.stations: dict[str, StationHealth] = {}
        self.dropped: dict[str, int] = {"invalid": 0, "topic_mismatch": 0, "duplicate": 0}
        self.client.on_message = self._on_message

    def on_connected(self) -> None:
        for sub in subscriptions_for_hub(self.site):
            self.client.subscribe(sub, qos=QOS)

    def receive(self, topic: str, raw: bytes, received: float | None = None) -> str:
        """Validate and record one message; returns "stored" or the reason it was dropped."""
        received = time.time() if received is None else received
        where = parse(topic)
        try:
            env = E.validate(json.loads(raw))
        except (ValueError, E.EnvelopeError):
            with self.lock:
                self.dropped["invalid"] += 1
            return "invalid"
        if where is None or where[1] != env["station"]["id"] or where[2] != env["kind"]:
            with self.lock:
                self.dropped["topic_mismatch"] += 1
            return "topic_mismatch"
        site, station_id, kind = where
        with self.lock:
            if env["id"] in self.ids:
                self.dropped["duplicate"] += 1
                return "duplicate"
            self.ids[env["id"]] = None
            while len(self.ids) > self.keep * 2:
                self.ids.popitem(last=False)
            record = {"envelope": env, "topic": topic, "site": site,
                      "received_at": datetime.fromtimestamp(received, timezone.utc).isoformat(timespec="milliseconds")}
            self.messages.append(record)
            health = self.stations.setdefault(station_id, StationHealth(station_id, site))
            health.last_message, health.clock = received, env["clock"]
            if kind == "heartbeat":
                health.last_heartbeat = received
                health.status = "offline" if env["payload"].get("status") == "offline" else "online"
            elif kind == "capabilities":
                health.capabilities = env["payload"]
        if self.on_envelope:
            self.on_envelope(record)
        return "stored"

    def _on_message(self, client, userdata, msg) -> None:
        self.receive(msg.topic, msg.payload)

    def health(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        out = []
        with self.lock:
            for h in self.stations.values():
                status = h.status
                if status == "online" and (h.last_heartbeat is None or now - h.last_heartbeat > self.stale_s):
                    status = "stale"
                out.append({"station_id": h.station_id, "site": h.site, "status": status,
                            "last_heartbeat_age_s": None if h.last_heartbeat is None else round(now - h.last_heartbeat, 1),
                            "clock": h.clock, "capabilities": h.capabilities})
        return sorted(out, key=lambda s: s["station_id"])

    def send_mission(self, site: str, station_id: str, payload: dict[str, Any], ttl_s: int,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        env = E.make("mission_request", payload, ttl_s=ttl_s, idempotency_key=idempotency_key)
        self.client.publish(station_topic(site, station_id, "mission_request"), json.dumps(env), qos=QOS)
        return env
