"""MQTT topic layout for Command: ``rfs/v1/<site>/stations/<hostname>/<arch>/<kind>``.

A station publishes only under its own ``<hostname>/<arch>`` (the broker's ACL enforces it from the station's
certificate), and only the hub may publish ``mission_request`` to a station. The hub checks that an envelope's
``station.id`` and ``kind`` match the topic it arrived on.
"""
from __future__ import annotations

import re

PREFIX = "rfs/v1"
FROM_STATION = ("observation", "heartbeat", "capabilities", "mission_ack", "mission_status", "mission_result")
TO_STATION = ("mission_request",)
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HUB_IDENTITY = "rfs-hub"  # the hub's certificate name; station identities always contain "/", so they cannot collide


def check_site(site: str) -> str:
    if not SITE_RE.match(site or ""):
        raise ValueError("site names are 1-32 lower-case letters, digits, '-' or '_'")
    return site


def station_topic(site: str, station_id: str, kind: str) -> str:
    if kind not in FROM_STATION + TO_STATION:
        raise ValueError(f"unknown topic kind {kind!r}")
    hostname, arch = station_id.split("/", 1)
    return f"{PREFIX}/{check_site(site)}/stations/{hostname}/{arch}/{kind}"


def parse(topic: str) -> tuple[str, str, str] | None:
    """(site, station_id, kind) for a station topic, else None."""
    parts = topic.split("/")
    if len(parts) != 7 or "/".join(parts[:2]) != PREFIX or parts[3] != "stations":
        return None
    site, hostname, arch, kind = parts[2], parts[4], parts[5], parts[6]
    if not SITE_RE.match(site) or kind not in FROM_STATION + TO_STATION:
        return None
    return site, f"{hostname}/{arch}", kind


def subscriptions_for_hub(site: str = "+") -> list[str]:
    return [f"{PREFIX}/{site}/stations/+/+/{kind}" for kind in FROM_STATION]
