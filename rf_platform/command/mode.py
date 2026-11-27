"""Run the dashboard as a Command station or as the hub (environment settings; off by default).

    RF_SENTINEL_COMMAND=station|hub      (unset or "off": no Command link)
    RF_SENTINEL_COMMAND_PKI=<dir>         ca.crt plus this side's certificate and key:
                                            hub:     hub.crt / hub.key
                                            station: station-<hostname>-<arch>.crt / .key (from `pki issue --station`)
    RF_SENTINEL_MQTT_HOST=<broker>        default 127.0.0.1
    RF_SENTINEL_MQTT_PORT=<port>          default 8883
    RF_SENTINEL_SITE=<site>               default "default"

Station: publishes a heartbeat every 15 s, its capabilities on connect, and each agent detection as an observation
(a bounded summary, never IQ). Mission requests are screened (expiry, replay, addressing) and acknowledged; running
them is not enabled yet (that is the mission broker, FOC-006), and the acknowledgement says so.
Hub: receives every station's envelopes and keeps station health for the dashboard.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from . import envelope as E
from .transport import Hub, StationLink, TLSFiles

HEARTBEAT_S = 15.0
OBSERVATION_FIELDS = ("kind", "agent_profile", "agent_key", "identity", "name", "protocol", "rssi_dbfs",
                      "frequency_hz", "channel", "confidence", "device_type", "seen_at", "simulated")


class CommandMode:
    def __init__(self, role: str, link: Any, site: str, capabilities: Callable[[], dict[str, Any]] | None = None):
        self.role, self.link, self.site = role, link, site
        self.capabilities = capabilities
        self.missions: deque[dict[str, Any]] = deque(maxlen=200)
        self.published = 0
        self._stop = threading.Event()

    @classmethod
    def from_env(cls, capabilities: Callable[[], dict[str, Any]] | None = None,
                 env: dict[str, str] | None = None) -> "CommandMode | None":
        env = dict(os.environ if env is None else env)
        role = (env.get("RF_SENTINEL_COMMAND") or "off").strip().lower()
        if role in ("", "off", "0", "false", "no"):
            return None
        if role not in ("station", "hub"):
            raise ValueError(f"RF_SENTINEL_COMMAND must be station, hub or off (got {role!r})")
        pki = Path(env.get("RF_SENTINEL_COMMAND_PKI") or "")
        if not (pki / "ca.crt").is_file():
            raise ValueError("RF_SENTINEL_COMMAND_PKI must point at a directory holding ca.crt and this side's certificate")
        host = env.get("RF_SENTINEL_MQTT_HOST") or "127.0.0.1"
        port = int(env.get("RF_SENTINEL_MQTT_PORT") or 8883)
        site = env.get("RF_SENTINEL_SITE") or "default"
        if role == "hub":
            link = Hub(host, port, TLSFiles(pki / "ca.crt", pki / "hub.crt", pki / "hub.key"), site=site)
            return cls("hub", link, site)
        ident = E.station_identity()
        name = f"station-{ident['hostname']}-{ident['arch']}"
        mode = cls("station", None, site, capabilities)
        mode.link = StationLink(host, port, TLSFiles(pki / "ca.crt", pki / f"{name}.crt", pki / f"{name}.key"), site,
                                station=ident, on_mission=mode._on_mission)
        return mode

    def start(self) -> None:
        self.link.start(wait_s=5)
        if self.role == "station":
            threading.Thread(target=self._heartbeats, name="command-heartbeat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.link.stop()

    # station ------------------------------------------------------------------------------------
    def _heartbeats(self) -> None:
        sent_caps = False
        while not self._stop.is_set():
            if self.link.connected.is_set():
                if not sent_caps and self.capabilities:
                    try:
                        self.link.publish("capabilities", self.capabilities())
                        sent_caps = True
                    except Exception:
                        pass
                self.link.publish("heartbeat", {"status": "online", "uptime_s": round(time.monotonic(), 1)})
            self._stop.wait(HEARTBEAT_S)

    def observation(self, event: dict[str, Any]) -> None:
        """Publish an agent detection (a bounded summary of it). Queued while the broker is unreachable."""
        if self.role != "station":
            return
        payload = {k: event[k] for k in OBSERVATION_FIELDS if event.get(k) not in (None, "")}
        if not payload:
            return
        try:
            self.link.publish("observation", payload)
            self.published += 1
        except (ValueError, E.EnvelopeError):
            pass

    def _on_mission(self, env: dict[str, Any]) -> str:
        self.missions.append({"received_at": time.time(), "idempotency_key": env["idempotency_key"],
                              "payload": env["payload"], "ttl_s": env["ttl_s"]})
        return "recorded, not run: remote missions are not enabled on this station yet"

    # both ---------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "site": self.site, "connected": self.link.connected.is_set(),
                               "broker": f"{self.link.host}:{self.link.port}"}
        if self.role == "station":
            out.update(station=self.link.station, published=self.published, missions=list(self.missions)[-20:])
        else:
            out.update(stations=self.link.health(), dropped=dict(self.link.dropped), messages=len(self.link.messages))
        return out

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.role != "hub":
            return []
        with self.link.lock:
            return list(self.link.messages)[-max(1, min(limit, 1000)):]
