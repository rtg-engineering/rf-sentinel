"""Mosquitto configuration for a Command site: TLS only, client certificates required, per-station topic ACL.

    python -m rf_platform.command broker-config <pki dir> [--port 8883]  ->  <pki dir>/mosquitto.conf, acl

Every client presents a certificate from the site CA. Its common name becomes its MQTT username, and:
- the hub (``rfs-hub``) reads every station topic and is the only client that may write ``mission_request``;
- a station (``<hostname>/<arch>``) writes only its own observation/heartbeat/capabilities/mission_* topics and reads
  only its own ``mission_request``. A station cannot publish as another station or send missions.
There is no plain-text listener and no anonymous access.
"""
from __future__ import annotations

from pathlib import Path

from .topics import FROM_STATION, HUB_IDENTITY, PREFIX


def acl_text() -> str:
    lines = ["# RF Sentinel Command ACL (generated). Usernames are certificate common names.",
             f"user {HUB_IDENTITY}", f"topic read {PREFIX}/+/stations/#",
             f"topic write {PREFIX}/+/stations/+/+/mission_request", "",
             "# Stations: %u is <hostname>/<arch>, so each station is confined to its own topics."]
    lines += [f"pattern write {PREFIX}/+/stations/%u/{kind}" for kind in FROM_STATION]
    lines.append(f"pattern read {PREFIX}/+/stations/%u/mission_request")
    return "\n".join(lines) + "\n"


def config_text(pki_dir: Path, port: int = 8883, bind: str | None = None, persistence_dir: Path | None = None) -> str:
    d = Path(pki_dir).resolve()
    out = ["# RF Sentinel Command broker (generated)", "per_listener_settings false", "allow_anonymous false",
           f"acl_file {d / 'acl'}", f"listener {port}" + (f" {bind}" if bind else ""),
           f"cafile {d / 'ca.crt'}", f"certfile {d / 'broker.crt'}", f"keyfile {d / 'broker.key'}",
           "require_certificate true", "use_identity_as_username true", "tls_version tlsv1.2",
           "max_queued_messages 10000", "message_size_limit 131072"]
    if persistence_dir:
        out += ["persistence true", f"persistence_location {Path(persistence_dir).resolve()}/"]
    return "\n".join(out) + "\n"


def write(pki_dir: Path, port: int = 8883, bind: str | None = None, persistence_dir: Path | None = None) -> Path:
    d = Path(pki_dir)
    (d / "acl").write_text(acl_text())
    conf = d / "mosquitto.conf"
    conf.write_text(config_text(d, port, bind, persistence_dir))
    return conf
