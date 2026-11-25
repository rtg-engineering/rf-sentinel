from __future__ import annotations

import argparse
import base64
import csv
import json
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import websocket

from .detector import BLE_ADV_CHANNELS, BLE_ADV_SAMPLE_RATE_SPS, BLEAdvertisingDetector, WideBLEAdvertisingDetector
from .gateway import (
    gateway_base,
    list_devices,
    list_streams,
    list_sweeps,
    iq_sweep_chunk,
    retune_stream,
    start_iq_sweep,
    start_planned_sweep,
    start_stream,
    stop_iq_sweep,
    stop_stream,
    stop_sweep,
    sweep_samples,
    ws_url_for_stream,
)


DEFAULT_DEVICE_ID = "hackrf:0"
DEFAULT_LNA_GAIN_DB = 40
DEFAULT_VGA_GAIN_DB = 16
DEFAULT_WIDEBAND_LNA_GAIN_DB = 40
DEFAULT_WIDEBAND_VGA_GAIN_DB = 32
DEFAULT_UPPER_WIDEBAND_VGA_GAIN_DB = 62
DEFAULT_UI_DWELL_S = 0.25


def _bounded_gain_db(value: int | None, default: int, maximum: int) -> int:
    return max(0, min(int(maximum), int(default if value is None else value)))


def _lna_gain_db(value: int | None, default: int) -> int:
    return max(0, min(40, int(default if value is None else value)))


def _vga_gain_db(value: int | None, default: int) -> int:
    return _bounded_gain_db(value, default, 62)


