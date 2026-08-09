#!/usr/bin/env python3
"""Generate deterministic public-safe RF Sentinel demo events."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path(".demo/events/public-demo-events.jsonl")

WEEKLY_1WK = [0, 7]
WEEKLY_1MO = [0, 7, 14, 21, 28]
WEEKLY_6WK = [0, 7, 14, 21, 28, 35, 42]
WEEKLY_231D = list(range(0, 232, 7))


BASE_EVENTS: list[tuple[float, dict[str, Any]]] = [
    (
        0.0,
        {
            "protocol": "btc",
            "kind": "classic_lap",
            "type": "lap_seen",
            "name": "Demo Headset Link",
            "nap": "8C17",
            "uap": "5A",
            "lap": "A1C3E5",
            "channel": 18,
            "center_freq_hz": 2_420_000_000,
            "rssi_dbfs": -43.8,
            "candidate_count": 1,
            "tracking_us": 356_000,
            "processed_packets": 24,
            "detail": "active Bluetooth Classic piconet traffic observed",
            "demo_seen_day_offsets": WEEKLY_1WK,
        },
    ),
    (
        0.35,
        {
            "protocol": "ble",
            "kind": "ble_adv",
            "address": "D0:EF:42:10:AA:01",
            "address_type": "random",
            "name": "Demo Beacon-01",
            "uuid16": ["0x180A", "0xFEAA"],
            "manufacturer": {"company_id": 4095, "company_name": "Demo Devices"},
            "appearance": {"category": "Generic Sensor"},
            "channel": 37,
            "center_freq_hz": 2_402_000_000,
            "rssi_dbfs": -51.2,
            "demo_seen_day_offsets": WEEKLY_1MO,
        },
    ),
    (
        0.75,
        {
            "protocol": "wifi",
            "kind": "beacon",
            "source": "02:10:7A:00:10:01",
            "destination": "ff:ff:ff:ff:ff:ff",
            "bssid": "02:10:7A:00:10:01",
            "ssid": "FieldLab-AP-01",
            "channel": 1,
            "frequency_mhz": 2412,
            "rssi_dbm": -38,
            "raw": "802.11 beacon",
            "demo_seen_day_offsets": WEEKLY_1MO,
        },
    ),
    (
        1.15,
        {
            "protocol": "ieee802154",
            "kind": "zigbee_frame",
            "channel": 15,
            "center_freq_hz": 2_425_000_000,
            "rssi_dbfs": -58.4,
            "confidence": 0.94,
            "payload_hex": "4188b1efbe02104a7c9d00112233445566",
            "mac": {
                "frame_type": "Data frame",
                "source_address": "0x7C4A",
                "destination_address": "0x0000",
                "source_pan_id": "0xBEEF",
                "sequence_number": 177,
                "fcs_hex": "a72c",
            },
            "fcs_ok": True,
            "demo_seen_day_offsets": WEEKLY_1WK,
        },
    ),
    (
        1.65,
        {
            "protocol": "fm",
            "kind": "fm_station",
            "identity": "Demo FM 101.7",
            "frequency_mhz": 101.7,
            "frequency_hz": 101_700_000,
            "power_dbfs": -36.6,
            "noise_dbfs": -79.0,
            "excess_db": 42.4,
            "pilot_db": 21.3,
            "rds_subcarrier_db": 9.8,
            "stereo_likely": True,
            "rds_likely": True,
            "demo_seen_day_offsets": WEEKLY_6WK,
        },
    ),
    (
        2.15,
        {
            "protocol": "ble",
            "kind": "ble_adv",
            "address": "E1:04:22:6A:B0:14",
            "address_type": "random",
            "name": "Demo Health Monitor",
            "uuid16": ["0x180D", "0x180F"],
            "manufacturer": {"company_id": 4094, "company_name": "Demo Medical"},
            "appearance": {"category": "Heart Rate Sensor"},
            "channel": 38,
            "center_freq_hz": 2_426_000_000,
            "rssi_dbfs": -46.0,
        },
    ),
    (
        2.75,
        {
            "protocol": "wifi",
            "kind": "probe_response",
            "source": "02:10:7A:00:20:06",
            "destination": "8A:D3:5B:21:44:10",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -47,
            "raw": "802.11 probe response",
            "demo_seen_day_offsets": WEEKLY_231D,
        },
    ),
    (
        3.1,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "8A:D3:5B:21:44:10",
            "destination": "02:10:7A:00:20:06",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "identity": "Gateway Tablet",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -54,
            "raw": "802.11 data frame",
            "demo_seen_day_offsets": WEEKLY_1MO,
        },
    ),
    (
        3.25,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "8A:D3:5B:21:44:11",
            "destination": "02:10:7A:00:20:06",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "identity": "Field Laptop",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -58,
            "raw": "802.11 QoS data",
        },
    ),
    (
        3.4,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "8A:D3:5B:21:44:12",
            "destination": "02:10:7A:00:20:06",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "identity": "Sensor Node",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -61,
            "raw": "802.11 data frame",
        },
    ),
    (
        3.55,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "8A:D3:5B:21:44:13",
            "destination": "02:10:7A:00:20:06",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "identity": "Handheld Terminal",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -64,
            "raw": "802.11 data frame",
        },
    ),
    (
        3.7,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "8A:D3:5B:21:44:14",
            "destination": "02:10:7A:00:20:06",
            "bssid": "02:10:7A:00:20:06",
            "ssid": "DemoMesh-2G",
            "identity": "Maintenance Phone",
            "channel": 6,
            "frequency_mhz": 2437,
            "rssi_dbm": -67,
            "raw": "802.11 QoS data",
        },
    ),
    (
        3.65,
        {
            "protocol": "tpms",
            "kind": "tpms_frame",
            "protocol_variant": "Demo TPMS",
            "decoded_fields": {"sensor_id": "DTPMS-02"},
            "center_freq_hz": 433_920_000,
            "rssi_dbfs": -62.5,
            "confidence": 0.82,
            "hex": "a55a1202ef9033",
            "demo_seen_day_offsets": WEEKLY_1WK,
        },
    ),
    (
        4.1,
        {
            "protocol": "walkie",
            "kind": "walkie_signal",
            "identity": "Demo FRS Voice Channel",
            "frequency_mhz": 462.5625,
            "frequency_hz": 462_562_500,
            "modulation": "NBFM",
            "classification": "voice_like_activity",
            "rssi_dbfs": -49.1,
            "audio_rms_dbfs": -23.4,
            "audio_bandwidth_hz": 2850,
            "voice_band_ratio": 0.74,
            "voice_activity_ratio": 0.68,
            "occupied_ratio": 0.43,
            "confidence": 0.79,
        },
    ),
    (
        4.75,
        {
            "protocol": "btc",
            "kind": "classic_lap",
            "type": "lap_narrowed",
            "nap": "XXXX",
            "uap": "XX",
            "lap": "C4D9B2",
            "channel": 52,
            "center_freq_hz": 2_454_000_000,
            "rssi_dbfs": -57.2,
            "candidate_count": 3,
            "tracking_us": 184_000,
            "processed_packets": 11,
            "detail": "Bluetooth Classic active piconet candidate",
        },
    ),
    (
        5.35,
        {
            "protocol": "ieee802154",
            "kind": "zigbee_frame",
            "channel": 20,
            "center_freq_hz": 2_450_000_000,
            "rssi_dbfs": -63.1,
            "confidence": 0.88,
            "payload_hex": "41884211aa55302133445566778899",
            "mac": {
                "frame_type": "Command frame",
                "source_address": "0x3021",
                "destination_address": "0x0000",
                "source_pan_id": "0x11AA",
                "sequence_number": 66,
                "fcs_hex": "18f1",
            },
            "fcs_ok": True,
        },
    ),
    (
        6.05,
        {
            "protocol": "wifi",
            "kind": "beacon",
            "source": "02:10:7A:00:30:11",
            "destination": "ff:ff:ff:ff:ff:ff",
            "bssid": "02:10:7A:00:30:11",
            "ssid": "Telemetry-GW",
            "channel": 11,
            "frequency_mhz": 2462,
            "rssi_dbm": -44,
            "raw": "802.11 beacon",
        },
    ),
    (
        6.45,
        {
            "protocol": "wifi",
            "kind": "data",
            "source": "72:94:11:3B:0C:90",
            "destination": "02:10:7A:00:30:11",
            "bssid": "02:10:7A:00:30:11",
            "ssid": "Telemetry-GW",
            "channel": 11,
            "frequency_mhz": 2462,
            "rssi_dbm": -61,
            "raw": "802.11 QoS data",
        },
    ),
    (
        7.05,
        {
            "protocol": "ble",
            "kind": "ble_adv",
            "address": "F2:A8:6C:03:09:51",
            "address_type": "random",
            "name": "Demo Asset Tag",
            "uuid16": ["0x180A", "0xFD6F"],
            "manufacturer": {"company_id": 4093, "company_name": "Demo Tags"},
            "appearance": {"category": "Tag"},
            "channel": 39,
            "center_freq_hz": 2_480_000_000,
            "rssi_dbfs": -59.0,
        },
    ),
    (
        7.65,
        {
            "protocol": "lfmf",
            "kind": "lfmf_signal",
            "band_label": "MF beacon",
            "frequency_khz": 530.0,
            "frequency_hz": 530_000,
            "carrier_dbfs": -42.8,
            "carrier_snr_db": 31.4,
            "excess_db": 24.1,
            "modulation_pct": 11.2,
            "active": True,
        },
    ),
    (
        8.35,
        {
            "protocol": "cellular",
            "kind": "cellular_signal",
            "frequency_mhz": 751.0,
            "frequency_hz": 751_000_000,
            "center_freq_hz": 751_000_000,
            "band": "3GPP Band 13 / 700 MHz Upper C",
            "link": "downlink",
            "cellular_type": "LTE",
            "technology": "LTE",
            "likely_operator": "Demo Carrier",
            "operator_confidence": "notional",
            "power_dbfs": -55.7,
            "noise_floor_dbfs": -88.6,
            "excess_db": 32.9,
            "occupied_width_hz": 9_800_000,
            "classification": "Passive cellular spectrum activity",
            "passive_only": True,
            "content_decoded": False,
        },
    ),
]


def _with_rssi(payload: dict[str, Any], delta: float) -> dict[str, Any]:
    item = deepcopy(payload)
    for key in ("rssi_dbfs", "last_rssi_dbfs", "rssi_dbm", "power_dbfs", "carrier_dbfs"):
        if key in item and isinstance(item[key], (int, float)):
            item[key] = round(float(item[key]) + delta, 1)
    return item


def build_events(cycles: int = 5, cycle_seconds: float = 9.5) -> list[dict[str, Any]]:
    rng = random.Random(20260809)
    events: list[dict[str, Any]] = []
    for cycle in range(cycles):
        cycle_offset = cycle * cycle_seconds
        for offset, payload in BASE_EVENTS:
            item = _with_rssi(payload, rng.uniform(-3.2, 2.6))
            item["offset_s"] = round(cycle_offset + offset + rng.uniform(-0.08, 0.08), 2)
            item["demo_sequence"] = len(events) + 1
            if item.get("protocol") == "btc":
                item["channel"] = int((int(item["channel"]) + (cycle * 13)) % 79)
                item["center_freq_hz"] = 2_402_000_000 + int(item["channel"]) * 1_000_000
            if item.get("protocol") == "ble":
                channels = [37, 38, 39]
                channel = channels[(channels.index(int(item["channel"])) + cycle) % len(channels)]
                item["channel"] = channel
                item["center_freq_hz"] = {37: 2_402_000_000, 38: 2_426_000_000, 39: 2_480_000_000}[channel]
            if item.get("protocol") == "wifi" and item.get("ssid") == "DemoMesh-2G":
                item["rssi_dbm"] = round(float(item["rssi_dbm"]) - (cycle % 2), 1)
            events.append(item)
    return sorted(events, key=lambda item: float(item["offset_s"]))


def write_events(output: Path, cycles: int = 5, cycle_seconds: float = 9.5) -> dict[str, Any]:
    events = build_events(cycles=cycles, cycle_seconds=cycle_seconds)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    counts = Counter(str(event.get("protocol") or "unknown").lower() for event in events)
    manifest = {
        "demo_id": "public-demo-rf-sentinel",
        "event_file": str(output),
        "event_count": len(events),
        "duration_s": max(float(event["offset_s"]) for event in events) if events else 0.0,
        "counts_by_protocol": dict(sorted(counts.items())),
        "public_safe": True,
        "description": "Deterministic synthetic RF Sentinel event replay for public demo use.",
    }
    manifest_path = output.with_name("manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--cycle-seconds", type=float, default=9.5)
    args = parser.parse_args()
    manifest = write_events(args.output, cycles=max(1, args.cycles), cycle_seconds=max(1.0, args.cycle_seconds))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
