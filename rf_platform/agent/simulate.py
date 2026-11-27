"""Simulated receivers for the agent: scripted RF scenarios, emitted exactly like the real plugins' JSON events.

  RF_SENTINEL_AGENT_SIMULATE=1 rf-sentinel agent run "watch for trackers for 2 minutes ..." --local
  python -m rf_platform.agent.simulate --profile ble --duration 60      # the plugin the agent runs

Used when no radio is attached (development, CI) and by the dashboard's demo mode. Everything is labelled
simulated: the events carry "simulated": true, and runs, reports and the UI say so. Addresses, LAPs and cell
IDs are synthetic. Nothing here touches hardware.

Scenario (times scale with the requested duration D; activity continues after D if a trigger extends the run;
in a playbook D is the whole playbook and later steps continue the same clock):
  BLE        a crowd of phones, watch, headphones, a Tile and a beacon; at 0.15 D a separated AirTag appears
             close by and stays; at 0.45 D an unknown strong device shows up for a while; a phone leaves at 0.6 D.
  Classic    a headset and a car kit piconet.
  Cellular   ten LTE/5G cells from three operators, serving-cell changes at 0.3 D and 0.7 D, and at 0.55 D a 2G
             cell on the test network PLMN 001-01, a classic sign of a fake base station.
  Zigbee     three nodes; TPMS: four tire sensors.
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import threading
import time
from typing import Any

SIM_DEVICES = [
    {"id": "sim:0", "driver": "sim", "label": "Simulated receiver A", "freq_min_hz": 70_000_000,
     "freq_max_hz": 6_000_000_000, "max_sample_rate_sps": 61_440_000, "occupied": False, "notes": "simulated"},
    {"id": "sim:1", "driver": "sim", "label": "Simulated receiver B", "freq_min_hz": 70_000_000,
     "freq_max_hz": 6_000_000_000, "max_sample_rate_sps": 61_440_000, "occupied": False, "notes": "simulated"},
]


class SimRadio:
    """Stands in for agent.radio.Radio: two simulated wideband receivers, nothing opened."""

    base_url = "sim://"
    token = ""
    headers: dict[str, str] = {}

    def __init__(self):
        self._busy: set[str] = set()

    def devices(self) -> list[dict[str, Any]]:
        return [{**d, "occupied": d["id"] in self._busy} for d in SIM_DEVICES]

    def choose(self, freqs_hz, rate_sps, wanted=None, exclude=()):
        pool = [d for d in self.devices() if d["id"] not in exclude and (not wanted or d["id"] == wanted)]
        if not pool:
            raise RuntimeError("Every simulated receiver is busy")
        return pool[0]

    # Simulated power sweep: one scripted pass every 4 s (see rf_platform.agent.discover.simulated_samples).
    def start_sweep(self, device, start_hz, stop_hz, gain_db=30.0) -> str:
        import time as _time
        self._sweep = {"start": _time.time(), "range": (int(start_hz), int(stop_hz))}
        return "sim-sweep"

    def sweep_samples(self, sweep_id) -> list[dict[str, Any]]:
        import time as _time
        from .discover import simulated_samples
        passes = int((_time.time() - self._sweep["start"]) // 4)
        out = []
        for k in range(max(0, passes - 3), passes):  # like the service's bounded buffer: recent passes only
            out += simulated_samples(*self._sweep["range"], pass_index=k)
        return out

    def stop_sweep(self, sweep_id) -> None:
        pass


def _apple(status: int, separated: bool) -> dict[str, Any]:
    length = "19" if separated else "02"
    return {"company_id": "0x004C", "company_name": "Apple, Inc.", "data": f"12{length}{status:02X}00", "source": "manufacturer"}


BLE_CROWD = [
    # key, identity, manufacturer, device_type, detail, base rssi, appears, leaves (fractions of D)
    ("5A:11:00:00:00:01", "Apple, Inc.", _apple(0x00, False), "", "Find My frame: Apple device (status 0x00, near owner)", -62, 0.0, 0.6),
    ("5A:11:00:00:00:02", "Apple, Inc.", _apple(0x00, False), "", "Find My frame: Apple device (status 0x00, near owner)", -71, 0.0, None),
    ("5A:11:00:00:00:03", "Microsoft", {"company_name": "Microsoft", "data": "0301", "source": "manufacturer"}, "", "Swift Pair frame", -66, 0.0, None),
    ("5A:11:00:00:00:04", "Bose QC Ultra", None, "Headphones", "", -58, 0.0, None),
    ("5A:11:00:00:00:05", "Galaxy Watch6", None, "Watch", "", -64, 0.05, None),
    ("5A:11:00:00:00:06", "Tile, Inc.", {"company_name": "Tile, Inc.", "source": "uuid16", "uuid16": "0xFEED"}, "Tracker", "Tile UUID16 service", -74, 0.0, None),
    ("5A:11:00:00:00:07", "iBeacon", None, "Beacon", "iBeacon frame", -79, 0.0, None),
    ("5A:11:00:00:00:A7", "AirTag", _apple(0x10, True), "AirTag", "Find My frame: AirTag (status 0x10, separated from owner)", -52, 0.15, None),
    ("5A:11:00:00:00:B9", "Unknown BLE device", None, "", "", -38, 0.45, 0.8),
]
CLASSIC = [("A1B2C3", "LAP A1B2C3", -61, 0.0), ("5F00D1", "LAP 5F00D1", -70, 0.1)]
CELLS = [
    # rat, plmn, operator, cell_id, pci, rsrp, appears
    ("LTE", "310410", "AT&T", "0A11001", 101, -88, 0.0), ("5G NR", "310410", "AT&T", "0A11002", 102, -95, 0.0),
    ("LTE", "311480", "Verizon", "0B22001", 201, -84, 0.0), ("LTE", "311480", "Verizon", "0B22002", 202, -97, 0.0),
    ("5G NR", "311480", "Verizon", "0B22003", 203, -92, 0.0), ("LTE", "310260", "T-Mobile", "0C33001", 301, -90, 0.0),
    ("5G NR", "310260", "T-Mobile", "0C33002", 302, -86, 0.0), ("LTE", "313100", "AT&T FirstNet", "0A11003", 103, -99, 0.0),
    ("LTE", "311480", "Verizon", "0B22004", 204, -103, 0.0), ("LTE", "310260", "T-Mobile", "0C33003", 303, -101, 0.0),
    ("2G GSM", "00101", "unknown operator", "0F00001", None, -71, 0.55),
]
SERVING_ORDER = [("0B22001", 0.0), ("0C33002", 0.3), ("0A11001", 0.7)]


def _events(profile: str, elapsed: float, duration: float, rng: random.Random, run_tag: int = 0) -> list[dict[str, Any]]:
    f = elapsed / max(duration, 1.0)
    now = time.time()
    out: list[dict[str, Any]] = []
    if profile == "ble":
        for key, identity, mfr, dtype, detail, rssi, start, end in BLE_CROWD:
            if key.endswith(":B9"):
                # The strong newcomer is a different device in every scenario, so "new device" rules can fire.
                key = f"5A:11:00:{(run_tag >> 8) & 0xFF:02X}:{run_tag & 0xFF:02X}:B9"
            if f < start or (end is not None and f > end) or rng.random() > 0.55:
                continue
            out.append({"kind": "ble_adv", "simulated": True, "address": key, "identity": identity, "name": "",
                        "manufacturer": mfr, "device_type": dtype, "device_type_detail": detail,
                        "rssi_dbfs": round(rssi + rng.gauss(0, 2.5), 1), "channel": rng.choice((37, 38, 39)),
                        "pdu_type": "ADV_NONCONN_IND", "seen_at": now})
    elif profile == "classic":
        for lap, identity, rssi, start in CLASSIC:
            if f >= start and rng.random() < 0.4:
                out.append({"kind": "classic_lap", "simulated": True, "lap": lap, "identity": identity, "protocol": "btc",
                            "rssi_dbfs": round(rssi + rng.gauss(0, 3), 1), "channel": rng.randint(30, 50), "seen_at": now})
    elif profile == "cellular":
        serving = [cid for cid, at in SERVING_ORDER if f >= at][-1]
        for rat, plmn, operator, cid, pci, rsrp, start in CELLS:
            if f < start or (cid != serving and rng.random() > 0.35):
                continue
            generation = {"5G NR": "5G", "LTE": "4G", "2G GSM": "2G"}[rat]
            out.append({"kind": "cell", "simulated": True, "protocol": "cellular", "key": f"{rat}:{cid}",
                        "role": "serving" if cid == serving else "scanned", "rat": rat, "generation": generation,
                        "plmn": plmn, "operator": operator, "plmn_verified": True, "cell_id": cid, "pci": pci,
                        "rsrp_dbm": round(rsrp + rng.gauss(0, 2), 1), "rssi_dbfs": round(rsrp + rng.gauss(0, 2), 1),
                        "identity": f"{operator} · {rat}" + (f" · PCI {pci}" if pci is not None else ""), "seen_at": now})
    elif profile == "zigbee":
        for node in ("0x1A2B", "0x3C4D", "0x5E6F"):
            if rng.random() < 0.3:
                out.append({"kind": "zigbee_frame", "simulated": True, "source_address": node, "pan_id": "0xBEEF",
                            "rssi_dbfs": round(-60 + rng.gauss(0, 4), 1), "seen_at": now})
    elif profile == "tpms":
        for sensor in ("0x1F2E3D4C", "0x5B6A7988", "0x9C8D7E6F", "0x10213243"):
            if rng.random() < 0.15:
                out.append({"kind": "tpms", "simulated": True, "sensor_id": sensor, "pressure_kpa": round(230 + rng.gauss(0, 4)),
                            "rssi_dbfs": round(-55 + rng.gauss(0, 4), 1), "seen_at": now})
    elif profile == "drone":
        # One drone link: first a drone-like link the detector cannot confirm, then (from 40% of the scenario) the
        # confirmed detection on the same track; plus a steady 5.8 GHz WiFi-like link that never confirms.
        if f >= 0.15 and rng.random() < 0.25:
            confirmed = f >= 0.4
            out.append({"kind": "drone_detection" if confirmed else "drone_link", "simulated": True,
                        "protocol": "drone_rf", "key": "sim-drone-track-1", "track_id": "sim-drone-track-1",
                        "frequency_hz": 5_760_000_000.0, "confidence": round(0.8 if confirmed else 0.45, 2),
                        "device_type": "confirmed drone" if confirmed else "drone-like link",
                        "identity": ("Drone link (confirmed)" if confirmed else "Drone-like link (not confirmed)")
                                    + ": dji_ocusync, 5.8 GHz", "seen_at": now})
        if rng.random() < 0.1:
            out.append({"kind": "drone_link", "simulated": True, "protocol": "drone_rf", "key": "drone_link:ism_5g8:5825000000",
                        "frequency_hz": 5_825_000_000.0, "confidence": 0.3, "device_type": "drone-like link",
                        "identity": "Drone-like link (not confirmed): drone, 5.8 GHz", "seen_at": now})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, choices=("ble", "classic", "cellular", "zigbee", "tpms", "drone"))
    parser.add_argument("--duration", type=float, default=60.0, help="Scenario length (D); events continue after it")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--run-tag", type=int, default=None, help="Scenario identity (default: start time)")
    parser.add_argument("--offset", type=float, default=0.0,
                        help="Seconds already elapsed in the scenario (a playbook's later steps continue its clock)")
    args = parser.parse_args(argv)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    rng = random.Random(args.seed)
    started = time.monotonic()
    run_tag = args.run_tag if args.run_tag is not None else int(time.time() - args.offset)
    try:
        while not stop.is_set():
            for event in _events(args.profile, time.monotonic() - started + args.offset, args.duration, rng, run_tag):
                print(json.dumps(event), flush=True)
            stop.wait(args.interval)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