def _install_stop_handlers(stop_requested: list[bool]) -> None:
    def handle_stop(signum: int, frame: object) -> None:
        stop_requested[0] = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    if hasattr(signal, "SIGQUIT"):
        signal.signal(signal.SIGQUIT, handle_stop)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bluetooth-lowenergy", description="RF Sentinel BLE advertising receiver")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list SDRs visible from sdr-gateway")
    devices.add_argument("--base-url", default=None)
    devices.add_argument("--token", default=None)

    listen = subparsers.add_parser("listen", help="listen for BLE advertising packets")
    listen.add_argument("--base-url", default=None)
    listen.add_argument("--token", default=None)
    listen.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    listen.add_argument("--channel", type=int, choices=sorted(BLE_ADV_CHANNELS), default=37)
    listen.add_argument("--hop", action="store_true", help="cycle BLE advertising channels 37, 38, and 39")
    listen.add_argument("--dwell-s", type=float, default=DEFAULT_UI_DWELL_S)
    listen.add_argument("--sample-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    listen.add_argument("--lna-gain-db", type=int, default=DEFAULT_LNA_GAIN_DB)
    listen.add_argument("--vga-gain-db", type=int, default=DEFAULT_VGA_GAIN_DB)
    listen.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    listen.add_argument("--baseband-filter-hz", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    listen.add_argument("--json", action="store_true")
    listen.add_argument("--csv", action="store_true")
    listen.add_argument("--debug-bursts", action="store_true")
    listen.add_argument("--max-events", type=int, default=0)
    listen.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    listen.add_argument("--replace-existing", action="store_true", help="stop existing sdr-gateway streams on this device before starting")

    scan = subparsers.add_parser("scan", help="UI-style BLE sweep across advertising channels 37, 38, and 39")
    scan.add_argument("--base-url", default=None)
    scan.add_argument("--token", default=None)
    scan.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    scan.add_argument("--dwell-s", type=float, default=DEFAULT_UI_DWELL_S)
    scan.add_argument("--sample-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    scan.add_argument("--lna-gain-db", type=int, default=DEFAULT_LNA_GAIN_DB)
    scan.add_argument("--vga-gain-db", type=int, default=DEFAULT_VGA_GAIN_DB)
    scan.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    scan.add_argument("--baseband-filter-hz", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--csv", action="store_true")
    scan.add_argument("--events", action="store_true", help="print one line per decoded packet instead of grouped summaries")
    scan.add_argument("--summary-interval-s", type=float, default=3.0)
    scan.add_argument("--top", type=int, default=12, help="maximum devices to show in grouped text summaries")
    scan.add_argument("--debug-bursts", action="store_true")
    scan.add_argument("--max-events", type=int, default=0)
    scan.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    scan.add_argument("--replace-existing", action="store_true", help="stop existing sdr-gateway streams on this device before starting")

    wideband = subparsers.add_parser("wideband-listen", help="listen to every BLE advertising channel visible inside one wide IQ stream")
    wideband.add_argument("--base-url", default=None)
    wideband.add_argument("--token", default=None)
    wideband.add_argument("--device-id", default="bladerf:0")
    wideband.add_argument("--center-freq-hz", type=int, default=2_414_000_000)
    wideband.add_argument("--sample-rate-sps", type=int, default=60_000_000)
    wideband.add_argument("--channel-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    wideband.add_argument("--channels", type=int, nargs="*", default=[37, 38, 39], choices=sorted(BLE_ADV_CHANNELS))
    wideband.add_argument("--lna-gain-db", type=int, default=DEFAULT_LNA_GAIN_DB)
    wideband.add_argument("--vga-gain-db", type=int, default=DEFAULT_VGA_GAIN_DB)
    wideband.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    wideband.add_argument("--baseband-filter-hz", type=int, default=0, help="0 means follow sample rate")
    wideband.add_argument("--json", action="store_true")
    wideband.add_argument("--csv", action="store_true")
    wideband.add_argument("--debug-bursts", action="store_true")
    wideband.add_argument("--max-events", type=int, default=0)
    wideband.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    wideband.add_argument("--replace-existing", action="store_true", help="stop existing sdr-gateway streams on this device before starting")

    band = subparsers.add_parser("band-scan", help="cover BLE advertising channels with two parked SDR windows")
    band.add_argument("--base-url", default=None)
    band.add_argument("--token", default=None)
    band.add_argument("--lower-device-id", default=DEFAULT_DEVICE_ID)
    band.add_argument("--lower-center-freq-hz", type=int, default=2_412_000_000)
    band.add_argument("--lower-sample-rate-sps", type=int, default=20_000_000)
    band.add_argument("--upper-device-id", default="bladerf:0")
    band.add_argument("--upper-center-freq-hz", type=int, default=2_453_000_000)
    band.add_argument("--upper-sample-rate-sps", type=int, default=60_000_000)
    band.add_argument("--channel-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    band.add_argument("--lna-gain-db", type=int, default=DEFAULT_WIDEBAND_LNA_GAIN_DB)
    band.add_argument("--vga-gain-db", type=int, default=DEFAULT_WIDEBAND_VGA_GAIN_DB)
    band.add_argument("--lower-lna-gain-db", type=int, default=None, help="override LNA gain for the lower SDR window")
    band.add_argument("--lower-vga-gain-db", type=int, default=None, help="override VGA gain for the lower SDR window")
    band.add_argument("--upper-lna-gain-db", type=int, default=None, help="override LNA gain for the upper SDR window")
    band.add_argument("--upper-vga-gain-db", type=int, default=None, help="override VGA gain for the upper SDR window")
    band.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    band.add_argument("--json", action="store_true")
    band.add_argument("--csv", action="store_true")
    band.add_argument("--events", action="store_true", help="print one line per decoded packet instead of grouped summaries")
    band.add_argument("--summary-interval-s", type=float, default=3.0)
    band.add_argument("--top", type=int, default=20, help="maximum devices to show in grouped text summaries")
    band.add_argument("--debug-bursts", action="store_true")
    band.add_argument("--max-events", type=int, default=0)
    band.add_argument("--replace-existing", action="store_true", help="stop existing streams on both devices before starting")

    dual = subparsers.add_parser("dual-hop", help="hop BLE advertising channels with two SDRs at once")
    dual.add_argument("--base-url", default=None)
    dual.add_argument("--token", default=None)
    dual.add_argument("--device-a-id", default=DEFAULT_DEVICE_ID)
    dual.add_argument("--device-a-channels", type=int, nargs="+", default=[37, 38], choices=sorted(BLE_ADV_CHANNELS))
    dual.add_argument("--device-b-id", default="bladerf:0")
    dual.add_argument("--device-b-channels", type=int, nargs="+", default=[39, 38], choices=sorted(BLE_ADV_CHANNELS))
    dual.add_argument("--dwell-s", type=float, default=1.5, help="seconds per channel; retune has about a 1s settle period")
    dual.add_argument("--sample-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    dual.add_argument("--lna-gain-db", type=int, default=DEFAULT_LNA_GAIN_DB)
    dual.add_argument("--vga-gain-db", type=int, default=DEFAULT_VGA_GAIN_DB)
    dual.add_argument("--device-a-lna-gain-db", type=int, default=None)
    dual.add_argument("--device-a-vga-gain-db", type=int, default=None)
    dual.add_argument("--device-b-lna-gain-db", type=int, default=None)
    dual.add_argument("--device-b-vga-gain-db", type=int, default=DEFAULT_UPPER_WIDEBAND_VGA_GAIN_DB)
    dual.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    dual.add_argument("--baseband-filter-hz", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    dual.add_argument("--json", action="store_true")
    dual.add_argument("--csv", action="store_true")
    dual.add_argument("--events", action="store_true", help="print one line per decoded packet instead of grouped summaries")
    dual.add_argument("--summary-interval-s", type=float, default=3.0)
    dual.add_argument("--top", type=int, default=20, help="maximum devices to show in grouped text summaries")
    dual.add_argument("--debug-bursts", action="store_true")
    dual.add_argument("--max-events", type=int, default=0)
    dual.add_argument("--replace-existing", action="store_true", help="stop existing streams on both devices before starting")

    sweep = subparsers.add_parser("sweep", help="use gateway native sweep to find BLE advertising-channel energy")
    sweep.add_argument("--base-url", default=None)
    sweep.add_argument("--token", default=None)
    sweep.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    sweep.add_argument("--channels", type=int, nargs="+", default=[37, 38, 39], choices=sorted(BLE_ADV_CHANNELS))
    sweep.add_argument("--margin-hz", type=int, default=2_000_000)
    sweep.add_argument("--bin-width-hz", type=int, default=100_000)
    sweep.add_argument("--lna-gain-db", type=int, default=40)
    sweep.add_argument("--vga-gain-db", type=int, default=62)
    sweep.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    sweep.add_argument("--interval-s", type=float, default=1.0)
    sweep.add_argument("--max-prints", type=int, default=0)
    sweep.add_argument("--max-events", type=int, default=0)
    sweep.add_argument("--json", action="store_true")
    sweep.add_argument("--csv", action="store_true")
    sweep.add_argument("--follow-decode", action="store_true", help="pause sweep and decode the strongest BLE channel with normal IQ demod")
    sweep.add_argument("--decode-dwell-s", type=float, default=2.0)
    sweep.add_argument("--decode-threshold-db", type=float, default=-35.0)
    sweep.add_argument("--sample-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    sweep.add_argument("--baseband-filter-hz", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    sweep.add_argument("--debug-bursts", action="store_true")
    sweep.add_argument("--events", action="store_true", help="with --follow-decode, print one line per decoded packet")
    sweep.add_argument("--summary-interval-s", type=float, default=2.0)
    sweep.add_argument("--top", type=int, default=12, help="with --follow-decode, maximum devices to show in grouped summaries")
    sweep.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    sweep.add_argument("--replace-existing", action="store_true", help="stop existing streams/sweeps on this device before starting")

    iq_sweep = subparsers.add_parser("iq-sweep", help="decode BLE advertisements from a gateway-managed IQ sweep")
    iq_sweep.add_argument("--base-url", default=None)
    iq_sweep.add_argument("--token", default=None)
    iq_sweep.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    iq_sweep.add_argument("--channels", type=int, nargs="+", default=[37, 38, 39], choices=sorted(BLE_ADV_CHANNELS))
    iq_sweep.add_argument("--start-freq-hz", type=int, default=None)
    iq_sweep.add_argument("--stop-freq-hz", type=int, default=None)
    iq_sweep.add_argument("--hop-hz", type=int, default=None)
    iq_sweep.add_argument("--sample-rate-sps", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    iq_sweep.add_argument("--dwell-s", type=float, default=0.5)
    iq_sweep.add_argument("--lna-gain-db", type=int, default=40)
    iq_sweep.add_argument("--vga-gain-db", type=int, default=40)
    iq_sweep.add_argument("--amp-enable", action=argparse.BooleanOptionalAction, default=False)
    iq_sweep.add_argument("--baseband-filter-hz", type=int, default=BLE_ADV_SAMPLE_RATE_SPS)
    iq_sweep.add_argument("--chunk-bytes", type=int, default=131072)
    iq_sweep.add_argument("--json", action="store_true")
    iq_sweep.add_argument("--csv", action="store_true")
    iq_sweep.add_argument("--events", action="store_true", help="print one line per decoded packet instead of grouped summaries")
    iq_sweep.add_argument("--summary-interval-s", type=float, default=3.0)
    iq_sweep.add_argument("--top", type=int, default=12)
    iq_sweep.add_argument("--debug-bursts", action="store_true")
    iq_sweep.add_argument("--max-events", type=int, default=0)
    iq_sweep.add_argument("--replace-existing", action="store_true", help="stop existing streams/sweeps on this device before starting")
    return parser


@dataclass
class BLEDeviceRow:
    address: str
    first_seen: float
    last_seen: float
    hits: int = 0
    channels: set[int] = field(default_factory=set)
    best_rssi_dbfs: float = -120.0
    last_rssi_dbfs: float = -120.0
    names: set[str] = field(default_factory=set)
    uuid16: set[str] = field(default_factory=set)
    uuid16_names: set[str] = field(default_factory=set)
    manufacturer_name: str = ""
    manufacturer_id: str = ""
    manufacturer_data_prefix: str = ""
    identity: str = ""
    identity_source: str = ""
    device_type: str = ""
    device_type_detail: str = ""
    address_type: str = ""


class BLETextReporter:
    def __init__(self, summary_interval_s: float = 3.0, top: int = 12) -> None:
        self.summary_interval_s = max(0.5, float(summary_interval_s))
        self.top = max(1, int(top))
        self.devices: dict[str, BLEDeviceRow] = {}
        self.started_at = time.time()
        self.last_summary_at = 0.0
        self.total_events = 0
        self.last_channel = "-"

    def record(self, event: dict[str, Any]) -> None:
        if event.get("kind") != "ble_adv":
            return
        now = float(event.get("seen_at") or time.time())
        address = str(event.get("address") or "unknown")
        row = self.devices.get(address)
        if row is None:
            row = BLEDeviceRow(address=address, first_seen=now, last_seen=now)
            self.devices[address] = row
        row.hits += 1
        self.total_events += 1
        row.last_seen = now
        channel = _safe_int(event.get("channel"))
        if channel is not None:
            row.channels.add(channel)
            self.last_channel = str(channel)
        rssi = _safe_float(event.get("rssi_dbfs"), -120.0)
        row.last_rssi_dbfs = rssi
        row.best_rssi_dbfs = max(row.best_rssi_dbfs, rssi)
        if event.get("name"):
            row.names.add(str(event.get("name")))
        for uuid in event.get("uuid16") or []:
            row.uuid16.add(str(uuid))
        for name in event.get("uuid16_names") or []:
            row.uuid16_names.add(str(name))
        manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else {}
        manufacturer_name = str(manufacturer.get("company_name") or "").strip()
        if manufacturer_name:
            row.manufacturer_name = manufacturer_name
        manufacturer_id = str(manufacturer.get("company_id") or "").strip()
        if manufacturer_id:
            row.manufacturer_id = manufacturer_id
        manufacturer_data = str(manufacturer.get("data") or "").upper()
        if manufacturer_data:
            row.manufacturer_data_prefix = manufacturer_data[:16]
        for key, attr in (
            ("identity", "identity"),
            ("identity_source", "identity_source"),
            ("device_type", "device_type"),
            ("device_type_detail", "device_type_detail"),
            ("address_type", "address_type"),
        ):
            value = str(event.get(key) or "").strip()
            if value:
                setattr(row, attr, value)

    def maybe_print_summary(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_summary_at) < self.summary_interval_s:
            return
        self.last_summary_at = now
        self.print_summary(now)

    def print_summary(self, now: float | None = None) -> None:
        now = now or time.time()
        rows = sorted(self.devices.values(), key=lambda row: (row.hits, row.best_rssi_dbfs, row.last_seen), reverse=True)
        print("", flush=True)
        print(
            f"=== BLE Summary devices={len(rows)} detections={self.total_events} "
            f"runtime={_age(now - self.started_at)} last_ch={self.last_channel} ===",
            flush=True,
        )
        if not rows:
            print("No BLE advertisements decoded yet.", flush=True)
            return
        grouped: dict[str, list[BLEDeviceRow]] = {}
        for row in rows:
            grouped.setdefault(self._group_name(row), []).append(row)
        groups = sorted(grouped.items(), key=lambda item: (sum(row.hits for row in item[1]), len(item[1])), reverse=True)
        shown = 0
        for manufacturer, members in groups:
            if shown >= self.top:
                break
            detections = sum(row.hits for row in members)
            best_rssi = max(row.best_rssi_dbfs for row in members)
            newest = max(row.last_seen for row in members)
            channels = sorted({ch for row in members for ch in row.channels})
            print(
                f"{manufacturer} | devices={len(members)} detections={detections} "
                f"best={best_rssi:.1f} dBFS last={_age(now - newest)} ch={','.join(map(str, channels)) or '-'}",
                flush=True,
            )
            for row in sorted(members, key=lambda item: (item.hits, item.best_rssi_dbfs), reverse=True):
                if shown >= self.top:
                    break
                shown += 1
                print(f"  {_device_line(row, now)}", flush=True)
        if len(rows) > shown:
            print(f"  ... {len(rows) - shown} more devices", flush=True)

    @staticmethod
    def _group_name(row: BLEDeviceRow) -> str:
        if row.manufacturer_name:
            return row.manufacturer_name
        if row.uuid16_names:
            return sorted(row.uuid16_names)[0]
        if row.names:
            return sorted(row.names)[0]
        return "Unknown"


def _run_devices(args: argparse.Namespace) -> int:
    for dev in list_devices(args.base_url, args.token):
        print(json.dumps(dev, sort_keys=True))
    return 0


def _stop_existing_streams_for_device(args: argparse.Namespace, device_id: str | None = None) -> None:
    target_device_id = str(device_id or args.device_id)
    try:
        streams = list_streams(args.base_url, args.token)
    except Exception as exc:
        print(f"warning: could not list existing streams: {exc}", file=sys.stderr, flush=True)
        return
    for stream in streams:
        config = stream.get("config") if isinstance(stream.get("config"), dict) else {}
        if str(config.get("device_id") or "") != target_device_id:
            continue
        stream_id = str(stream.get("stream_id") or "")
        if not stream_id:
            continue
        print(f"stopping existing stream {stream_id} on {target_device_id}", file=sys.stderr, flush=True)
        stop_stream(args.base_url, args.token, stream_id)


def _stop_existing_sweeps_for_device(args: argparse.Namespace, device_id: str | None = None) -> None:
    target_device_id = str(device_id or args.device_id)
    try:
        sweeps = list_sweeps(args.base_url, args.token)
    except Exception as exc:
        print(f"warning: could not list existing sweeps: {exc}", file=sys.stderr, flush=True)
        return
    for sweep in sweeps:
        config = sweep.get("config") if isinstance(sweep.get("config"), dict) else {}
        if str(config.get("device_id") or "") != target_device_id:
            continue
        sweep_id = str(sweep.get("sweep_id") or "")
        if not sweep_id:
            continue
        print(f"stopping existing sweep {sweep_id} on {target_device_id}", file=sys.stderr, flush=True)
        stop_sweep(args.base_url, args.token, sweep_id)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ble_channel_for_center(center_freq_hz: int, allowed_channels: list[int] | None = None) -> int | None:
    allowed = set(int(ch) for ch in (allowed_channels or sorted(BLE_ADV_CHANNELS)))
    best_channel: int | None = None
    best_delta: int | None = None
    for channel, freq_hz in BLE_ADV_CHANNELS.items():
        if int(channel) not in allowed:
            continue
        delta = abs(int(freq_hz) - int(center_freq_hz))
        if best_delta is None or delta < best_delta:
            best_channel = int(channel)
            best_delta = delta
    # Narrow BLE IQ sweep centers should land on the advertising channel.
    if best_delta is not None and best_delta <= 1_000_000:
        return best_channel
    return None


def _age(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _device_line(row: BLEDeviceRow, now: float) -> str:
    label = row.identity or next(iter(sorted(row.names)), "") or row.manufacturer_name or row.address
    chips: list[str] = [row.address]
    if row.address_type:
        chips.append(row.address_type)
    if row.device_type:
        chips.append(f"type={row.device_type}")
    if row.device_type_detail:
        chips.append(row.device_type_detail)
    if row.identity_source:
        chips.append(row.identity_source)
    if row.uuid16:
        chips.append("uuid16=" + ",".join(sorted(row.uuid16)))
    if row.manufacturer_id:
        chips.append(f"mfg={row.manufacturer_id}")
    if row.manufacturer_data_prefix:
        chips.append(f"data={row.manufacturer_data_prefix}...")
    channels = ",".join(map(str, sorted(row.channels))) or "-"
    return (
        f"{label} | hits={row.hits} best={row.best_rssi_dbfs:.1f} dBFS "
        f"last={_age(now - row.last_seen)} ch={channels} | " + " | ".join(chips)
    )


CSV_FIELDS = [
    "seen_at",
    "device_id",
    "window",
    "channel",
    "rssi_dbfs",
    "address",
    "address_type",
    "pdu_type",
    "identity",
    "manufacturer",
    "manufacturer_id",
    "device_type",
    "device_type_detail",
    "name",
    "uuid16",
    "payload_len",
    "confidence",
    "decoder",
    "packet",
]


def _csv_row(event: dict[str, Any]) -> dict[str, Any]:
    manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else {}
    return {
        "seen_at": f"{float(event.get('seen_at') or time.time()):.6f}",
        "device_id": str(event.get("device_id") or ""),
        "window": str(event.get("window") or ""),
        "channel": event.get("channel") or "",
        "rssi_dbfs": event.get("rssi_dbfs") or "",
        "address": str(event.get("address") or ""),
        "address_type": str(event.get("address_type") or ""),
        "pdu_type": str(event.get("pdu_type") or ""),
        "identity": str(event.get("identity") or ""),
        "manufacturer": str(manufacturer.get("company_name") or ""),
        "manufacturer_id": str(manufacturer.get("company_id") or ""),
        "device_type": str(event.get("device_type") or ""),
        "device_type_detail": str(event.get("device_type_detail") or ""),
        "name": str(event.get("name") or ""),
        "uuid16": ";".join(str(item) for item in (event.get("uuid16") or [])),
        "payload_len": event.get("payload_len") or "",
        "confidence": event.get("confidence") or "",
        "decoder": str(event.get("decoder") or ""),
        "packet": str(event.get("packet") or ""),
    }


def _print_csv_header() -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    sys.stdout.flush()


def _print_csv_event(event: dict[str, Any]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writerow(_csv_row(event))
    sys.stdout.flush()


def _print_event(event: dict[str, Any], as_json: bool, as_csv: bool = False) -> None:
    if as_json:
        print(json.dumps(event, sort_keys=True), flush=True)
        return
    if as_csv:
        if event.get("kind") == "ble_adv":
            _print_csv_event(event)
        return
    if event.get("kind") == "ble_adv":
        name = f" name={json.dumps(event.get('name'))}" if event.get("name") else ""
        manufacturer = event.get("manufacturer") or {}
        company = f" company={json.dumps(manufacturer.get('company_name'))}" if manufacturer.get("company_name") else ""
        print(
            f"ble_adv ch={event.get('channel')} addr={event.get('address')} type={event.get('address_type')} "
            f"pdu={event.get('pdu_type')} rssi_dbfs={event.get('rssi_dbfs')}{name}{company}",
            flush=True,
        )
        return
    print(
        f"ble_burst ch={event.get('channel')} rssi_dbfs={event.get('rssi_dbfs')} peak_dbfs={event.get('peak_dbfs')}",
        flush=True,
    )


def _listen_one_channel(args: argparse.Namespace, channel: int, stop_requested: list[bool], reporter: BLETextReporter | None = None) -> int:
    center = BLE_ADV_CHANNELS[channel]
    if getattr(args, "replace_existing", False):
        _stop_existing_streams_for_device(args)
        args.replace_existing = False
    body = start_stream(
        base_url=args.base_url,
        token=args.token,
        device_id=args.device_id,
        center_freq_hz=center,
        sample_rate_sps=args.sample_rate_sps,
        lna_gain_db=args.lna_gain_db,
        vga_gain_db=args.vga_gain_db,
        amp_enable=args.amp_enable,
        baseband_filter_hz=args.baseband_filter_hz,
    )
    stream_id = str(body["stream_id"])
    accepted = body.get("config", {}) or {}
    sample_rate = int(accepted.get("sample_rate_sps", args.sample_rate_sps))
    detector = BLEAdvertisingDetector(sample_rate_sps=sample_rate, center_freq_hz=center, channel=channel)
    mode_label = "scan-hop" if getattr(args, "hop", False) else "listen"
    print(
        f"using mode={mode_label} device={args.device_id} ch={channel} center={center} sr={sample_rate} "
        f"lna={args.lna_gain_db} vga={args.vga_gain_db} amp={int(args.amp_enable)}",
        file=sys.stderr,
        flush=True,
    )
    events_seen = 0
    started = time.monotonic()
    try:
        while not stop_requested[0]:
            try:
                ws = websocket.create_connection(ws_url_for_stream(gateway_base(args.base_url), stream_id, args.token), timeout=8)
                ws.settimeout(1.0)
                while not stop_requested[0]:
                    if args.hop and time.monotonic() - started >= args.dwell_s:
                        return events_seen
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    _, events = detector.process_iq_i8(bytes(chunk))
                    for event in events:
                        if event.get("kind") != "ble_adv" and not args.debug_bursts:
                            continue
                        event["device_id"] = args.device_id
                        event["window"] = mode_label
                        if event.get("kind") == "ble_adv":
                            if reporter is not None and not args.json and not args.csv and not getattr(args, "events", False):
                                reporter.record(event)
                            else:
                                _print_event(event, args.json, args.csv)
                            events_seen += 1
                            if args.max_events and events_seen >= args.max_events:
                                stop_requested[0] = True
                                return events_seen
                        else:
                            _print_event(event, args.json, args.csv)
                    if reporter is not None and not args.json and not args.csv and not getattr(args, "events", False):
                        reporter.maybe_print_summary()
            except Exception as exc:
                print(f"stream error={exc}; reconnecting in {args.reconnect_delay_seconds:.1f}s", file=sys.stderr, flush=True)
                time.sleep(max(0.1, args.reconnect_delay_seconds))
            finally:
                try:
                    ws.close()  # type: ignore[name-defined]
                except Exception:
                    pass
    finally:
        stop_stream(args.base_url, args.token, stream_id)
        if args.json and getattr(detector, "stats", None):
            # Decoder yield for this dwell: candidates, failures by reason, CRC-valid packets.
            _print_event({"kind": "ble_decoder_stats", "channel": channel, "device_id": args.device_id,
                          "dwell_s": round(time.monotonic() - started, 2), **detector.stats}, True)
    return events_seen


def _run_wideband_listen(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    baseband_filter_hz = int(args.baseband_filter_hz or args.sample_rate_sps)
    if getattr(args, "replace_existing", False):
        _stop_existing_streams_for_device(args)
    body = start_stream(
        base_url=args.base_url,
        token=args.token,
        device_id=args.device_id,
        center_freq_hz=args.center_freq_hz,
        sample_rate_sps=args.sample_rate_sps,
        lna_gain_db=args.lna_gain_db,
        vga_gain_db=args.vga_gain_db,
        amp_enable=args.amp_enable,
        baseband_filter_hz=baseband_filter_hz,
    )
    stream_id = str(body["stream_id"])
    accepted = body.get("config", {}) or {}
    sample_rate = int(accepted.get("sample_rate_sps", args.sample_rate_sps))
    center_freq = int(accepted.get("center_freq_hz", args.center_freq_hz))
    detector = WideBLEAdvertisingDetector(
        sample_rate_sps=sample_rate,
        center_freq_hz=center_freq,
        channels=list(args.channels or sorted(BLE_ADV_CHANNELS)),
        channel_rate_sps=args.channel_rate_sps,
    )
    visible = ",".join(str(lane["channel"]) for lane in detector.lanes) or "-"
    print(
        f"using device={args.device_id} center={center_freq} sr={sample_rate} "
        f"visible_ble_channels={visible} lna={args.lna_gain_db} vga={args.vga_gain_db} amp={int(args.amp_enable)}",
        file=sys.stderr,
        flush=True,
    )
    events_seen = 0
    try:
        while not stop_requested[0]:
            try:
                ws = websocket.create_connection(ws_url_for_stream(gateway_base(args.base_url), stream_id, args.token), timeout=8)
                ws.settimeout(1.0)
                while not stop_requested[0]:
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    _, events = detector.process_iq_i8(bytes(chunk))
                    for event in events:
                        if event.get("kind") != "ble_adv" and not args.debug_bursts:
                            continue
                        event["device_id"] = args.device_id
                        event["window"] = "wideband"
                        _print_event(event, args.json, args.csv)
                        if event.get("kind") == "ble_adv":
                            events_seen += 1
                            if args.max_events and events_seen >= args.max_events:
                                stop_requested[0] = True
                                return 0
            except Exception as exc:
                print(f"stream error={exc}; reconnecting in {args.reconnect_delay_seconds:.1f}s", file=sys.stderr, flush=True)
                time.sleep(max(0.1, args.reconnect_delay_seconds))
            finally:
                try:
                    ws.close()  # type: ignore[name-defined]
                except Exception:
                    pass
    finally:
        stop_stream(args.base_url, args.token, stream_id)
    return 0


def _narrow_hop_worker(
    *,
    args: argparse.Namespace,
    label: str,
    device_id: str,
    channels: list[int],
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    event_queue: "queue.Queue[dict[str, Any]]",
    stop_requested: list[bool],
) -> None:
    stream_id = ""
    channel_index = 0
    ws = None
    try:
        channel = int(channels[channel_index % len(channels)])
        center = BLE_ADV_CHANNELS[channel]
        body = start_stream(
            base_url=args.base_url,
            token=args.token,
            device_id=device_id,
            center_freq_hz=center,
            sample_rate_sps=sample_rate_sps,
            lna_gain_db=lna_gain_db,
            vga_gain_db=vga_gain_db,
            amp_enable=args.amp_enable,
            baseband_filter_hz=args.baseband_filter_hz,
        )
        stream_id = str(body["stream_id"])
        accepted = body.get("config", {}) or {}
        actual_rate = int(accepted.get("sample_rate_sps", sample_rate_sps))
        detector = BLEAdvertisingDetector(sample_rate_sps=actual_rate, center_freq_hz=center, channel=channel)
        event_queue.put(
            {
                "kind": "status",
                "message": (
                    f"{label}: device={device_id} ch={channel} center={center} sr={actual_rate} "
                    f"lna={lna_gain_db} vga={vga_gain_db} hop_channels={','.join(map(str, channels))}"
                ),
            }
        )
        next_retune_at = time.monotonic() + max(1.05, float(args.dwell_s))
        ws = websocket.create_connection(ws_url_for_stream(gateway_base(args.base_url), stream_id, args.token), timeout=8)
        ws.settimeout(0.25)
        while not stop_requested[0]:
            now = time.monotonic()
            if now >= next_retune_at:
                channel_index += 1
                channel = int(channels[channel_index % len(channels)])
                center = BLE_ADV_CHANNELS[channel]
                body = retune_stream(
                    base_url=args.base_url,
                    token=args.token,
                    stream_id=stream_id,
                    device_id=device_id,
                    center_freq_hz=center,
                    sample_rate_sps=sample_rate_sps,
                    lna_gain_db=lna_gain_db,
                    vga_gain_db=vga_gain_db,
                    amp_enable=args.amp_enable,
                    baseband_filter_hz=args.baseband_filter_hz,
                )
                accepted = body.get("config", {}) or {}
                actual_rate = int(accepted.get("sample_rate_sps", sample_rate_sps))
                detector = BLEAdvertisingDetector(sample_rate_sps=actual_rate, center_freq_hz=center, channel=channel)
                next_retune_at = time.monotonic() + max(1.05, float(args.dwell_s))
                event_queue.put(
                    {
                        "kind": "status",
                        "message": f"{label}: retuned device={device_id} ch={channel} center={center} sr={actual_rate}",
                    }
                )
                continue
            try:
                chunk = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            _, events = detector.process_iq_i8(bytes(chunk))
            for event in events:
                if event.get("kind") != "ble_adv" and not args.debug_bursts:
                    continue
                event["window"] = label
                event["device_id"] = device_id
                event_queue.put(event)
    except Exception as exc:
        event_queue.put({"kind": "fatal", "message": f"{label}: {exc}"})
        stop_requested[0] = True
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        if stream_id:
            stop_stream(args.base_url, args.token, stream_id)


def _wideband_worker(
    *,
    args: argparse.Namespace,
    label: str,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    channels: list[int],
    event_queue: "queue.Queue[dict[str, Any]]",
    stop_requested: list[bool],
) -> None:
    stream_id = ""
    try:
        body = start_stream(
            base_url=args.base_url,
            token=args.token,
            device_id=device_id,
            center_freq_hz=center_freq_hz,
            sample_rate_sps=sample_rate_sps,
            lna_gain_db=lna_gain_db,
            vga_gain_db=vga_gain_db,
            amp_enable=args.amp_enable,
            baseband_filter_hz=sample_rate_sps,
        )
        stream_id = str(body["stream_id"])
        accepted = body.get("config", {}) or {}
        actual_rate = int(accepted.get("sample_rate_sps", sample_rate_sps))
        actual_center = int(accepted.get("center_freq_hz", center_freq_hz))
        detector = WideBLEAdvertisingDetector(
            sample_rate_sps=actual_rate,
            center_freq_hz=actual_center,
            channels=channels,
            channel_rate_sps=args.channel_rate_sps,
            guard_hz=0,
        )
        visible = ",".join(str(lane["channel"]) for lane in detector.lanes) or "-"
        requested = ",".join(str(channel) for channel in channels) or "-"
        missing = sorted(set(channels) - {int(lane["channel"]) for lane in detector.lanes})
        missing_note = f" missing_requested={','.join(map(str, missing))}" if missing else ""
        event_queue.put(
            {
                "kind": "status",
                "message": (
                    f"{label}: device={device_id} center={actual_center} sr={actual_rate} "
                    f"lna={lna_gain_db} vga={vga_gain_db} "
                    f"requested_ble_channels={requested} visible_ble_channels={visible}{missing_note}"
                ),
            }
        )
        ws = None
        while not stop_requested[0]:
            try:
                ws = websocket.create_connection(ws_url_for_stream(gateway_base(args.base_url), stream_id, args.token), timeout=8)
                ws.settimeout(1.0)
                while not stop_requested[0]:
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    _, events = detector.process_iq_i8(bytes(chunk))
                    for event in events:
                        if event.get("kind") != "ble_adv" and not args.debug_bursts:
                            continue
                        event["window"] = label
                        event["device_id"] = device_id
                        event_queue.put(event)
            except Exception as exc:
                event_queue.put({"kind": "error", "message": f"{label}: stream error={exc}; reconnecting"})
                time.sleep(1.0)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
    except Exception as exc:
        event_queue.put({"kind": "fatal", "message": f"{label}: {exc}"})
        stop_requested[0] = True
    finally:
        if stream_id:
            stop_stream(args.base_url, args.token, stream_id)


def _run_band_scan(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    if args.replace_existing:
        _stop_existing_streams_for_device(args, args.lower_device_id)
        _stop_existing_streams_for_device(args, args.upper_device_id)

    event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    reporter = None if args.json or args.csv or args.events else BLETextReporter(args.summary_interval_s, args.top)
    lower_thread = threading.Thread(
        target=_wideband_worker,
        kwargs={
            "args": args,
            "label": "lower20",
            "device_id": args.lower_device_id,
            "center_freq_hz": args.lower_center_freq_hz,
            "sample_rate_sps": args.lower_sample_rate_sps,
            "lna_gain_db": _lna_gain_db(args.lower_lna_gain_db, args.lna_gain_db),
            "vga_gain_db": _vga_gain_db(args.lower_vga_gain_db, args.vga_gain_db),
            "channels": [37],
            "event_queue": event_queue,
            "stop_requested": stop_requested,
        },
        daemon=True,
    )
    upper_thread = threading.Thread(
        target=_wideband_worker,
        kwargs={
            "args": args,
            "label": "upper60",
            "device_id": args.upper_device_id,
            "center_freq_hz": args.upper_center_freq_hz,
            "sample_rate_sps": args.upper_sample_rate_sps,
            "lna_gain_db": _lna_gain_db(args.upper_lna_gain_db, args.lna_gain_db),
            "vga_gain_db": _vga_gain_db(args.upper_vga_gain_db, DEFAULT_UPPER_WIDEBAND_VGA_GAIN_DB),
            "channels": [38, 39],
            "event_queue": event_queue,
            "stop_requested": stop_requested,
        },
        daemon=True,
    )
    lower_thread.start()
    upper_thread.start()

    events_seen = 0
    try:
        while not stop_requested[0]:
            try:
                event = event_queue.get(timeout=0.25)
            except queue.Empty:
                if reporter is not None:
                    reporter.maybe_print_summary()
                continue
            kind = str(event.get("kind") or "")
            if kind == "status":
                print(str(event.get("message") or ""), file=sys.stderr, flush=True)
                continue
            if kind in {"error", "fatal"}:
                print(str(event.get("message") or ""), file=sys.stderr, flush=True)
                if kind == "fatal":
                    return 1
                continue
            if kind != "ble_adv" and not args.debug_bursts:
                continue
            if kind == "ble_adv":
                if reporter is not None:
                    reporter.record(event)
                else:
                    _print_event(event, args.json, args.csv)
                events_seen += 1
                if args.max_events and events_seen >= args.max_events:
                    stop_requested[0] = True
                    break
            else:
                _print_event(event, args.json, args.csv)
            if reporter is not None:
                reporter.maybe_print_summary()
    finally:
        stop_requested[0] = True
        lower_thread.join(timeout=3.0)
        upper_thread.join(timeout=3.0)
        if reporter is not None:
            reporter.maybe_print_summary(force=True)
    return 0


def _run_dual_hop(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    if args.replace_existing:
        _stop_existing_streams_for_device(args, args.device_a_id)
        _stop_existing_streams_for_device(args, args.device_b_id)

    event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    reporter = None if args.json or args.csv or args.events else BLETextReporter(args.summary_interval_s, args.top)
    threads = [
        threading.Thread(
            target=_narrow_hop_worker,
            kwargs={
                "args": args,
                "label": "hop-a",
                "device_id": args.device_a_id,
                "channels": list(args.device_a_channels),
                "sample_rate_sps": args.sample_rate_sps,
                "lna_gain_db": _lna_gain_db(args.device_a_lna_gain_db, args.lna_gain_db),
                "vga_gain_db": _vga_gain_db(args.device_a_vga_gain_db, args.vga_gain_db),
                "event_queue": event_queue,
                "stop_requested": stop_requested,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_narrow_hop_worker,
            kwargs={
                "args": args,
                "label": "hop-b",
                "device_id": args.device_b_id,
                "channels": list(args.device_b_channels),
                "sample_rate_sps": args.sample_rate_sps,
                "lna_gain_db": _lna_gain_db(args.device_b_lna_gain_db, args.lna_gain_db),
                "vga_gain_db": _vga_gain_db(args.device_b_vga_gain_db, DEFAULT_UPPER_WIDEBAND_VGA_GAIN_DB),
                "event_queue": event_queue,
                "stop_requested": stop_requested,
            },
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    events_seen = 0
    try:
        while not stop_requested[0]:
            try:
                event = event_queue.get(timeout=0.25)
            except queue.Empty:
                if reporter is not None:
                    reporter.maybe_print_summary()
                continue
            kind = str(event.get("kind") or "")
            if kind == "status":
                print(str(event.get("message") or ""), file=sys.stderr, flush=True)
                continue
            if kind in {"error", "fatal"}:
                print(str(event.get("message") or ""), file=sys.stderr, flush=True)
                if kind == "fatal":
                    return 1
                continue
            if kind != "ble_adv" and not args.debug_bursts:
                continue
            if kind == "ble_adv":
                if reporter is not None:
                    reporter.record(event)
                else:
                    _print_event(event, args.json, args.csv)
                events_seen += 1
                if args.max_events and events_seen >= args.max_events:
                    stop_requested[0] = True
                    break
            else:
                _print_event(event, args.json, args.csv)
            if reporter is not None:
                reporter.maybe_print_summary()
    finally:
        stop_requested[0] = True
        for thread in threads:
            thread.join(timeout=3.0)
        if reporter is not None:
            reporter.maybe_print_summary(force=True)
    return 0


def _sweep_channel_power(sample: dict[str, Any], freq_hz: int, radius_hz: int) -> float | None:
    try:
        hz_low = int(sample.get("hz_low"))
        hz_high = int(sample.get("hz_high"))
        db_values = [float(value) for value in sample.get("db_values") or []]
    except (TypeError, ValueError):
        return None
    if not db_values or hz_high <= hz_low:
        return None
    bin_width = float(hz_high - hz_low) / float(len(db_values))
    powers: list[float] = []
    for idx, value in enumerate(db_values):
        bin_center = hz_low + ((idx + 0.5) * bin_width)
        if abs(bin_center - float(freq_hz)) <= float(radius_hz):
            powers.append(value)
    return max(powers) if powers else None


def _best_sweep_channel(samples: list[dict[str, Any]], channels: list[int], bin_width_hz: int) -> tuple[int | None, float | None]:
    best_channel: int | None = None
    best_power: float | None = None
    radius_hz = max(int(bin_width_hz), 250_000)
    for sample in samples:
        for channel in channels:
            power = _sweep_channel_power(sample, BLE_ADV_CHANNELS[int(channel)], radius_hz)
            if power is None:
                continue
            if best_power is None or power > best_power:
                best_channel = int(channel)
                best_power = float(power)
    return best_channel, best_power


def _start_ble_sweep(args: argparse.Namespace) -> tuple[str, str, dict[str, Any]]:
    frequencies = [BLE_ADV_CHANNELS[int(channel)] for channel in args.channels]
    body = start_planned_sweep(
        base_url=args.base_url,
        token=args.token,
        device_id=args.device_id,
        frequencies_hz=frequencies,
        margin_hz=args.margin_hz,
        bin_width_hz=args.bin_width_hz,
        lna_gain_db=_lna_gain_db(args.lna_gain_db, 40),
        vga_gain_db=_vga_gain_db(args.vga_gain_db, 62),
        amp_enable=args.amp_enable,
        label="ble-adv-sweep",
    )
    sweep_id = str(body.get("sweep_id") or "")
    engine = str(body.get("engine") or "sweep")
    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    print(
        f"using sweep_id={sweep_id} engine={engine} device={args.device_id} "
        f"range={config.get('start_freq_hz')}-{config.get('stop_freq_hz')} bin_width={config.get('bin_width_hz')}",
        file=sys.stderr,
        flush=True,
    )
    return sweep_id, engine, config


def _run_sweep(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    if args.replace_existing:
        _stop_existing_streams_for_device(args, args.device_id)
        _stop_existing_sweeps_for_device(args, args.device_id)

    sweep_id, engine, _config = _start_ble_sweep(args)
    if args.csv and args.follow_decode:
        _print_csv_header()
    elif args.csv:
        print("seen_at,device_id,sweep_id,engine,channel,freq_hz,power_db", flush=True)

    printed = 0
    seen_keys: set[tuple[str, int]] = set()
    reporter = None if args.csv or args.events else BLETextReporter(args.summary_interval_s, args.top)
    try:
        while not stop_requested[0]:
            samples = sweep_samples(args.base_url, args.token, sweep_id)
            for sample in samples:
                timestamp = str(sample.get("timestamp") or "")
                for channel in args.channels:
                    freq_hz = BLE_ADV_CHANNELS[int(channel)]
                    key = (timestamp, int(channel))
                    if key in seen_keys:
                        continue
                    power = _sweep_channel_power(sample, freq_hz, max(int(args.bin_width_hz), 250_000))
                    if power is None:
                        continue
                    seen_keys.add(key)
                    if args.csv and args.follow_decode:
                        pass
                    elif args.csv:
                        print(
                            f"{timestamp},{args.device_id},{sweep_id},{engine},{int(channel)},{freq_hz},{power:.1f}",
                            flush=True,
                        )
                    else:
                        print(
                            f"ble_sweep device={args.device_id} engine={engine} ch={int(channel)} "
                            f"freq={freq_hz} power_db={power:.1f} sweep_id={sweep_id}",
                            flush=True,
                        )
                    printed += 1
                    if args.max_prints and printed >= args.max_prints:
                        stop_requested[0] = True
                        break
                if stop_requested[0]:
                    break
            if args.follow_decode and not stop_requested[0]:
                best_channel, best_power = _best_sweep_channel(samples, list(args.channels), args.bin_width_hz)
                if best_channel is not None and best_power is not None and best_power >= float(args.decode_threshold_db):
                    print(
                        f"sweep_follow_decode ch={best_channel} power_db={best_power:.1f} dwell_s={args.decode_dwell_s:.1f}",
                        file=sys.stderr,
                        flush=True,
                    )
                    stop_sweep(args.base_url, args.token, sweep_id)
                    decode_stop = [False]
                    old_hop = getattr(args, "hop", False)
                    old_dwell = getattr(args, "dwell_s", None)
                    args.hop = True
                    args.dwell_s = max(0.25, float(args.decode_dwell_s))
                    try:
                        _listen_one_channel(args, int(best_channel), decode_stop, reporter=reporter)
                    finally:
                        args.hop = old_hop
                        if old_dwell is not None:
                            args.dwell_s = old_dwell
                    if reporter is not None:
                        reporter.maybe_print_summary(force=True)
                    if stop_requested[0]:
                        break
                    sweep_id, engine, _config = _start_ble_sweep(args)
            time.sleep(max(0.1, float(args.interval_s)))
    finally:
        stop_sweep(args.base_url, args.token, sweep_id)
    return 0


def _run_iq_sweep(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    if args.replace_existing:
        _stop_existing_streams_for_device(args, args.device_id)
        _stop_existing_sweeps_for_device(args, args.device_id)

    center_freqs_hz = [] if args.start_freq_hz or args.stop_freq_hz or args.hop_hz else [BLE_ADV_CHANNELS[int(ch)] for ch in args.channels]
    body = start_iq_sweep(
        base_url=args.base_url,
        token=args.token,
        device_id=args.device_id,
        center_freqs_hz=center_freqs_hz,
        start_freq_hz=args.start_freq_hz,
        stop_freq_hz=args.stop_freq_hz,
        hop_hz=args.hop_hz,
        sample_rate_sps=args.sample_rate_sps,
        dwell_s=args.dwell_s,
        lna_gain_db=_lna_gain_db(args.lna_gain_db, 40),
        vga_gain_db=_vga_gain_db(args.vga_gain_db, 62),
        amp_enable=args.amp_enable,
        baseband_filter_hz=args.baseband_filter_hz,
    )
    iq_sweep_id = str(body.get("iq_sweep_id") or "")
    stream_id = str(body.get("stream_id") or "")
    print(
        f"using iq_sweep_id={iq_sweep_id} stream_id={stream_id} device={args.device_id} "
        f"sr={args.sample_rate_sps} dwell_s={args.dwell_s:.3f}",
        file=sys.stderr,
        flush=True,
    )
    if str(args.device_id).startswith("hackrf:") and stream_id != "native":
        print(
            "warning: gateway is using retune-based IQ sweep, not native hackrf_iq_sweep; "
            "run `make native` in sdr-gateway and restart the sdr-gateway service",
            file=sys.stderr,
            flush=True,
        )

    reporter = None if args.json or args.csv or args.events else BLETextReporter(args.summary_interval_s, args.top)
    detectors: dict[int, BLEAdvertisingDetector] = {}
    events_seen = 0
    chunks_seen = 0
    exit_code = 0
    try:
        while not stop_requested[0]:
            try:
                payload = iq_sweep_chunk(args.base_url, args.token, iq_sweep_id, args.chunk_bytes)
            except RuntimeError as exc:
                print(f"iq_sweep_error device={args.device_id} sweep_id={iq_sweep_id} error={exc}", file=sys.stderr, flush=True)
                exit_code = 1
                break
            raw_b64 = str(payload.get("iq_i8_b64") or "")
            if not raw_b64:
                time.sleep(0.05)
                continue
            center_freq_hz = int(payload.get("center_freq_hz") or 0)
            channel = _ble_channel_for_center(center_freq_hz, list(args.channels))
            if channel is None:
                if args.debug_bursts:
                    print(f"iq_sweep_skip center={center_freq_hz} reason=no_ble_channel", file=sys.stderr, flush=True)
                continue
            decode_center_freq_hz = int(BLE_ADV_CHANNELS[channel])
            raw = base64.b64decode(raw_b64.encode("ascii"), validate=False)
            chunks_seen += 1
            if args.debug_bursts and (chunks_seen <= 12 or chunks_seen % 50 == 0):
                print(
                    f"iq_sweep_chunk device={args.device_id} ch={channel} center={center_freq_hz} "
                    f"bytes={len(raw)} point={payload.get('point_index')} stream_id={stream_id}",
                    file=sys.stderr,
                    flush=True,
                )
            detector = detectors.get(channel)
            if detector is None:
                detector = BLEAdvertisingDetector(
                    sample_rate_sps=int(payload.get("sample_rate_sps") or args.sample_rate_sps),
                    center_freq_hz=decode_center_freq_hz,
                    channel=channel,
                )
                detectors[channel] = detector
            _, events = detector.process_iq_i8(raw)
            for event in events:
                if event.get("kind") != "ble_adv" and not args.debug_bursts:
                    continue
                event["device_id"] = args.device_id
                event["window"] = "iq-sweep"
                event["iq_sweep_id"] = iq_sweep_id
                event["point_index"] = payload.get("point_index")
                if event.get("kind") == "ble_adv":
                    if reporter is not None:
                        reporter.record(event)
                    else:
                        _print_event(event, args.json, args.csv)
                    events_seen += 1
                    if args.max_events and events_seen >= args.max_events:
                        stop_requested[0] = True
                        break
                else:
                    _print_event(event, args.json, args.csv)
            if reporter is not None:
                reporter.maybe_print_summary()
    finally:
        stop_iq_sweep(args.base_url, args.token, iq_sweep_id)
        if reporter is not None:
            reporter.maybe_print_summary(force=True)
    return exit_code


def _run_listen(args: argparse.Namespace) -> int:
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    if not args.hop:
        _listen_one_channel(args, args.channel, stop_requested)
        return 0

    channels = [37, 38, 39]
    index = 0
    while not stop_requested[0]:
        _listen_one_channel(args, channels[index % len(channels)], stop_requested)
        index += 1
    return 0


def _run_scan(args: argparse.Namespace) -> int:
    args.hop = True
    args.channel = 37
    stop_requested = [False]
    _install_stop_handlers(stop_requested)
    reporter = None if args.json or args.csv or args.events else BLETextReporter(args.summary_interval_s, args.top)
    channels = [37, 38, 39]
    index = 0
    try:
        while not stop_requested[0]:
            _listen_one_channel(args, channels[index % len(channels)], stop_requested, reporter=reporter)
            index += 1
    finally:
        if reporter is not None:
            reporter.maybe_print_summary(force=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "csv", False) and not getattr(args, "json", False) and args.command != "sweep":
        _print_csv_header()
    try:
        if args.command == "devices":
            return _run_devices(args)
        if args.command == "listen":
            return _run_listen(args)
        if args.command == "scan":
            return _run_scan(args)
        if args.command == "wideband-listen":
            return _run_wideband_listen(args)
        if args.command == "band-scan":
            return _run_band_scan(args)
        if args.command == "dual-hop":
            return _run_dual_hop(args)
        if args.command == "sweep":
            return _run_sweep(args)
        if args.command == "iq-sweep":
            return _run_iq_sweep(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        if "already in use" in str(exc):
            print("hint: add --replace-existing to stop stale streams for this device before starting", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
