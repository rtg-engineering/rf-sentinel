"""What the agent can collect, and how plain-language requests map onto it."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable

MAX_DURATION_S = 600
DEFAULT_DURATION_S = 30


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    keywords: tuple[str, ...]
    plugin: str                    # receiver plugin executable
    freqs_hz: tuple[int, ...]       # centers measured for gain, in Hz
    sample_rate_sps: int
    detection_kinds: tuple[str, ...]
    key: Callable[[dict[str, Any]], str | None]
    burst_kinds: tuple[str, ...] = ()
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _ble_key(event):
    return event.get("address")


def _zigbee_key(event):
    return event.get("source_address") or event.get("src") or event.get("mac") or event.get("pan_id")


def _classic_key(event):
    # A full BD_ADDR (from a passive FHS) when available, else the LAP from the access code.
    return event.get("address") or event.get("bdaddr") or (f"LAP {event['lap']}" if event.get("lap") else None)


def _tpms_key(event):
    return event.get("sensor_id") or event.get("id") or event.get("device_id_hex")


PROFILES = {
    # Classic is listed first so "bluetooth classic" picks it; a plain "bluetooth" request matches BLE and
    # adds Classic as a companion when the hardware supports it (see Task.companions).
    "classic": Profile("classic", "Bluetooth Classic (BR/EDR) piconets",
                       ("bluetooth classic", "classic bluetooth", "br/edr", "bredr", "bt classic"),
                       "bluetooth_classic", (2_432_000_000, 2_442_000_000, 2_452_000_000), 20_000_000,
                       ("classic_lap",), _classic_key,
                       notes="Captures 20 of the 79 Classic channels (2432-2452 MHz). Classic hops across all 79, "
                             "so every active piconet appears, at about a quarter of its packet rate. Devices are "
                             "identified by LAP (lower address part); full addresses need a passive FHS.",
                       extra={"center_mhz": 2442, "bandwidth_mhz": 20}),
    "ble": Profile("ble", "Bluetooth Low Energy advertisements", ("bluetooth", "ble", "btle", "beacon", "airtag", "find my"),
                   "ble_scanner", (2_402_000_000, 2_426_000_000, 2_480_000_000), 4_000_000,
                   ("ble_adv",), _ble_key, ("ble_burst",),
                   notes="Hops advertising channels 37, 38, 39."),
    "zigbee": Profile("zigbee", "Zigbee / IEEE 802.15.4 frames", ("zigbee", "802.15.4", "thread", "smart home", "matter"),
                      "zigbee_802154", (2_425_000_000,), 4_000_000,
                      ("zigbee_frame", "802154_frame", "frame"), _zigbee_key, ("burst",),
                      notes="Listens on 802.15.4 channel 15 (2425 MHz) unless another channel is named.", extra={"channel": 15}),
    "tpms": Profile("tpms", "Tire-pressure (TPMS) sensors", ("tpms", "tire", "tyre", "car", "vehicle"),
                    "tpms_stack", (315_000_000, 433_920_000), 2_000_000,
                    ("tpms", "tpms_packet", "packet"), _tpms_key,
                    notes="Hops the known 315 MHz and 433.92 MHz TPMS bands."),
}

ALIASES = {"bluetooth devices": "ble", "wifi": None}


@dataclass
class Task:
    profile: Profile
    duration_s: int
    device_id: str | None = None
    channel: int | None = None
    command: str = ""
    companions: tuple[str, ...] = ()   # profiles to run alongside on another receiver, when supported


class IntentError(ValueError):
    pass


def parse(command: str) -> Task:
    """Map a plain-language request onto a bounded, receive-only task."""
    text = " ".join(str(command).lower().split())
    if not text:
        raise IntentError("Say what to look for, e.g. 'find bluetooth devices'.")
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth)\b", text):
        raise IntentError("RF Sentinel only receives. It will not transmit, jam, spoof, or replay.")
    if re.search(r"\bwi-?fi\b", text):
        raise IntentError("WiFi needs a monitor-mode WiFi adapter, not an SDR. Supported here: "
                          + ", ".join(p.title for p in PROFILES.values()) + ".")
    matches = [p for p in PROFILES.values() if any(k in text for k in p.keywords)]
    if not matches:
        raise IntentError("I can look for: " + "; ".join(f"{p.title} ('{p.keywords[0]}')" for p in PROFILES.values()) + ".")
    profile = matches[0]
    companions: tuple[str, ...] = ()
    if profile.name == "classic" and re.search(r"\b(ble|low energy|btle)\b", text):
        companions = ("ble",)  # "bluetooth classic and ble"
    elif profile.name == "ble" and not re.search(r"\b(ble|btle|low energy|beacons?|airtags?|find my)\b", text):
        companions = ("classic",)  # a plain "bluetooth" request covers both kinds
    duration = DEFAULT_DURATION_S
    m = re.search(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b", text)
    if m:
        value, unit = float(m.group(1)), m.group(2)
        duration = int(value * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1))
    if not 5 <= duration <= MAX_DURATION_S:
        raise IntentError(f"Duration must be between 5 and {MAX_DURATION_S} seconds.")
    device = re.search(r"\b((?:bladerf|hackrf|rtlsdr|airspy|sdrplay|lime|uhd|plutosdr):\d+)\b", text)
    channel = re.search(r"\bchannel\s+(\d{1,2})\b", text)
    return Task(profile, duration, device.group(1) if device else None,
                int(channel.group(1)) if channel else None, command, companions)
