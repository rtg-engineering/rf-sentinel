import os
import calendar
import contextlib
import copy
import csv
import io
import json
import logging
import platform
import queue
import re
import shutil
import signal
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import requests
import websocket
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest
from websocket._exceptions import WebSocketConnectionClosedException

try:
    from rich.text import Text
    from textual.app import App as TextualApp
    from textual.app import ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import DataTable, Footer, Header, RichLog, Static
except Exception:
    Text = None
    TextualApp = None
    ComposeResult = Any
    Horizontal = Vertical = None
    DataTable = Footer = Header = RichLog = Static = None


BLE_ADV_CHANNELS = {
    37: 2_402_000_000,
    38: 2_426_000_000,
    39: 2_480_000_000,
}

BT_CLASSIC_CHANNELS = {idx: 2_402_000_000 + (idx * 1_000_000) for idx in range(79)}
BT_CLASSIC_BANK_SIZE = 60
# Match the reference path more closely: 1 MHz-spaced Classic lanes decoded at 1 Msps after channelization.
BT_CLASSIC_CHANNEL_BW_HZ = 1_000_000
BT_CLASSIC_LANE_RATE_SPS = 1_000_000
BT_CLASSIC_LANE_SPACING_HZ = 1_000_000
BLE_ADV_CHANNEL_BW_HZ = 2_000_000
BLE_ADV_SAMPLE_RATE_SPS = 2_000_000
BLE_ADV_ACCESS_BYTES = bytes.fromhex("d6be898e")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / "config" / "env.txt")

DATA_DIR = Path(__file__).resolve().parent / "data"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


RF_SENTINEL_LOG_DIR = Path(os.getenv("RF_SENTINEL_LOG_DIR", "/var/log/rf_sentinel"))
RF_SENTINEL_CONTROL_PATH = RF_SENTINEL_LOG_DIR / "rf_sentinel_control.json"
RF_SENTINEL_UI_CONFIG_PATH = RF_SENTINEL_LOG_DIR / "rf_sentinel_ui_config.json"
RF_SENTINEL_RUNS_DIR = RF_SENTINEL_LOG_DIR / "runs"
RF_SENTINEL_ARCHIVE_DIR = RF_SENTINEL_LOG_DIR / "archives"
RF_SENTINEL_CSV_RETENTION_DAYS = max(1, int(os.getenv("RF_SENTINEL_CSV_RETENTION_DAYS", "7")))
RF_SENTINEL_CSV_ARCHIVE_MAX_MB = max(1, int(os.getenv("RF_SENTINEL_CSV_ARCHIVE_MAX_MB", "1000")))
RF_SENTINEL_DISCOVERY_TABLE_MAX_ROWS = max(500, int(os.getenv("RF_SENTINEL_DISCOVERY_TABLE_MAX_ROWS", "5000")))
RF_SENTINEL_BTC_NAME_LOOKUP = os.getenv("RF_SENTINEL_BTC_NAME_LOOKUP", "0").strip().lower() in {"1", "true", "yes", "on"}
RF_SENTINEL_NO_CHANGE = object()
RF_SENTINEL_PROTOCOLS = {"btc", "ble", "zigbee", "tpms", "walkie", "wifi", "fm", "lfmf", "cellular"}
RF_SENTINEL_DEMO_MODE = _env_flag("RF_SENTINEL_DEMO_MODE")
RF_SENTINEL_DEMO_LOOP = _env_flag("RF_SENTINEL_DEMO_LOOP", "1")
RF_SENTINEL_DEMO_TIME_SCALE = max(0.1, float(os.getenv("RF_SENTINEL_DEMO_TIME_SCALE", "1.0")))
RF_SENTINEL_DEMO_EVENT_FILE = Path(
    os.getenv("RF_SENTINEL_DEMO_EVENT_FILE", str(PROJECT_ROOT / ".demo" / "events" / "public-demo-events.jsonl"))
)
WIFI_SUPPORTED_CHANNELS = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
    36, 40, 44, 48, 52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
}
PROTOCOL_DEVICE_OVERRIDES = {"zigbee", "tpms", "walkie", "fm", "cellular"}
RF_SENTINEL_KEEP_BAD_FCS = os.getenv("RF_SENTINEL_KEEP_BAD_FCS", "0").strip().lower() in {"1", "true", "yes", "on"}
BLE_IDENTITY_CACHE_PATH = DATA_DIR / "ble_identities.json"
BTC_NAME_CACHE_PATH = DATA_DIR / "btc_names.json"
COMPANY_IDENTIFIERS_PATH = DATA_DIR / "company_identifiers.json"
UUID16_IDENTIFIERS_PATH = DATA_DIR / "uuid16_identifiers.json"
BTC_SNIFFER_ROOT = Path(os.getenv("BTC_SNIFFER_ROOT", str(PROJECT_ROOT / "rf_platform" / "plugins" / "bluetooth-classic")))
BTC_SNIFFER_BINARY = Path(os.getenv("BTC_SNIFFER_BINARY", str(BTC_SNIFFER_ROOT / "build" / "btcexplorer-sniffer")))
BTC_SNIFFER_LOG_PATH = Path(os.getenv("BTC_SNIFFER_LOG", str(RF_SENTINEL_LOG_DIR / "btcexplorer-sniffer.log")))
BTC_SNIFFER_AUTO_BUILD = os.getenv("BTC_SNIFFER_AUTO_BUILD", "1").strip().lower() not in {"0", "false", "no"}
BTC_ENGINE_DEFAULT = os.getenv("BTC_ENGINE", "btcsniffer").strip().lower()
SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS = float(os.getenv("SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS", "10"))
INVALID_CLK_INDEX = -1
DELTA_TS_SAME_THRESHOLD_US = 40
DELTA_TS_SLOT_THRESHOLD_US = 620
SLOT_DURATION_US = 625.0
SLOT_ERROR_THRESHOLD = 0.05
BT_CLASSIC_ACCESS_REPAIR_MAX_DISTANCE = 0
BT_CLASSIC_HEADER_MIN_PERFECT_TRIPLETS = 18
BT_CLASSIC_USE_CPP_FFT = os.getenv("BT_CLASSIC_USE_CPP_FFT", "1").strip().lower() not in {"0", "false", "no"}
btcsniffer_build_lock = threading.Lock()


def _design_lowpass_taps(sample_rate_hz: int, cutoff_hz: float, num_taps: int) -> np.ndarray:
    taps = max(15, int(num_taps) | 1)
    nyquist = max(1.0, float(sample_rate_hz) / 2.0)
    normalized_cutoff = min(0.98, max(0.001, float(cutoff_hz) / nyquist))
    n = np.arange(taps, dtype=np.float64) - ((taps - 1) / 2.0)
    kernel = normalized_cutoff * np.sinc(normalized_cutoff * n)
    kernel *= np.hamming(taps)
    kernel_sum = float(np.sum(kernel))
    if abs(kernel_sum) < 1e-12:
        return np.array([1.0], dtype=np.float32)
    kernel /= kernel_sum
    return kernel.astype(np.float32)


class FmAudioDemod:
    def __init__(self, in_rate: int, out_rate: int = 48_000, channel_cutoff_hz: float = 125_000.0) -> None:
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.decim = max(1, int(round(self.in_rate / 240_000.0)))
        self.demod_rate = self.in_rate / float(self.decim)
        self.prev = np.complex64(1.0 + 0j)
        self.channel_filter = self._design_lowpass(257, channel_cutoff_hz, float(self.in_rate))
        self._channel_tail = np.zeros(max(0, self.channel_filter.size - 1), dtype=np.complex64)
        self.mono_filter = self._design_lowpass(129, 15_000.0, float(self.demod_rate))
        self._mono_tail = np.zeros(max(0, self.mono_filter.size - 1), dtype=np.float32)
        self._audio_scale = 1.0
        self.resample_pos = 0.0
        self._leftover = b""

    def process_iq_i8(self, raw: bytes) -> bytes:
        if not raw:
            return b""
        if self._leftover:
            raw = self._leftover + raw
            self._leftover = b""
        if len(raw) % 2 != 0:
            self._leftover = raw[-1:]
            raw = raw[:-1]
        if len(raw) < 4:
            return b""
        iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
        z = (iq[0::2] / 128.0 + 1j * (iq[1::2] / 128.0)).astype(np.complex64)
        if z.size < 8:
            return b""
        z = self._channel_filter_and_decimate(z)
        if z.size < 8:
            return b""
        prev = np.empty_like(z)
        prev[0] = self.prev
        prev[1:] = z[:-1]
        self.prev = z[-1]
        demod = np.angle(z * np.conj(prev)).astype(np.float32)
        if demod.size < 8:
            return b""
        demod -= float(np.mean(demod))
        # Match AetherCast's forgiving mono fallback: it is much harder to upset
        # on marginal RF than the stricter audio low-pass path.
        kernel = np.array([0.2, 0.2, 0.2, 0.2, 0.2], dtype=np.float32)
        mono = np.convolve(demod, kernel, mode="same").astype(np.float32)
        if mono.size < 4:
            return b""
        step = self.demod_rate / float(self.out_rate)
        positions = np.arange(self.resample_pos, mono.size - 1, step, dtype=np.float64)
        if positions.size == 0:
            self.resample_pos = float(self.resample_pos + mono.size)
            return b""
        next_pos = float(positions[-1] + step - (mono.size - 1))
        idx = np.floor(positions).astype(np.int32)
        valid = idx + 1 < mono.size
        idx = idx[valid]
        positions = positions[valid]
        if positions.size == 0:
            self.resample_pos = max(0.0, next_pos)
            return b""
        frac = positions - idx
        audio = mono[idx] * (1.0 - frac) + mono[idx + 1] * frac
        self.resample_pos = max(0.0, next_pos)
        peak = float(np.max(np.abs(audio))) if audio.size else 1.0
        target_scale = 0.85 / max(peak, 0.2)
        self._audio_scale = (self._audio_scale * 0.9) + (target_scale * 0.1)
        audio = np.clip(audio * self._audio_scale, -1.0, 1.0)
        mono_i16 = (audio * 32767.0).astype(np.int16)
        return mono_i16.tobytes()

    @staticmethod
    def _design_lowpass(num_taps: int, cutoff_hz: float, sample_rate_hz: float) -> np.ndarray:
        cutoff = min(float(cutoff_hz), (float(sample_rate_hz) / 2.0) * 0.92)
        n = np.arange(int(num_taps), dtype=np.float32) - ((int(num_taps) - 1) / 2.0)
        taps = 2.0 * cutoff / float(sample_rate_hz) * np.sinc(2.0 * cutoff / float(sample_rate_hz) * n)
        taps *= np.hamming(int(num_taps)).astype(np.float32)
        taps /= max(1e-12, float(np.sum(taps)))
        return taps.astype(np.float32)

    def _filter_float(self, x: np.ndarray, taps: np.ndarray, tail_name: str) -> np.ndarray:
        tail = getattr(self, tail_name)
        x = x.astype(np.float32, copy=False)
        x_ext = np.concatenate((tail, x))
        filtered = np.convolve(x_ext, taps, mode="valid").astype(np.float32)
        setattr(self, tail_name, x_ext[-tail.size :].astype(np.float32) if tail.size else tail)
        return filtered

    def _channel_filter_and_decimate(self, z: np.ndarray) -> np.ndarray:
        z_ext = np.concatenate((self._channel_tail, z))
        filtered = np.convolve(z_ext, self.channel_filter, mode="valid").astype(np.complex64)
        if self._channel_tail.size:
            self._channel_tail = z_ext[-self._channel_tail.size :].astype(np.complex64)
        decim = int(self.decim)
        if decim <= 1:
            return filtered
        usable = (filtered.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return filtered[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)


def _gateway_base() -> str:
    return os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def _gateway_token() -> str:
    token = (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()
    if token:
        return token
    return ""


def _gateway_headers() -> dict[str, str]:
    token = _gateway_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _configured_btc_target(mac_override: str | None = None) -> dict[str, Any] | None:
    mac = str(mac_override or "").strip()
    if not mac:
        mac = (os.getenv("BTC_TARGET_MAC", "") or "").strip()
    if not mac:
        return None
    try:
        target = _classic_target_from_mac(mac)
    except ValueError:
        return None
    target["inquiry_status"] = "manual traffic generation"
    target["source"] = "scan form BTC target MAC" if mac_override else "env BTC_TARGET_MAC"
    return target


def _ws_url_for_stream(stream_id: str) -> str:
    base = _gateway_base()
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    else:
        ws_base = "ws://" + base[len("http://") :]
    return f"{ws_base}/ws/iq/{stream_id}?keep=1"


@dataclass
class ExplorerState:
    running: bool = False
    mode: str = "ble"
    stream_id: str | None = None
    stream_ids: dict[str, str] = field(default_factory=dict)
    device_id: str | None = None
    device_ids: dict[str, str] = field(default_factory=dict)
    center_freq_hz: int = BLE_ADV_CHANNELS[37]
    sample_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS
    lna_gain_db: int = 24
    vga_gain_db: int = 28
    channel: int = 37
    channels_by_mode: dict[str, int] = field(default_factory=dict)
    worker_alive: bool = False
    worker_alive_by_mode: dict[str, bool] = field(default_factory=dict)
    worker_error: str = ""
    worker_errors: dict[str, str] = field(default_factory=dict)
    gateway_start_response: dict[str, Any] | None = None
    chunks_seen: int = 0
    bytes_seen: int = 0
    last_rssi_dbfs: float = -120.0
    rssi_by_mode: dict[str, float] = field(default_factory=dict)
    chunks_by_mode: dict[str, int] = field(default_factory=dict)
    bytes_by_mode: dict[str, int] = field(default_factory=dict)
    noise_floor_dbfs: float = -120.0
    bursts_seen: int = 0
    ble_packets_seen: int = 0
    classic_bursts_seen: int = 0
    detections: list[dict[str, Any]] = field(default_factory=list)
    classic_candidates: list[dict[str, Any]] = field(default_factory=list)
    classic_addresses: list[dict[str, Any]] = field(default_factory=list)
    discovery_table: list[dict[str, Any]] = field(default_factory=list)
    channel_activity: dict[int, dict[str, Any]] = field(default_factory=dict)
    page_activity: dict[str, dict[str, Any]] = field(default_factory=dict)
    decoder_stats: dict[str, Any] = field(default_factory=dict)
    test_target: dict[str, Any] | None = None
    test_target_error: str = ""
    btc_engine: str = ""
    btc_engine_command: list[str] = field(default_factory=list)
    btc_engine_log: str = ""
    scanner_log: list[str] = field(default_factory=list)
    scanner_assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    csv_run_id: str = ""
    csv_log_dir: str = ""


@dataclass
class FmPlaybackState:
    running: bool = False
    pending: bool = False
    pending_freq_mhz: float = 0.0
    pending_device_id: str = ""
    device_id: str = ""
    freq_mhz: float = 0.0
    sample_rate_sps: int = 2_000_000
    lna_gain_db: int = 32
    vga_gain_db: int = 32
    stream_id: str = ""
    worker_alive: bool = False
    worker_error: str = ""
    last_audio_rms: float = 0.0
    produced_chunks: int = 0
    served_chunks: int = 0
    empty_audio_polls: int = 0
    scanner_protocol_paused: bool = False


@dataclass
class WalkiePlaybackState:
    running: bool = False
    pending: bool = False
    pending_freq_mhz: float = 0.0
    pending_device_id: str = ""
    device_id: str = ""
    freq_mhz: float = 462.5
    sample_rate_sps: int = 1_000_000
    lna_gain_db: int = 16
    vga_gain_db: int = 20
    stream_id: str = ""
    worker_alive: bool = False
    worker_error: str = ""
    last_audio_rms: float = 0.0
    produced_chunks: int = 0
    served_chunks: int = 0
    recent_chunks: int = 0
    recent_started_at: float = 0.0
    recent_updated_at: float = 0.0
    scanner_protocol_paused: bool = False


@dataclass
class LapState:
    lap: int
    status: str = "new"
    ts_us: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    processed_packets: int = 0
    cannot_init: int = 0
    broken_packets: int = 0


class BluetoothDetector:
    def __init__(self, sample_rate_sps: int, mode: str, center_freq_hz: int, channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.mode = mode
        self.center_freq_hz = int(center_freq_hz)
        self.channel = int(channel)
        self._prev = np.complex64(1.0 + 0j)
        self._bit_tail: list[int] = []
        self._classic_bit_tails: dict[int, list[int]] = {}
        self._classic_bits_processed = 0
        self._lap_map: dict[int, LapState] = {}
        self._seen_packet_keys: dict[str, float] = {}
        self._burst_holdoff = 0
        self.stats = {
            "preamble_hits": 0,
            "barker_hits": 0,
            "access_code_mismatch": 0,
            "access_code_hits": 0,
            "access_code_repair_hits": 0,
            "target_access_near_hits": 0,
            "target_access_best_distance": 68,
            "lap_hits": 0,
            "header_failures": 0,
            "header_relaxed_hits": 0,
            "uap_candidate_hits": 0,
        }

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = self._iq_bytes_to_complex(raw)
        if z.size < 64:
            return -120.0, [], []
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        power = np.abs(z) ** 2
        rssi = float(10.0 * np.log10(float(np.mean(power)) + 1e-12))
        threshold = max(float(np.median(power) * 6.5), float(np.mean(power) * 2.2))
        burst_spans = self._find_bursts(power, threshold)

        if self.mode == "classic":
            return self._classic_events(z, rssi, burst_spans)
        return rssi, self._ble_events(z, rssi, burst_spans), []

    def _iq_bytes_to_complex(self, raw: bytes) -> np.ndarray:
        if len(raw) < 4:
            return np.empty(0, dtype=np.complex64)
        if len(raw) % 2:
            raw = raw[:-1]
        iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def _find_bursts(self, power: np.ndarray, threshold: float) -> list[tuple[int, int, float]]:
        active = power > threshold
        if not np.any(active):
            self._burst_holdoff = max(0, self._burst_holdoff - power.size)
            return []
        idx = np.flatnonzero(active)
        splits = np.where(np.diff(idx) > max(8, int(self.sample_rate_sps * 0.000012)))[0] + 1
        groups = np.split(idx, splits)
        spans: list[tuple[int, int, float]] = []
        min_len = max(20, int(self.sample_rate_sps * 0.000018))
        for group in groups:
            if group.size < min_len:
                continue
            start = int(group[0])
            stop = int(group[-1])
            if self._burst_holdoff > start:
                continue
            peak = float(10.0 * np.log10(float(np.max(power[start : stop + 1])) + 1e-12))
            spans.append((start, stop, peak))
            self._burst_holdoff = stop + int(self.sample_rate_sps * 0.000050)
        self._burst_holdoff = max(0, self._burst_holdoff - power.size)
        return spans[:12]

    def _ble_events(
        self,
        z: np.ndarray,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        bits = self._gfsk_bits(z)
        if not bits:
            return self._burst_only_events("ble_burst", rssi_dbfs, burst_spans)

        search_bits = (self._bit_tail + bits)[-8192:]
        self._bit_tail = search_bits[-96:]
        for polarity in (0, 1):
            normalized = [bit ^ polarity for bit in search_bits]
            packet_events = self._extract_ble_adv_packets(normalized, rssi_dbfs)
            events.extend(packet_events)
        if not events:
            events.extend(self._burst_only_events("ble_burst", rssi_dbfs, burst_spans))
        return events[:16]

    def _gfsk_bits(self, z: np.ndarray) -> list[int]:
        freq = self._gfsk_discriminator(z)
        sps = max(1, int(round(self.sample_rate_sps / 1_000_000.0)))
        usable = (freq.size // sps) * sps
        if usable <= 0:
            return []
        symbols = freq[:usable].reshape(-1, sps).mean(axis=1)
        return [1 if value > 0 else 0 for value in symbols.tolist()]

    def _gfsk_discriminator(self, z: np.ndarray) -> np.ndarray:
        prev = np.empty_like(z)
        prev[0] = self._prev
        prev[1:] = z[:-1]
        self._prev = z[-1]
        # Same sign discriminator idea as the research code, but vectorized.
        cross = (prev.real * z.imag) - (prev.imag * z.real)
        freq = cross.astype(np.float32)
        freq -= float(np.median(freq))
        return freq

    def _classic_bit_phase_streams(self, z: np.ndarray) -> list[tuple[int, list[int]]]:
        freq = self._gfsk_discriminator(z)
        sps = max(1, int(round(self.sample_rate_sps / 1_000_000.0)))
        streams: list[tuple[int, list[int]]] = []
        for phase in range(sps):
            symbols = freq[phase::sps]
            if symbols.size:
                streams.append((phase, [1 if value > 0 else 0 for value in symbols.tolist()]))
        return streams

    def _extract_ble_adv_packets(self, bits: list[int], rssi_dbfs: float) -> list[dict[str, Any]]:
        access_bits = self._bytes_to_lsb_bits(BLE_ADV_ACCESS_BYTES)
        out: list[dict[str, Any]] = []
        for pos in self._find_bit_pattern(bits, access_bits, max_errors=1):
            start = pos + len(access_bits)
            dewhitened = self._ble_dewhiten_bits(bits[start : start + (2 + 37 + 3) * 8], self.channel)
            if len(dewhitened) < 16:
                continue
            header = self._bits_to_bytes(dewhitened[:16])
            if len(header) != 2:
                continue
            pdu_type = header[0] & 0x0F
            if pdu_type > 6:
                continue
            tx_add = bool(header[0] & 0x40)
            length = header[1] & 0x3F
            if length < 6 or length > 37:
                continue
            packet_bit_len = (2 + length + 3) * 8
            dewhitened = self._ble_dewhiten_bits(bits[start : start + packet_bit_len], self.channel)
            if len(dewhitened) < packet_bit_len:
                continue
            pdu_bits = dewhitened[: (2 + length) * 8]
            crc_bits_rx = dewhitened[(2 + length) * 8 : packet_bit_len]
            if self._ble_crc24_bits(pdu_bits) != crc_bits_rx:
                continue
            packet = self._bits_to_bytes(pdu_bits)
            if len(packet) < 2 + length:
                continue
            body = packet[2 : 2 + length]
            advertiser = self._format_ble_addr(body[:6])
            ad_data = body[6:] if len(body) > 6 else b""
            ad_fields = self._ble_ad_fields(ad_data)
            local_name = self._ble_local_name_from_fields(ad_fields)
            uuid16 = self._ble_uuid16s_from_fields(ad_fields)
            manufacturer = self._ble_manufacturer_from_fields(ad_fields)
            appearance = self._ble_appearance_from_fields(ad_fields)
            key = f"own-crc:{self.channel}:{pdu_type}:{advertiser}:{packet.hex()}"
            now = time.time()
            if now - self._seen_packet_keys.get(key, 0.0) < 1.0:
                continue
            self._seen_packet_keys[key] = now
            out.append(
                {
                    "kind": "ble_adv",
                    "seen_at": now,
                    "channel": self.channel,
                    "center_freq_hz": self.center_freq_hz,
                    "rssi_dbfs": round(rssi_dbfs, 1),
                    "pdu_type": self._ble_pdu_name(pdu_type),
                    "pdu_type_id": pdu_type,
                    "address": advertiser or "unknown",
                    "address_type": "random" if tx_add else "public",
                    "name": local_name,
                    "uuid16": uuid16,
                    "manufacturer": manufacturer,
                    "appearance": appearance,
                    "payload_len": length,
                    "confidence": 0.94,
                    "decoder": "own-crc",
                }
            )
        return out

    @staticmethod
    def _ble_dewhiten_bits(bits: list[int], channel: int) -> list[int]:
        lfsr = [
            1,
            (channel >> 5) & 1,
            (channel >> 4) & 1,
            (channel >> 3) & 1,
            (channel >> 2) & 1,
            (channel >> 1) & 1,
            channel & 1,
        ]
        out: list[int] = []
        for raw_bit in bits:
            out.append((raw_bit & 1) ^ lfsr[6])
            lfsr = [
                lfsr[6],
                lfsr[0],
                lfsr[1],
                lfsr[2],
                lfsr[3] ^ lfsr[6],
                lfsr[4],
                lfsr[5],
            ]
        return out

    @staticmethod
    def _ble_crc24_bits(bits: list[int]) -> list[int]:
        state = [1, 0] * 12
        for bit in bits:
            new_bit = state[23] ^ (bit & 1)
            state = [
                new_bit,
                state[0] ^ new_bit,
                state[1],
                state[2] ^ new_bit,
                state[3] ^ new_bit,
                state[4],
                state[5] ^ new_bit,
                state[6],
                state[7],
                state[8] ^ new_bit,
                state[9] ^ new_bit,
                state[10],
                state[11],
                state[12],
                state[13],
                state[14],
                state[15],
                state[16],
                state[17],
                state[18],
                state[19],
                state[20],
                state[21],
                state[22],
            ]
        return list(reversed(state))

    @staticmethod
    def _ble_ad_fields(ad_data: bytes) -> list[tuple[int, bytes]]:
        idx = 0
        fields: list[tuple[int, bytes]] = []
        while idx < len(ad_data):
            field_len = ad_data[idx]
            if field_len == 0:
                break
            field_end = idx + 1 + field_len
            if field_end > len(ad_data):
                break
            ad_type = ad_data[idx + 1]
            value = ad_data[idx + 2 : field_end]
            fields.append((ad_type, value))
            idx = field_end
        return fields

    @classmethod
    def _ble_local_name(cls, ad_data: bytes) -> str:
        return cls._ble_local_name_from_fields(cls._ble_ad_fields(ad_data))

    @staticmethod
    def _ble_local_name_from_fields(fields: list[tuple[int, bytes]]) -> str:
        best = ""
        for ad_type, value in fields:
            if ad_type in {0x08, 0x09} and value:
                try:
                    name = value.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
                except Exception:
                    name = ""
                if name:
                    best = name
                    if ad_type == 0x09:
                        return best
        return best

    @staticmethod
    def _ble_uuid16s_from_fields(fields: list[tuple[int, bytes]]) -> list[str]:
        uuids: list[str] = []
        for ad_type, value in fields:
            if ad_type in {0x02, 0x03, 0x14}:
                for idx in range(0, len(value) - 1, 2):
                    uuids.append(f"0x{int.from_bytes(value[idx : idx + 2], 'little'):04X}")
                continue
            if ad_type == 0x16 and len(value) >= 2:
                uuids.append(f"0x{int.from_bytes(value[:2], 'little'):04X}")
        return uuids

    @staticmethod
    def _ble_manufacturer_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0xFF or len(value) < 2:
                continue
            company_id = int.from_bytes(value[:2], "little")
            company_hex = f"0x{company_id:04X}"
            return {
                "company_id": company_hex,
                "company_name": _company_name(company_hex),
                "data": value[2:].hex().upper(),
            }
        return None

    @staticmethod
    def _ble_appearance_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0x19 or len(value) < 2:
                continue
            code = int.from_bytes(value[:2], "little")
            return {
                "code": f"0x{code:04X}",
                "label": BLE_APPEARANCE_LABELS.get(code, f"Appearance {code:#06x}"),
            }
        return None

    def _classic_events(
        self,
        z: np.ndarray,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        phase_streams = self._classic_bit_phase_streams(z)
        for phase, bits in phase_streams:
            tail = self._classic_bit_tails.get(phase, [])
            base_bit_index = self._classic_bits_processed - len(tail)
            search_bits = tail + bits
            for polarity in (0, 1):
                normalized = [bit ^ polarity for bit in search_bits]
                for observation in self._extract_classic_observations(normalized, base_bit_index, rssi_dbfs):
                    event, lap_candidates = self._update_lap_state(observation)
                    event["phase"] = phase
                    events.append(event)
                    candidates.extend(lap_candidates)
            self._classic_bit_tails[phase] = search_bits[-192:]

        self._classic_bits_processed += max((len(bits) for _, bits in phase_streams), default=0)
        if events:
            return rssi_dbfs, events[:16], candidates[:24]

        for start, stop, peak in burst_spans:
            duration_us = (stop - start + 1) * 1_000_000.0 / float(self.sample_rate_sps)
            events.append(
                {
                    "kind": "classic_burst",
                    "seen_at": time.time(),
                    "channel": self.channel,
                    "center_freq_hz": self.center_freq_hz,
                    "rssi_dbfs": round(rssi_dbfs, 1),
                    "peak_dbfs": round(peak, 1),
                    "duration_us": round(duration_us, 1),
                    "uap": None,
                    "confidence": 0.35,
                }
            )
        return rssi_dbfs, events, []

    def process_classic_cpp_bits(self, bits: list[int], rssi_dbfs: float) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        tail = self._classic_bit_tails.get(0, [])
        base_bit_index = self._classic_bits_processed - len(tail)
        search_bits = tail + bits
        min_packet_bits = 72 + 54
        pos = 0
        stop = max(0, len(search_bits) - min_packet_bits)
        while pos < stop:
            if not self._classic_preamble_ok(search_bits, pos):
                pos += 1
                continue
            self.stats["preamble_hits"] += 1
            if self._classic_barker(search_bits, pos) is not None:
                self.stats["barker_hits"] += 1
            access = self._classic_access_code(search_bits, pos)
            if access is None:
                self._classic_target_access_diagnostic(search_bits, pos)
                pos += 1
                continue
            self.stats["access_code_hits"] += 1
            header_result = self._classic_bruteforce_all_uaps(search_bits[pos + 72 : pos + 72 + 54])
            if header_result["valid_uaps"] == 0:
                self.stats["header_failures"] += 1
                pos += 1
                continue
            self.stats["lap_hits"] += 1
            self.stats["uap_candidate_hits"] += int(header_result["valid_uaps"])
            observation = {
                "lap": access["lap"],
                "access_word": access["access_word"],
                "observed_access_word": access.get("observed_access_word", access["access_word"]),
                "repair_distance": int(access.get("repair_distance", 0)),
                "repaired": bool(access.get("repaired", False)),
                "header": header_result["header"],
                "header_perfect_triplets": int(header_result.get("perfect_triplets", 0)),
                "header_relaxed": bool(header_result.get("relaxed", False)),
                "uap_results": header_result["uap_results"],
                "valid_uaps": header_result["valid_uaps"],
                "ts_us": int(base_bit_index + pos),
                "rssi_dbfs": round(rssi_dbfs, 1),
            }
            event, lap_candidates = self._update_lap_state(observation)
            event["phase"] = 0
            event["demod"] = "cpp-cross"
            events.append(event)
            candidates.extend(lap_candidates)
            pos += 100
            if len(events) >= 8:
                break
        self._classic_bit_tails[0] = search_bits[-192:]
        self._classic_bits_processed += len(bits)
        return rssi_dbfs, events, candidates

    def _extract_classic_observations(
        self,
        bits: list[int],
        base_bit_index: int,
        rssi_dbfs: float,
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        min_packet_bits = 72 + 54
        for pos in range(0, max(0, len(bits) - min_packet_bits)):
            if not self._classic_preamble_ok(bits, pos):
                continue
            self.stats["preamble_hits"] += 1
            if self._classic_barker(bits, pos) is not None:
                self.stats["barker_hits"] += 1
            access = self._classic_access_code(bits, pos)
            if access is None:
                self._classic_target_access_diagnostic(bits, pos)
                continue
            self.stats["access_code_hits"] += 1
            header_result = self._classic_bruteforce_all_uaps(bits[pos + 72 : pos + 72 + 54])
            if header_result["valid_uaps"] == 0:
                self.stats["header_failures"] += 1
                continue
            self.stats["lap_hits"] += 1
            self.stats["uap_candidate_hits"] += int(header_result["valid_uaps"])
            observations.append(
                {
                    "lap": access["lap"],
                    "access_word": access["access_word"],
                    "observed_access_word": access.get("observed_access_word", access["access_word"]),
                    "repair_distance": int(access.get("repair_distance", 0)),
                    "repaired": bool(access.get("repaired", False)),
                    "header": header_result["header"],
                    "header_perfect_triplets": int(header_result.get("perfect_triplets", 0)),
                    "header_relaxed": bool(header_result.get("relaxed", False)),
                    "uap_results": header_result["uap_results"],
            "valid_uaps": header_result["valid_uaps"],
            "ts_us": int(base_bit_index + pos),
            "rssi_dbfs": round(rssi_dbfs, 1),
        }
    )
            if len(observations) >= 8:
                break
        return observations

    def _classic_preamble_ok(self, bits: list[int], pos: int) -> bool:
        if pos + 68 >= len(bits):
            return False
        even = bits[pos] + bits[pos + 2]
        odd = bits[pos + 1] + bits[pos + 3]
        return (even == 2 and odd == 0) or (even == 0 and odd == 2)

    def _classic_barker(self, bits: list[int], pos: int) -> int | None:
        barker = self._extract_lsb_byte(bits, pos + 62) & 0x3F
        return barker if barker in {0x13, 0x2C} else None

    def _classic_access_code(self, bits: list[int], pos: int) -> dict[str, int] | None:
        barker = self._classic_barker(bits, pos)
        if barker is None:
            return None
        lap = (
            (self._extract_lsb_byte(bits, pos + 54) << 16)
            | (self._extract_lsb_byte(bits, pos + 46) << 8)
            | self._extract_lsb_byte(bits, pos + 38)
        )
        code = (
            (self._extract_lsb_byte(bits, pos + 4) << 0)
            | (self._extract_lsb_byte(bits, pos + 12) << 8)
            | (self._extract_lsb_byte(bits, pos + 20) << 16)
            | (self._extract_lsb_byte(bits, pos + 28) << 24)
            | (self._extract_lsb_byte(bits, pos + 36) << 32)
        ) & 0x3FFFFFFFF
        access_word = (barker << 58) | (lap << 34) | code

        barker_true = 0x13 if (lap & 0x800000) else 0x2C
        x = (barker_true << 24) | lap
        p = 0x83848D96BBCC54FC
        xtilde = (p >> 34) ^ x
        gp = int("157464165547", 8)
        g = (gp << 1) ^ gp
        ctilde = self._compute_remainder(xtilde, g)
        expected = (ctilde | (xtilde << 34)) ^ p
        if access_word != expected:
            self.stats["access_code_mismatch"] += 1
            distance = (access_word ^ expected).bit_count()
            if distance > BT_CLASSIC_ACCESS_REPAIR_MAX_DISTANCE:
                return None
            self.stats["access_code_repair_hits"] += 1
            return {
                "lap": lap,
                "access_word": expected,
                "observed_access_word": access_word,
                "repair_distance": distance,
                "repaired": True,
            }
        return {"lap": lap, "access_word": access_word, "repair_distance": 0, "repaired": False}

    def _classic_target_access_diagnostic(self, bits: list[int], pos: int) -> None:
        with state_lock:
            target = dict(state.test_target or {})
        if target.get("protocol") != "BTC":
            return
        try:
            lap = int(str(target.get("lap") or ""), 16)
        except ValueError:
            return
        expected = self._classic_expected_access_word(lap)
        observed = self._classic_observed_access_word(bits, pos)
        if observed is None:
            return
        distance = (observed ^ expected).bit_count()
        self.stats["target_access_best_distance"] = min(int(self.stats.get("target_access_best_distance", 68)), int(distance))
        if distance <= 8:
            self.stats["target_access_near_hits"] += 1

    def _classic_observed_access_word(self, bits: list[int], pos: int) -> int | None:
        if pos + 72 > len(bits):
            return None
        barker = self._classic_barker(bits, pos)
        if barker is None:
            return None
        lap = (
            (self._extract_lsb_byte(bits, pos + 54) << 16)
            | (self._extract_lsb_byte(bits, pos + 46) << 8)
            | self._extract_lsb_byte(bits, pos + 38)
        )
        code = (
            (self._extract_lsb_byte(bits, pos + 4) << 0)
            | (self._extract_lsb_byte(bits, pos + 12) << 8)
            | (self._extract_lsb_byte(bits, pos + 20) << 16)
            | (self._extract_lsb_byte(bits, pos + 28) << 24)
            | (self._extract_lsb_byte(bits, pos + 36) << 32)
        ) & 0x3FFFFFFFF
        return (barker << 58) | (lap << 34) | code

    @classmethod
    def _classic_expected_access_word(cls, lap: int) -> int:
        barker_true = 0x13 if (lap & 0x800000) else 0x2C
        x = (barker_true << 24) | lap
        p = 0x83848D96BBCC54FC
        xtilde = (p >> 34) ^ x
        gp = int("157464165547", 8)
        g = (gp << 1) ^ gp
        ctilde = cls._compute_remainder(xtilde, g)
        return (ctilde | (xtilde << 34)) ^ p

    def _classic_bruteforce_all_uaps(self, header_bits: list[int]) -> dict[str, Any]:
        header = 0
        perfect_rx = 0
        for idx in range(0, 54, 3):
            triple = header_bits[idx : idx + 3]
            s1 = sum(triple)
            s0 = 3 - s1
            header >>= 1
            if s1 == 0 or s0 == 0:
                perfect_rx += 1
            if s1 > s0:
                header |= 0x20000
        if perfect_rx < BT_CLASSIC_HEADER_MIN_PERFECT_TRIPLETS:
            return {"header": header, "valid_uaps": 0, "uap_results": [], "perfect_triplets": perfect_rx, "relaxed": False}
        relaxed = perfect_rx != 18
        if relaxed:
            self.stats["header_relaxed_hits"] += 1

        results: list[dict[str, Any]] = []
        for uap in range(256):
            clks = self._classic_header_clks_for_uap(header, uap)
            if clks:
                results.append({"uap": uap, "clks": clks})
        return {
            "header": header,
            "valid_uaps": len(results),
            "uap_results": results,
            "perfect_triplets": perfect_rx,
            "relaxed": relaxed,
        }

    def _classic_header_clks_for_uap(self, header: int, uap: int) -> list[int]:
        found: list[int] = []
        for clk in range(64):
            header_dewhiten = header
            whitener = (clk & 0x3F) | 0x40
            for bit_idx in range(18):
                whitener_out = (whitener >> 6) & 0x1
                whitener_shifted = (whitener << 1) & 0x7F
                whitener = whitener_shifted ^ (whitener_out | (whitener_out << 4))
                header_dewhiten ^= whitener_out << bit_idx

            lfsr = uap
            for bit_idx in range(10):
                lfsr_out = (lfsr >> 7) & 0x1
                data_in = (header_dewhiten >> bit_idx) & 0x1
                lfsr_in = lfsr_out ^ data_in
                lfsr_adder = (
                    (lfsr_in << 7)
                    | (lfsr_in << 5)
                    | (lfsr_in << 2)
                    | (lfsr_in << 1)
                    | (lfsr_in << 0)
                )
                lfsr = ((lfsr << 1) & 0xFF) ^ lfsr_adder

            for bit_idx in range(8):
                bit_rx = (header_dewhiten >> (10 + bit_idx)) & 0x1
                bit_tx = (lfsr >> (7 - bit_idx)) & 0x1
                if bit_rx != bit_tx:
                    break
            else:
                found.append(clk)
        return found

    def _update_lap_state(self, observation: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lap = int(observation["lap"])
        node = self._lap_map.get(lap)
        if node is None:
            node = LapState(lap=lap)
            self._lap_map[lap] = node
        node.processed_packets += 1
        event_status = node.status

        if node.status == "new":
            init_candidates = [
                {"uap": item["uap"], "clks": item["clks"], "clk_index": INVALID_CLK_INDEX, "valid": True}
                for item in observation["uap_results"]
                if len(item["clks"]) == 2
            ]
            node.ts_us = int(observation["ts_us"])
            if len(init_candidates) == 32:
                node.candidates = init_candidates
                node.status = "brute_forcing"
                event_status = "initialized"
            else:
                node.cannot_init += 1
                event_status = f"init_found_{len(init_candidates)}"
        elif node.status == "brute_forcing":
            self._prune_lap_candidates(node, observation)
            event_status = node.status

        candidates = [self._candidate_payload(node, item, observation) for item in node.candidates if item.get("valid")]
        event = {
            "kind": "classic_lap",
            "seen_at": time.time(),
            "channel": self.channel,
            "center_freq_hz": self.center_freq_hz,
            "rssi_dbfs": observation["rssi_dbfs"],
            "lap": f"{lap:06X}",
            "access_word": f"{int(observation['access_word']):018X}",
            "observed_access_word": f"{int(observation.get('observed_access_word', observation['access_word'])):018X}",
            "repaired": bool(observation.get("repaired", False)),
            "repair_distance": int(observation.get("repair_distance", 0)),
            "header_perfect_triplets": int(observation.get("header_perfect_triplets", 0)),
            "header_relaxed": bool(observation.get("header_relaxed", False)),
            "ts_us": int(observation.get("ts_us", 0)),
            "candidate_count": len(candidates),
            "processed_packets": node.processed_packets,
            "broken_packets": node.broken_packets,
            "cannot_init": node.cannot_init,
            "uap": f"{candidates[0]['uap']:02X}" if len(candidates) == 1 else None,
            "status": event_status,
            "confidence": 0.92 if len(candidates) == 1 else 0.68 if candidates else 0.42,
        }
        return event, candidates

    def _prune_lap_candidates(self, node: LapState, observation: dict[str, Any]) -> None:
        delta_us = int(observation["ts_us"]) - int(node.ts_us)
        if delta_us < 0 or abs(delta_us) < DELTA_TS_SAME_THRESHOLD_US:
            return
        if abs(delta_us) < DELTA_TS_SLOT_THRESHOLD_US:
            self._lap_map.pop(node.lap, None)
            return
        periods = float(delta_us) / SLOT_DURATION_US
        periods_round = round(periods)
        if abs(periods - periods_round) > SLOT_ERROR_THRESHOLD:
            self._lap_map.pop(node.lap, None)
            return

        result_by_uap = {item["uap"]: item["clks"] for item in observation["uap_results"] if len(item["clks"]) == 2}
        valid_count = 0
        broken_count = 0
        slot = int(periods_round) % 64
        for candidate in node.candidates:
            if not candidate.get("valid"):
                continue
            new_clks = result_by_uap.get(candidate["uap"])
            if not new_clks:
                candidate["valid"] = False
                broken_count += 1
                continue
            old_clks = candidate["clks"]
            if new_clks == old_clks:
                valid_count += 1
                continue
            old_to_check = [old_clks[candidate["clk_index"]]] if candidate["clk_index"] != INVALID_CLK_INDEX else old_clks
            matches: list[int] = []
            for old_clk in old_to_check:
                for new_idx, new_clk in enumerate(new_clks):
                    if ((new_clk - old_clk) % 64) == slot:
                        matches.append(new_idx)
            if len(matches) > 1:
                candidate["clks"] = new_clks
                candidate["clk_index"] = INVALID_CLK_INDEX
                valid_count += 1
            elif len(matches) == 1:
                candidate["clks"] = new_clks
                candidate["clk_index"] = matches[0]
                valid_count += 1
            else:
                candidate["valid"] = False

        if valid_count == 0 and broken_count > 0:
            node.broken_packets += 1
            return
        if valid_count <= 2:
            node.status = "resolved"
        node.ts_us = int(observation["ts_us"])

    def _candidate_payload(self, node: LapState, candidate: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        valid = [item for item in node.candidates if item.get("valid")]
        score = 1.0 / max(1, len(valid))
        return {
            "lap": f"{node.lap:06X}",
            "uap": int(candidate["uap"]),
            "uap_hex": f"{int(candidate['uap']):02X}",
            "score": round(score, 3),
            "channel": self.channel,
            "center_freq_hz": self.center_freq_hz,
            "rssi_dbfs": observation["rssi_dbfs"],
            "status": node.status,
            "candidate_count": len(valid),
            "processed_packets": node.processed_packets,
            "broken_packets": node.broken_packets,
            "repaired": bool(observation.get("repaired", False)),
            "repair_distance": int(observation.get("repair_distance", 0)),
            "header_perfect_triplets": int(observation.get("header_perfect_triplets", 0)),
            "header_relaxed": bool(observation.get("header_relaxed", False)),
            "ts_us": int(observation.get("ts_us", 0)),
            "clks": candidate["clks"],
            "notes": [
                "LAP extracted from Classic access code.",
                "UAP candidate validated by dewhitening header and matching HEC.",
            ],
        }

    @staticmethod
    def _extract_lsb_byte(bits: list[int], start: int) -> int:
        value = 0
        for idx in range(8):
            value |= (bits[start + idx] & 1) << idx
        return value

    @staticmethod
    def _swap_bits(value: int) -> int:
        out = 0
        for idx in range(8):
            out = (out << 1) | ((value >> idx) & 1)
        return out

    @staticmethod
    def _bit_length(value: int) -> int:
        return int(value).bit_length()

    @classmethod
    def _compute_remainder(cls, input_value: int, divisor: int) -> int:
        divisor_length = cls._bit_length(divisor)
        input_value <<= divisor_length
        while cls._bit_length(input_value) >= divisor_length:
            input_value ^= divisor << (cls._bit_length(input_value) - divisor_length)
        return input_value

    def _burst_only_events(
        self,
        kind: str,
        rssi_dbfs: float,
        burst_spans: list[tuple[int, int, float]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "kind": kind,
                "seen_at": time.time(),
                "channel": self.channel,
                "center_freq_hz": self.center_freq_hz,
                "rssi_dbfs": round(rssi_dbfs, 1),
                "peak_dbfs": round(peak, 1),
                "confidence": 0.28,
            }
            for _, _, peak in burst_spans[:8]
        ]

    @staticmethod
    def _bytes_to_lsb_bits(data: bytes) -> list[int]:
        return [(byte >> bit) & 1 for byte in data for bit in range(8)]

    @staticmethod
    def _bits_to_bytes(bits: list[int]) -> bytes:
        if len(bits) < 8:
            return b""
        usable = (len(bits) // 8) * 8
        out = bytearray()
        for idx in range(0, usable, 8):
            value = 0
            for bit in range(8):
                value |= (bits[idx + bit] & 1) << bit
            out.append(value)
        return bytes(out)

    @staticmethod
    def _find_bit_pattern(bits: list[int], pattern: list[int], max_errors: int) -> list[int]:
        if len(bits) < len(pattern):
            return []
        hits: list[int] = []
        plen = len(pattern)
        for pos in range(0, len(bits) - plen + 1):
            errors = 0
            for offset, expected in enumerate(pattern):
                if bits[pos + offset] != expected:
                    errors += 1
                    if errors > max_errors:
                        break
            if errors <= max_errors:
                hits.append(pos)
        return hits[:8]

    @staticmethod
    def _format_ble_addr(raw: bytes) -> str:
        return ":".join(f"{byte:02X}" for byte in raw[::-1])

    @staticmethod
    def _ble_pdu_name(pdu_type: int) -> str:
        names = {
            0: "ADV_IND",
            1: "ADV_DIRECT_IND",
            2: "ADV_NONCONN_IND",
            3: "SCAN_REQ",
            4: "SCAN_RSP",
            5: "CONNECT_IND",
            6: "ADV_SCAN_IND",
        }
        return names.get(pdu_type, f"PDU_{pdu_type}")


class WideClassicDetector:
    def __init__(self, sample_rate_sps: int, center_freq_hz: int, bank_start_channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.bank_start_channel = int(bank_start_channel)
        self.lane_rate_sps = BT_CLASSIC_LANE_RATE_SPS
        self.decim = max(1, int(round(self.sample_rate_sps / self.lane_rate_sps)))
        self.use_cpp_fft = bool(BT_CLASSIC_USE_CPP_FFT and self.decim == BT_CLASSIC_BANK_SIZE)
        self._fft_tail = np.empty(0, dtype=np.complex64)
        self.sample_phase_offsets = self._sample_phase_offsets(self.decim)
        self.freq_offset_adjustments_hz = self._freq_offset_adjustments_hz()
        filter_taps = max(31, min(193, (self.decim * 4) | 1))
        cutoff_hz = min(float(self.lane_rate_sps) * 0.40, 800_000.0)
        self._lane_filter_taps = _design_lowpass_taps(self.sample_rate_sps, cutoff_hz, filter_taps)
        self.lanes: list[dict[str, Any]] = []
        self.stats = {
            "preamble_hits": 0,
            "barker_hits": 0,
            "access_code_mismatch": 0,
            "access_code_hits": 0,
            "access_code_repair_hits": 0,
            "target_access_near_hits": 0,
            "target_access_best_distance": 68,
            "lap_hits": 0,
            "header_failures": 0,
            "header_relaxed_hits": 0,
            "uap_candidate_hits": 0,
        }
        for idx in range(BT_CLASSIC_BANK_SIZE):
            channel = self.bank_start_channel + idx
            if channel not in BT_CLASSIC_CHANNELS:
                continue
            freq_hz = BT_CLASSIC_CHANNELS[channel]
            self.lanes.append(
                {
                    "channel": channel,
                    "freq_hz": freq_hz,
                    "offset_hz": float(freq_hz - self.center_freq_hz),
                    "cpp_detector": BluetoothDetector(self.lane_rate_sps, "classic", freq_hz, channel),
                    "mix_paths": [
                        {
                            "freq_adjust_hz": int(freq_adjust_hz),
                            "mix_phase_rad": 0.0,
                            "filter_state": np.empty(0, dtype=np.complex64),
                            "phase_paths": [
                                {
                                    "sample_offset": sample_offset,
                                    "detector": BluetoothDetector(self.lane_rate_sps, "classic", freq_hz, channel),
                                }
                                for sample_offset in self.sample_phase_offsets
                            ],
                        }
                        for freq_adjust_hz in self.freq_offset_adjustments_hz
                    ],
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = BluetoothDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, [], []
        if self.use_cpp_fft:
            return self._process_iq_cpp_fft(z)

        all_events: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        rssis: list[float] = []
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.lanes:
            base_offset_hz = float(lane["offset_hz"])
            for mix_path in lane.get("mix_paths", []):
                offset_hz = base_offset_hz + float(mix_path.get("freq_adjust_hz", 0))
                phase_step = float((-2.0 * np.pi * offset_hz) / float(self.sample_rate_sps))
                phase0 = float(mix_path.get("mix_phase_rad", 0.0))
                rot = np.exp(1j * (phase0 + (phase_step * sample_idx))).astype(np.complex64)
                mix_path["mix_phase_rad"] = float((phase0 + (phase_step * float(z.size))) % (2.0 * np.pi))
                mixed = z * rot
                lane_samples_by_offset = self._decimate_lane(mix_path, mixed)
                if not lane_samples_by_offset:
                    continue
                for phase_path in mix_path.get("phase_paths", []):
                    sample_offset = int(phase_path.get("sample_offset", 0))
                    lane_samples = lane_samples_by_offset.get(sample_offset)
                    if lane_samples is None or lane_samples.size < 64:
                        continue
                    detector = phase_path["detector"]
                    rssi, events, candidates = detector.process_complex(lane_samples)
                    for key, value in detector.stats.items():
                        if key == "target_access_best_distance":
                            current = int(self.stats.get(key, 68))
                            self.stats[key] = min(current, int(value))
                            detector.stats[key] = 68
                            continue
                        self.stats[key] = int(self.stats.get(key, 0)) + int(value)
                        detector.stats[key] = 0
                    rssis.append(rssi)
                    all_events.extend(events)
                    all_candidates.extend(candidates)

        all_events = self._dedupe_classic_events(all_events)
        all_candidates = self._dedupe_classic_candidates(all_candidates)
        bank_rssi = max(rssis) if rssis else -120.0
        return bank_rssi, all_events[:80], all_candidates[:80]

    def _process_iq_cpp_fft(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        if self._fft_tail.size:
            z = np.concatenate((self._fft_tail, z))
        usable = (z.size // self.decim) * self.decim
        self._fft_tail = z[usable:].astype(np.complex64, copy=False)
        if usable <= 0:
            return -120.0, [], []

        frames = z[:usable].reshape(-1, self.decim)
        bins = np.fft.fft(frames, axis=1).astype(np.complex64, copy=False)
        all_events: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        rssis: list[float] = []

        for idx, lane in enumerate(self.lanes):
            fft_bin = (idx + (self.decim // 2)) % self.decim
            lane_samples = bins[:, fft_bin]
            if lane_samples.size < 2:
                continue
            prev = lane_samples[:-1]
            cur = lane_samples[1:]
            cross = (prev.real * cur.imag) - (prev.imag * cur.real)
            bits = [1 if value > 0 else 0 for value in cross.tolist()]
            rssi = float(10.0 * np.log10(float(np.mean(np.abs(lane_samples) ** 2)) + 1e-12))
            detector = lane["cpp_detector"]
            _, events, candidates = detector.process_classic_cpp_bits(bits, rssi)
            for event in events:
                event["btcsniffer_bin"] = idx
                event["demod"] = "cpp-fft"
            for candidate in candidates:
                candidate["btcsniffer_bin"] = idx
                candidate["demod"] = "cpp-fft"
            for key, value in detector.stats.items():
                if key == "target_access_best_distance":
                    current = int(self.stats.get(key, 68))
                    self.stats[key] = min(current, int(value))
                    detector.stats[key] = 68
                    continue
                self.stats[key] = int(self.stats.get(key, 0)) + int(value)
                detector.stats[key] = 0
            rssis.append(rssi)
            all_events.extend(events)
            all_candidates.extend(candidates)

        all_events = self._dedupe_classic_events(all_events)
        all_candidates = self._dedupe_classic_candidates(all_candidates)
        bank_rssi = max(rssis) if rssis else -120.0
        return bank_rssi, all_events[:80], all_candidates[:80]

    @staticmethod
    def _event_rank(event: dict[str, Any]) -> tuple[float, ...]:
        return (
            1.0 if not bool(event.get("repaired", False)) else 0.0,
            -float(event.get("candidate_count") or 99),
            float(event.get("processed_packets") or 0),
            float(event.get("header_perfect_triplets") or 0),
            float(event.get("rssi_dbfs") or -120.0),
        )

    @classmethod
    def _dedupe_classic_events(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        passthrough: list[dict[str, Any]] = []
        for event in events:
            if event.get("kind") != "classic_lap":
                passthrough.append(event)
                continue
            ts_bucket = int(round(float(event.get("ts_us") or 0) / 80.0))
            key = (
                str(event.get("lap") or ""),
                int(event.get("channel") or -1),
                ts_bucket,
            )
            existing = deduped.get(key)
            if existing is None or cls._event_rank(event) > cls._event_rank(existing):
                deduped[key] = event
        lap_events = list(deduped.values())
        lap_events.sort(key=lambda item: float(item.get("seen_at") or 0), reverse=True)
        passthrough.sort(key=lambda item: float(item.get("seen_at") or 0), reverse=True)
        return lap_events + passthrough

    @classmethod
    def _dedupe_classic_candidates(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidates:
            ts_bucket = int(round(float(candidate.get("ts_us") or 0) / 80.0))
            key = (
                str(candidate.get("lap") or ""),
                str(candidate.get("uap_hex") or candidate.get("uap") or ""),
                int(candidate.get("channel") or -1),
                ts_bucket,
            )
            existing = deduped.get(key)
            if existing is None or cls._event_rank(candidate) > cls._event_rank(existing):
                deduped[key] = candidate
        rows = list(deduped.values())
        rows.sort(
            key=lambda item: (
                float(item.get("candidate_count") or 99),
                -float(item.get("processed_packets") or 0),
                -float(item.get("rssi_dbfs") or -120.0),
            )
        )
        return rows

    @staticmethod
    def _sample_phase_offsets(decim: int) -> list[int]:
        if decim <= 1:
            return [0]
        count = min(4, decim)
        if count == decim:
            return list(range(decim))
        offsets = sorted({min(decim - 1, int(round((idx * decim) / count))) for idx in range(count)})
        if 0 not in offsets:
            offsets.insert(0, 0)
        return offsets

    @staticmethod
    def _freq_offset_adjustments_hz() -> list[int]:
        # Small CFO sweep around each nominal 1 MHz channel center.
        return [-40_000, 0, 40_000]

    def _decimate_lane(self, path_state: dict[str, Any], z: np.ndarray) -> dict[int, np.ndarray]:
        history = path_state.get("filter_state")
        if not isinstance(history, np.ndarray):
            history = np.empty(0, dtype=np.complex64)
        if history.size:
            z = np.concatenate((history, z))
        taps = self._lane_filter_taps
        if z.size < taps.size:
            path_state["filter_state"] = z[-(taps.size - 1) :].astype(np.complex64, copy=False)
            return {}
        filtered_i = np.convolve(z.real.astype(np.float32, copy=False), taps, mode="valid")
        filtered_q = np.convolve(z.imag.astype(np.float32, copy=False), taps, mode="valid")
        path_state["filter_state"] = z[-(taps.size - 1) :].astype(np.complex64, copy=False)
        filtered = (filtered_i + 1j * filtered_q).astype(np.complex64)
        if self.decim <= 1:
            return {0: filtered}
        outputs: dict[int, np.ndarray] = {}
        for sample_offset in self.sample_phase_offsets:
            if sample_offset >= filtered.size:
                continue
            available = filtered.size - sample_offset
            usable = (available // self.decim) * self.decim
            if usable <= 0:
                continue
            outputs[sample_offset] = filtered[sample_offset : sample_offset + usable : self.decim].astype(np.complex64, copy=False)
        return outputs


class CombinedBluetoothDetector:
    def __init__(self, sample_rate_sps: int, center_freq_hz: int, bank_start_channel: int) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.classic = WideClassicDetector(sample_rate_sps, center_freq_hz, bank_start_channel)
        self.ble_lanes: list[dict[str, Any]] = []
        self.stats = self.classic.stats
        self.ble_decim = max(1, int(round(self.sample_rate_sps / BLE_ADV_SAMPLE_RATE_SPS)))
        for channel, freq_hz in BLE_ADV_CHANNELS.items():
            offset_hz = float(freq_hz - self.center_freq_hz)
            if abs(offset_hz) > (self.sample_rate_sps / 2.0) - 1_200_000:
                continue
            self.ble_lanes.append(
                {
                    "channel": channel,
                    "freq_hz": freq_hz,
                    "offset_hz": offset_hz,
                    "detector": BluetoothDetector(BLE_ADV_SAMPLE_RATE_SPS, "ble", freq_hz, channel),
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        z = BluetoothDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, [], []

        classic_rssi, events, candidates = self.classic.process_iq_i8(raw)
        self.stats = self.classic.stats
        rssis = [classic_rssi]
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.ble_lanes:
            rot = np.exp((-2j * np.pi * float(lane["offset_hz"]) / float(self.sample_rate_sps)) * sample_idx).astype(np.complex64)
            mixed = z * rot
            lane_samples = self._decimate(mixed, self.ble_decim)
            if lane_samples.size < 64:
                continue
            rssi, ble_events, _ = lane["detector"].process_complex(lane_samples)
            rssis.append(rssi)
            events.extend(ble_events)
        return max(rssis) if rssis else -120.0, events[:96], candidates[:80]

    @staticmethod
    def _decimate(z: np.ndarray, decim: int) -> np.ndarray:
        if decim <= 1:
            return z
        usable = (z.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return z[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)


app = Flask(__name__, static_folder=str(PROJECT_ROOT / "ui" / "frontend"), static_url_path="")
app.logger.setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
state = ExplorerState()
fm_playback = FmPlaybackState()
walkie_playback = WalkiePlaybackState()
state_lock = threading.RLock()
csv_log_lock = threading.Lock()
seen_history_lock = threading.Lock()
seen_history_cache: dict[str, Any] = {"loaded_at": 0.0, "dates": {}}
identity_cache_lock = threading.Lock()
worker_stop = threading.Event()
demo_replay_paused = threading.Event()
worker_thread: threading.Thread | None = None
worker_stops: dict[str, threading.Event] = {}
worker_threads: dict[str, threading.Thread] = {}
fm_worker_stop = threading.Event()
fm_worker_thread: threading.Thread | None = None
fm_audio_q: queue.Queue[bytes] = queue.Queue(maxsize=96)
fm_pending_thread: threading.Thread | None = None
fm_request_serial = 0
walkie_worker_stop = threading.Event()
walkie_worker_thread: threading.Thread | None = None
walkie_audio_q: queue.Queue[bytes] = queue.Queue(maxsize=96)
walkie_recent_audio: deque[bytes] = deque(maxlen=160)
walkie_recent_lock = threading.Lock()
walkie_pending_thread: threading.Thread | None = None
walkie_request_serial = 0

CONSOLE_DASHBOARD = os.getenv("RF_SENTINEL_CONSOLE_DASHBOARD", "1").strip().lower() not in {"0", "false", "no", "off"}
CONSOLE_COLOR = os.getenv("RF_SENTINEL_CONSOLE_COLOR", "1").strip().lower() not in {"0", "false", "no", "off"}
CONSOLE_REFRESH_S = 0.5
CONSOLE_LOG_BUFFER_LINES = max(20, int(os.getenv("RF_SENTINEL_CONSOLE_LOG_BUFFER_LINES", "200")))
CONSOLE_LOG_VIEW_LINES = max(4, int(os.getenv("RF_SENTINEL_CONSOLE_LOG_VIEW_LINES", "10")))
console_lock = threading.Lock()
console_log_lines: list[str] = []
console_last_render = 0.0
console_dashboard_stop = threading.Event()
console_dashboard_thread: threading.Thread | None = None
console_textual_active = threading.Event()
inquiry_process: subprocess.Popen[str] | None = None
btc_engine_process: subprocess.Popen[str] | None = None
btc_engine_thread: threading.Thread | None = None
btc_engine_stop = threading.Event()
rf_sentinel_process: subprocess.Popen[str] | None = None
rf_sentinel_thread: threading.Thread | None = None
rf_sentinel_stop = threading.Event()
devices_cache_lock = threading.Lock()
devices_cache: list[dict[str, Any]] = []
devices_cache_updated_at = 0.0
shutdown_lock = threading.Lock()
shutdown_complete = False
ble_identity_cache: dict[str, dict[str, Any]] = {}
btc_name_cache_lock = threading.Lock()
btc_name_cache: dict[str, dict[str, Any]] = {}
company_identifier_lut: dict[str, str] = {}
uuid16_identifier_lut: dict[str, str] = {}
UUID16_VENDOR_OVERRIDES = {
    "0xFCB2": "Apple, Inc.",
    "0xFEED": "Tile, Inc.",
}

CSV_COMMON_COLUMNS = [
    "run_id",
    "observed_at_iso",
    "observed_at_epoch",
    "logged_at_iso",
    "scanner_source",
    "protocol",
    "kind",
    "identity",
    "device_type",
    "device_type_detail",
    "mac",
    "name",
    "source_address",
    "destination_address",
    "bssid",
    "ssid",
    "wifi_role",
    "channel",
    "center_freq_hz",
    "frequency_hz",
    "frequency_mhz",
    "rssi_dbfs",
    "rssi_dbm",
    "confidence",
    "detail",
    "payload_hex",
    "raw_json",
]

CSV_PROTOCOL_COLUMNS = {
    "btle": [
        "address",
        "address_type",
        "uuid16",
        "uuid16_names",
        "manufacturer_id",
        "manufacturer_name",
        "appearance_category",
        "appearance_name",
    ],
    "btc": [
        "lap",
        "uap",
        "nap",
        "full_mac",
        "status",
        "target",
        "candidate_count",
        "processed_packets",
        "broken_packets",
        "repaired",
        "repair_distance",
    ],
    "zigbee": ["pan_id", "fcs_ok", "fcs_hex", "decoded_text", "sequence_number", "psdu_hex"],
    "tpms": ["protocol_variant", "sensor_id"],
    "walkie": [
        "classification",
        "modulation",
        "signal_dbfs",
        "audio_rms_dbfs",
        "audio_bandwidth_hz",
        "voice_band_ratio",
        "voice_activity_ratio",
        "occupied_ratio",
        "freq_std_hz",
        "saved_iq_path",
        "saved_meta_path",
        "saved_wav_path",
    ],
    "wifi": ["ssid_visible", "count"],
    "fm": ["power_dbfs", "noise_dbfs", "excess_db", "audio_rms", "pilot_db", "rds_subcarrier_db", "stereo_likely", "rds_likely"],
    "lfmf": [
        "frequency_khz",
        "carrier_dbfs",
        "carrier_snr_db",
        "excess_db",
        "audio_dbfs",
        "modulation_pct",
        "band",
        "band_label",
        "active",
    ],
    "cellular": [
        "band",
        "link",
        "cellular_type",
        "technology",
        "likely_operator",
        "operator_confidence",
        "likely_mcc",
        "likely_mnc",
        "likely_plmn",
        "plmn_source",
        "decoded_mcc",
        "decoded_mnc",
        "decoded_plmn",
        "decoded_plmn_source",
        "lte_sync_status",
        "lte_pss_detected",
        "lte_n_id_2",
        "lte_pss_metric",
        "lte_pss_freq_offset_hz",
        "lte_cell_id_status",
        "lte_mib_status",
        "lte_sib1_status",
        "excess_db",
        "noise_floor_dbfs",
        "occupied_width_hz",
        "target",
        "passive_only",
        "content_decoded",
    ],
}

CSV_COMBINED_COLUMNS = CSV_COMMON_COLUMNS + sorted({col for cols in CSV_PROTOCOL_COLUMNS.values() for col in cols})
CSV_PROTOCOL_FILE_NAMES = {
    "BTLE": "btle.csv",
    "BTC": "btc.csv",
    "ZIGBEE": "zigbee.csv",
    "TPMS": "tpms.csv",
    "WALKIE": "walkie.csv",
    "WIFI": "wifi.csv",
    "FM": "fm.csv",
    "LFMF": "lfmf.csv",
    "CELLULAR": "cellular.csv",
}
CSV_LOGGABLE_KINDS = {
    "ble_adv",
    "classic_lap",
    "zigbee_frame",
    "tpms_frame",
    "walkie_signal",
    "wifi_frame",
    "fm_station",
    "lfmf_signal",
    "cellular_signal",
}

BLE_APPEARANCE_LABELS = {
    0x0000: "Unknown",
    0x0040: "Phone",
    0x0080: "Computer",
    0x00C0: "Watch",
    0x00C1: "Sports Watch",
    0x0100: "Clock",
    0x0140: "Display",
    0x0180: "Remote",
    0x01C0: "Eye-glasses",
    0x0200: "Tag",
    0x0240: "Keyring",
    0x0280: "Media Player",
    0x02C0: "Barcode Scanner",
    0x0300: "Thermometer",
    0x0340: "Heart Rate Sensor",
    0x0380: "Blood Pressure",
    0x03C0: "HID",
    0x03C1: "Keyboard",
    0x03C2: "Mouse",
    0x03C3: "Joystick",
    0x03C4: "Gamepad",
    0x03C5: "Digitizer Tablet",
    0x03C6: "Card Reader",
    0x03C7: "Digital Pen",
    0x03C8: "Barcode Scanner",
    0x0400: "Glucose Meter",
    0x0440: "Running Sensor",
    0x0441: "Running Sensor Pod",
    0x0442: "Running Sensor Shoe",
    0x0480: "Cycling",
    0x0481: "Cycling Computer",
    0x0482: "Cycling Speed Sensor",
    0x0483: "Cycling Cadence Sensor",
    0x0484: "Cycling Power Sensor",
    0x0485: "Cycling Speed/Cadence Sensor",
    0x04C0: "Pulse Oximeter",
    0x0500: "Weight Scale",
    0x0540: "Personal Mobility",
    0x0580: "Continuous Glucose Monitor",
    0x05C0: "Insulin Pump",
    0x0600: "Medication Delivery",
    0x0640: "Outdoor Sports",
    0x0641: "Location Display Device",
    0x0642: "Location Navigation Device",
    0x0643: "Location Pod",
    0x0644: "Location Beacon",
}


def _btc_log(message: str, *args: Any, level: int = logging.INFO) -> None:
    try:
        app.logger.log(level, f"[BTC] {message}", *args)
    except Exception:
        pass


def _log_http_error(status_code: int, handler_name: str, payload: dict[str, Any], exc: Exception | None = None) -> None:
    path = request.path if request else "?"
    method = request.method if request else "?"
    detail = payload.get("detail") or payload.get("error") or ""
    if exc is not None:
        app.logger.warning(
            "[HTTP %d] handler=%s method=%s path=%s error=%s exc=%s",
            status_code,
            handler_name,
            method,
            path,
            detail,
            exc,
        )
    else:
        app.logger.warning(
            "[HTTP %d] handler=%s method=%s path=%s error=%s",
            status_code,
            handler_name,
            method,
            path,
            detail,
        )


def _json_error(status_code: int, handler_name: str, **payload: Any):
    _log_http_error(status_code, handler_name, payload)
    return jsonify(payload), status_code


def _normalize_mac(mac: str) -> str:
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()
    if len(clean) != 12:
        return str(mac or "").upper()
    return ":".join(clean[idx : idx + 2] for idx in range(0, 12, 2))


def _load_company_identifier_lut() -> dict[str, str]:
    if not COMPANY_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with COMPANY_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    companies = data.get("companies") if isinstance(data, dict) else {}
    if not isinstance(companies, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in companies.items()}


def _company_name(company_id: str) -> str:
    return company_identifier_lut.get(str(company_id or "").upper().replace("X", "x"), "")


def _load_uuid16_identifier_lut() -> dict[str, str]:
    if not UUID16_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with UUID16_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    uuids = data.get("uuids") if isinstance(data, dict) else {}
    if not isinstance(uuids, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in uuids.items()}


def _uuid16_name(uuid16: str) -> str:
    key = str(uuid16 or "").upper().replace("X", "x")
    return UUID16_VENDOR_OVERRIDES.get(key) or uuid16_identifier_lut.get(key, "")


def _uuid16_names(uuid16_values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for uuid in uuid16_values for name in [_uuid16_name(uuid)] if name))


def _canonical_ble_vendor(name: str) -> str:
    value = str(name or "").strip()
    lowered = value.lower()
    if "apple" in lowered:
        return "Apple, Inc."
    if "microsoft" in lowered:
        return "Microsoft"
    if "tile" in lowered:
        return "Tile, Inc."
    return value


def _manufacturer_from_uuid16(uuid16_values: list[str]) -> dict[str, Any] | None:
    for uuid in uuid16_values:
        name = _canonical_ble_vendor(_uuid16_name(uuid))
        if not name:
            continue
        return {
            "company_id": "",
            "company_name": name,
            "data": "",
            "source": "uuid16",
            "uuid16": str(uuid).upper().replace("X", "x"),
        }
    return None


def _ble_identity_label(name: str, uuid16_names: list[str], manufacturer: dict[str, Any] | None, mac: str) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return local_name
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if uuid16_names:
        first_uuid_name = str(uuid16_names[0] or "").strip()
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return first_uuid_name
    if manufacturer_name:
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return manufacturer_name
    return mac


def _ble_device_type_label(
    name: str,
    uuid16_names: list[str],
    manufacturer: dict[str, Any] | None,
    appearance: dict[str, Any] | None,
) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return ""
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "AirTag"
    if appearance and str(appearance.get("label") or "").strip():
        return str(appearance.get("label") or "").strip()
    if manufacturer_name == "Tile, Inc.":
        return "Tracker"
    return ""


def _ble_device_type_detail(
    uuid16_names: list[str],
    manufacturer: dict[str, Any] | None,
    appearance: dict[str, Any] | None,
) -> str:
    manufacturer_name = _canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    manufacturer_data = str((manufacturer or {}).get("data") or "").upper()
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "Find My UUID16"
    if manufacturer_name == "Apple, Inc." and manufacturer_data:
        prefix = manufacturer_data[:4]
        if prefix == "1202":
            return "Find My manufacturer frame"
        if prefix in {"1005", "1003", "1001"}:
            return "Continuity frame"
        return f"Apple manufacturer frame {prefix}" if prefix else "Apple manufacturer frame"
    if manufacturer_name == "Microsoft" and manufacturer_data:
        prefix = manufacturer_data[:2]
        if prefix == "03":
            return "Swift Pair frame"
        return f"Microsoft manufacturer frame {manufacturer_data[:4]}" if manufacturer_data else "Microsoft manufacturer frame"
    if manufacturer_name == "Tile, Inc.":
        if uuid16_names:
            return "Tile UUID16 service"
        if manufacturer_data:
            return "Tile manufacturer frame"
        return "Tile tracker"
    if appearance and str(appearance.get("code") or "").strip():
        return str(appearance.get("code") or "").strip()
    return ""


def _ble_identity_source(name: str, uuid16_names: list[str], manufacturer: dict[str, Any] | None) -> str:
    manufacturer_name = str((manufacturer or {}).get("company_name") or "")
    if name:
        return "Local name"
    if uuid16_names:
        label = uuid16_names[0]
        if _canonical_ble_vendor(label) == "Apple, Inc.":
            return "AirTag inferred from UUID16 service"
        return f"{label} UUID16 service"
    if manufacturer_name:
        if (manufacturer or {}).get("source") == "uuid16":
            if _canonical_ble_vendor(manufacturer_name) == "Apple, Inc.":
                return "AirTag inferred from UUID16 service"
            return f"{manufacturer_name} inferred from UUID16 service"
        return f"{manufacturer_name} manufacturer data"
    return "MAC only"


def _load_ble_identity_cache() -> dict[str, dict[str, Any]]:
    if not BLE_IDENTITY_CACHE_PATH.exists():
        return {}
    try:
        with BLE_IDENTITY_CACHE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("devices", data) if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in rows.items():
        if not isinstance(value, dict):
            continue
        mac = _normalize_mac(str(value.get("mac") or key))
        if not mac:
            continue
        manufacturer = value.get("manufacturer") if isinstance(value.get("manufacturer"), dict) else None
        appearance = value.get("appearance") if isinstance(value.get("appearance"), dict) else None
        if manufacturer and manufacturer.get("company_id") and not manufacturer.get("company_name"):
            manufacturer = dict(manufacturer)
            manufacturer["company_name"] = _company_name(str(manufacturer.get("company_id")))
        uuid16 = value.get("uuid16") if isinstance(value.get("uuid16"), list) else []
        if not manufacturer:
            manufacturer = _manufacturer_from_uuid16(uuid16)
        out[mac] = {
            "mac": mac,
            "name": str(value.get("name") or "").strip(),
            "address_type": str(value.get("address_type") or "").strip(),
            "uuid16": uuid16,
            "uuid16_names": _uuid16_names(uuid16),
            "manufacturer": manufacturer,
            "appearance": appearance,
            "identity_source": str(value.get("identity_source") or ""),
            "device_type": str(value.get("device_type") or ""),
            "device_type_detail": str(value.get("device_type_detail") or ""),
            "first_seen_at": float(value.get("first_seen_at") or value.get("last_seen_at") or time.time()),
            "last_seen_at": float(value.get("last_seen_at") or time.time()),
            "seen_count": int(value.get("seen_count") or 0),
        }
    return out


def _save_ble_identity_cache() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "devices": dict(sorted(ble_identity_cache.items())),
    }
    tmp_path = BLE_IDENTITY_CACHE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_path.replace(BLE_IDENTITY_CACHE_PATH)


def _remember_ble_identity(
    mac: str,
    name: str,
    address_type: str,
    seen_at: float,
    uuid16: list[str] | None = None,
    manufacturer: dict[str, Any] | None = None,
    appearance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_mac(mac)
    with identity_cache_lock:
        row = dict(ble_identity_cache.get(normalized) or {})
        row["mac"] = normalized
        if name:
            row["name"] = name
        else:
            row.setdefault("name", "")
        if address_type:
            row["address_type"] = address_type
        else:
            row.setdefault("address_type", "")
        merged_uuid16 = list(dict.fromkeys([*(row.get("uuid16") or []), *(uuid16 or [])]))
        row["uuid16"] = merged_uuid16
        row["uuid16_names"] = _uuid16_names(merged_uuid16)
        if manufacturer:
            row["manufacturer"] = manufacturer
        else:
            row["manufacturer"] = row.get("manufacturer") or _manufacturer_from_uuid16(merged_uuid16)
        if appearance:
            row["appearance"] = appearance
        else:
            row.setdefault("appearance", row.get("appearance") if isinstance(row.get("appearance"), dict) else None)
        row["identity_source"] = _ble_identity_source(str(row.get("name") or ""), row["uuid16_names"], row.get("manufacturer"))
        row["device_type"] = _ble_device_type_label(
            str(row.get("name") or ""),
            row["uuid16_names"],
            row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
        )
        row["device_type_detail"] = _ble_device_type_detail(
            row["uuid16_names"],
            row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
        )
        row["first_seen_at"] = float(row.get("first_seen_at") or seen_at)
        row["last_seen_at"] = seen_at
        row["seen_count"] = int(row.get("seen_count") or 0) + 1
        ble_identity_cache[normalized] = row
        _save_ble_identity_cache()
        return dict(row)


def _btc_name_keys(address: Any = "", lap: Any = "", uap: Any = "") -> list[str]:
    keys: list[str] = []
    address_clean = _classic_mac_key(address)
    lap_clean = _classic_lap_key(lap or address)
    uap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(uap or "")).upper()
    if address_clean:
        keys.append(f"addr:{address_clean}")
    if lap_clean and _classic_uap_resolved(uap_clean):
        keys.append(f"uaplap:{uap_clean}:{lap_clean}")
    if lap_clean:
        keys.append(f"lap:{lap_clean}")
    return list(dict.fromkeys(keys))


def _load_btc_name_cache() -> dict[str, dict[str, Any]]:
    if not BTC_NAME_CACHE_PATH.exists():
        return {}
    try:
        with BTC_NAME_CACHE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("devices", data) if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in rows.items():
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").strip()
        if not name:
            continue
        row = {
            "name": name,
            "address": _classic_mac_key(value.get("address")),
            "lap": _classic_lap_key(value.get("lap")),
            "uap": re.sub(r"[^0-9A-Fa-f]", "", str(value.get("uap") or "")).upper()[:2],
            "checked_at": float(value.get("checked_at") or value.get("last_seen_at") or 0.0),
            "last_seen_at": float(value.get("last_seen_at") or value.get("checked_at") or 0.0),
            "pending": False,
            "ok": True,
        }
        out[str(key)] = row
    return out


def _save_btc_name_cache() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "devices": dict(sorted((key, value) for key, value in btc_name_cache.items() if value.get("name"))),
    }
    tmp_path = BTC_NAME_CACHE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_path.replace(BTC_NAME_CACHE_PATH)


def _remember_btc_name(name: str, address: Any = "", lap: Any = "", uap: Any = "") -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        return {}
    row = {
        "name": clean_name,
        "address": _classic_mac_key(address),
        "lap": _classic_lap_key(lap or address),
        "uap": re.sub(r"[^0-9A-Fa-f]", "", str(uap or "")).upper()[:2],
        "checked_at": time.time(),
        "last_seen_at": time.time(),
        "pending": False,
        "ok": True,
    }
    with btc_name_cache_lock:
        for key in _btc_name_keys(row["address"], row["lap"], row["uap"]):
            btc_name_cache[key] = dict(row)
        _save_btc_name_cache()
    return dict(row)


def _cached_btc_name(lap: Any = "", uap: Any = "", address: Any = "") -> str:
    keys = _btc_name_keys(address, lap, uap)
    with btc_name_cache_lock:
        for key in keys:
            name = str((btc_name_cache.get(key) or {}).get("name") or "").strip()
            if name:
                return name
    return ""


company_identifier_lut.update(_load_company_identifier_lut())
uuid16_identifier_lut.update(_load_uuid16_identifier_lut())
ble_identity_cache.update(_load_ble_identity_cache())


def _btcsniffer_binary() -> Path:
    if BTC_SNIFFER_BINARY.exists():
        return BTC_SNIFFER_BINARY
    fallback = BTC_SNIFFER_ROOT / "build" / "btsniffer"
    return fallback if fallback.exists() else BTC_SNIFFER_BINARY


def _native_arch_tokens() -> tuple[str, ...]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return ("x86-64", "x86_64", "amd64")
    if machine in {"aarch64", "arm64"}:
        return ("aarch64", "arm64")
    if machine.startswith("arm"):
        return ("arm",)
    return (machine,)


def _binary_arch_matches_host(binary: Path) -> tuple[bool, str]:
    if not binary.exists():
        return False, "missing"
    file_tool = shutil.which("file")
    if not file_tool:
        return True, "file tool unavailable"
    try:
        result = subprocess.run(
            [file_tool, "-b", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return True, f"file check failed: {exc}"
    description = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or not description:
        return True, description or f"file returned {result.returncode}"
    if "elf" not in description:
        return True, description
    expected = _native_arch_tokens()
    if any(token in description for token in expected):
        return True, description
    return False, description


def _btcsniffer_build_inputs() -> list[Path]:
    inputs = [BTC_SNIFFER_ROOT / "CMakeLists.txt"]
    inputs.extend(sorted((BTC_SNIFFER_ROOT / "src").glob("*.cpp")))
    inputs.extend(sorted((BTC_SNIFFER_ROOT / "src").glob("*.hpp")))
    return [path for path in inputs if path.exists()]


def _btcsniffer_cache_matches_source(build_dir: Path) -> bool:
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return True
    try:
        text = cache.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    source_line = f"btcexplorer-sniffer_SOURCE_DIR:STATIC={BTC_SNIFFER_ROOT}"
    home_line = f"CMAKE_HOME_DIRECTORY:INTERNAL={BTC_SNIFFER_ROOT}"
    return source_line in text or home_line in text


def _btcsniffer_rebuild_reason(binary: Path) -> str | None:
    if not binary.exists():
        return "binary missing"
    if not os.access(binary, os.X_OK):
        return "binary is not executable"
    arch_ok, arch_detail = _binary_arch_matches_host(binary)
    if not arch_ok:
        return f"binary architecture does not match host ({arch_detail})"
    build_dir = BTC_SNIFFER_ROOT / "build"
    if not _btcsniffer_cache_matches_source(build_dir):
        return "CMake cache points at a different source directory"
    try:
        binary_mtime = binary.stat().st_mtime
    except OSError:
        return "binary stat failed"
    newest_input = max((path.stat().st_mtime for path in _btcsniffer_build_inputs()), default=0.0)
    if newest_input > binary_mtime:
        return "source is newer than binary"
    return None


def _build_btcsniffer_binary(reason: str) -> Path:
    if not BTC_SNIFFER_AUTO_BUILD:
        raise RuntimeError(f"btcsniffer rebuild required but BTC_SNIFFER_AUTO_BUILD is disabled: {reason}")
    cmake = shutil.which("cmake")
    if not cmake:
        raise RuntimeError(f"btcsniffer rebuild required ({reason}) but cmake was not found")
    build_dir = BTC_SNIFFER_ROOT / "build"
    with btcsniffer_build_lock:
        binary = _btcsniffer_binary()
        second_reason = _btcsniffer_rebuild_reason(binary)
        if second_reason is None:
            return binary
        _btc_log("rebuilding btcsniffer: %s", second_reason)
        if build_dir.exists() and not _btcsniffer_cache_matches_source(build_dir):
            _btc_log("removing stale btcsniffer build directory: %s", build_dir)
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        configure = subprocess.run(
            [cmake, "-S", str(BTC_SNIFFER_ROOT), "-B", str(build_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if configure.returncode != 0:
            raise RuntimeError(
                "btcsniffer cmake configure failed\n"
                f"stdout:\n{configure.stdout[-4000:]}\n"
                f"stderr:\n{configure.stderr[-4000:]}"
            )
        jobs = os.getenv("BTC_SNIFFER_BUILD_JOBS", str(max(1, min(4, os.cpu_count() or 1))))
        build = subprocess.run(
            [cmake, "--build", str(build_dir), "--parallel", jobs],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if build.returncode != 0:
            raise RuntimeError(
                "btcsniffer build failed\n"
                f"stdout:\n{build.stdout[-4000:]}\n"
                f"stderr:\n{build.stderr[-4000:]}"
            )
        built_binary = BTC_SNIFFER_ROOT / "build" / "btcexplorer-sniffer"
        if not built_binary.exists():
            raise RuntimeError(f"btcsniffer build completed but binary is missing: {built_binary}")
        built_binary.chmod(built_binary.stat().st_mode | 0o111)
        arch_ok, arch_detail = _binary_arch_matches_host(built_binary)
        if not arch_ok:
            raise RuntimeError(f"btcsniffer rebuilt but architecture still mismatches host: {arch_detail}")
        _btc_log("btcsniffer rebuild complete: %s", built_binary)
        return built_binary


def _ensure_btcsniffer_binary() -> Path:
    binary = _btcsniffer_binary()
    reason = _btcsniffer_rebuild_reason(binary)
    if reason is None:
        return binary
    return _build_btcsniffer_binary(reason)


def _btcsniffer_driver_from_device(device_id: str) -> str:
    lowered = device_id.lower()
    if lowered.startswith("bladerf"):
        return "bladerf"
    if lowered.startswith("hackrf"):
        return "hackrf"
    if lowered.startswith("sidekiq"):
        return "sidekiq"
    return "bladerf"


def _btc_max_bandwidth_mhz_for_device(device_id: str) -> int:
    driver = _btcsniffer_driver_from_device(device_id)
    if driver == "hackrf":
        return 20
    if driver == "bladerf":
        return 60
    if driver == "sidekiq":
        return 60
    return 20


def _device_max_rate_mhz(device: dict[str, Any]) -> int:
    try:
        rate = int(round(float(device.get("max_sample_rate_sps") or 0) / 1_000_000.0))
    except (TypeError, ValueError):
        rate = 0
    if rate > 0:
        return rate
    return _btc_max_bandwidth_mhz_for_device(str(device.get("id") or ""))


def _pick_ism24_bluetooth_device(devices: list[dict[str, Any]], allowed_devices: set[str] | None = None) -> str:
    candidates = [
        dev
        for dev in devices
        if str(dev.get("id") or "").strip()
        and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
        and int(dev.get("freq_min_hz") or 0) <= 2_402_000_000
        and int(dev.get("freq_max_hz") or 0) >= 2_480_000_000
    ]
    if not candidates:
        candidates = [
            dev
            for dev in devices
            if str(dev.get("id") or "").strip()
            and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
        ]
    if not candidates:
        return ""
    wide = [dev for dev in candidates if _device_max_rate_mhz(dev) >= 60]
    pool = wide or candidates
    best = max(pool, key=lambda dev: (_device_max_rate_mhz(dev), "bladerf" in str(dev.get("id") or dev.get("label") or "").lower()))
    return str(best.get("id") or "")


def _pick_non_bluetooth_hop_device(
    devices: list[dict[str, Any]],
    bluetooth_device_id: str,
    allowed_devices: set[str] | None = None,
) -> str:
    blocked = str(bluetooth_device_id or "").strip()
    candidates = [
        dev
        for dev in devices
        if str(dev.get("id") or "").strip()
        and str(dev.get("id") or "").strip() != blocked
        and str(dev.get("id") or "").strip() != "wlan0"
        and (not allowed_devices or str(dev.get("id") or "").strip() in allowed_devices)
    ]
    if not candidates:
        return ""
    bluetooth_driver = str(bluetooth_device_id or "").split(":", 1)[0].lower()
    if bluetooth_driver == "bladerf":
        extra_bladerf = _pick_device(candidates, "bladerf")
        if extra_bladerf:
            return extra_bladerf
    return _pick_device(candidates, "hackrf", "sidekiq")


def _tail_text(path: Path, max_lines: int = 20) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def _classic_center_for_channel(channel: int) -> int:
    return BT_CLASSIC_CHANNELS.get(channel, BT_CLASSIC_CHANNELS[0])


def _btcsniffer_event_from_line(line: str, center_freq_hz: int, bank_start_channel: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.time()
    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    prefix = re.search(r"\[\s*(?P<channel>\d+)\]\s+(?P<ts>\d+)\s+us\s+--\s+(?P<lap>[0-9A-Fa-f]{6})\s+--\s+(?P<msg>.*)", line)
    if not prefix:
        return events, candidates

    bin_index = int(prefix.group("channel"))
    channel = bank_start_channel + bin_index
    ts_us = int(prefix.group("ts"))
    lap = prefix.group("lap").upper()
    msg = prefix.group("msg").strip()
    freq_hz = _classic_center_for_channel(channel)

    resolved = re.search(
        r"RESOLVED UAP:LAP\s+(?P<uap>[0-9A-Fa-f]{2}):(?P<lap>[0-9A-Fa-f]{6})(?:.*tracking(?:\s+for)?\s+(?P<tracking>\d+)\s+us)?",
        msg,
    )
    if resolved:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": resolved.group("lap").upper(),
            "uap": resolved.group("uap").upper(),
            "status": "resolved",
            "candidate_count": 1,
            "processed_packets": 1,
            "ts_us": ts_us,
            "tracking_us": int(resolved.group("tracking") or 0),
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 0.99})
        return events, candidates

    two_left = re.search(
        r"Only two UAP left \((?P<uap0>[0-9A-Fa-f]{2}) and (?P<uap1>[0-9A-Fa-f]{2})\).*tracking for\s+(?P<tracking>\d+)\s+us",
        msg,
    )
    if two_left:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": 2,
            "processed_packets": 1,
            "ts_us": ts_us,
            "tracking_us": int(two_left.group("tracking")),
        }
        events.append(event)
        candidates.append(
            {
                **event,
                "uap_hex": f"{two_left.group('uap0').upper()} / {two_left.group('uap1').upper()}",
                "score": 0.82,
                "notes": [f"btcsniffer narrowed LAP {lap} to two UAPs."],
            }
        )
        return events, candidates

    narrowed = re.search(r"(?P<count>\d+)\s+possible UAPs remaining\s+\[(?P<uaps>[0-9A-Fa-f ]+)\]", msg)
    if narrowed:
        count = int(narrowed.group("count"))
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": count,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": "Pending", "score": 0.68, "notes": [f"Remaining UAPs: {narrowed.group('uaps').strip()}"]})
        return events, candidates

    if "Initialized" in msg:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": lap,
            "uap": None,
            "status": "initialized",
            "candidate_count": 32,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        return events, candidates

    init_failed = re.search(r"lap init failed lap=(?P<lap>[0-9A-Fa-f]{6}) channel=(?P<channel>\d+) ts_us=(?P<ts>\d+) valid_uaps=(?P<valid>\d+)", msg)
    if init_failed:
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": init_failed.group("lap").upper(),
            "uap": None,
            "status": "init_failed",
            "candidate_count": int(init_failed.group("valid")),
            "processed_packets": 1,
            "cannot_init": 1,
            "ts_us": int(init_failed.group("ts")),
        }
        events.append(event)
        return events, candidates

    fhs = re.search(r"PASSIVE FHS BD_ADDR\s+(?P<addr>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", msg)
    if fhs:
        addr = fhs.group("addr").upper()
        parts = addr.split(":")
        event = {
            "kind": "classic_lap",
            "protocol": "BTC",
            "source": "btcsniffer",
            "seen_at": now,
            "channel": channel,
            "btcsniffer_bin": bin_index,
            "center_freq_hz": freq_hz,
            "bank_center_freq_hz": center_freq_hz,
            "rssi_dbfs": -120.0,
            "lap": "".join(parts[3:6]),
            "uap": parts[2],
            "nap": "".join(parts[0:2]),
            "mac": addr,
            "status": "passive_fhs",
            "candidate_count": 1,
            "processed_packets": 1,
            "ts_us": ts_us,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 1.0})
        return events, candidates

    return events, candidates


def _btcsniffer_event_from_json(payload: dict[str, Any], center_freq_hz: int, bank_start_channel: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    event_type = str(payload.get("type") or "")
    now = time.time()
    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    if event_type == "metrics":
        access_hits = int(payload.get("access_hits") or 0)
        lap_events = int(payload.get("lap_events") or 0)
        resolved_events = int(payload.get("resolved_events") or 0)
        fhs_events = int(payload.get("fhs_events") or 0)
        with state_lock:
            state.decoder_stats["preamble_hits"] = int(payload.get("preamble_hits") or 0)
            state.decoder_stats["barker_hits"] = int(payload.get("barker_hits") or 0)
            state.decoder_stats["access_code_hits"] = access_hits
            state.decoder_stats["access_code_mismatch"] = int(payload.get("access_rejects") or 0)
            state.decoder_stats["lap_hits"] = lap_events + resolved_events + fhs_events
            state.decoder_stats["btcsniffer_packets_seen"] = int(payload.get("packets_seen") or 0)
            state.decoder_stats["btcsniffer_samples_processed"] = int(payload.get("samples_processed") or 0)
            state.decoder_stats["btcsniffer_solved_laps"] = int(payload.get("solved_laps") or 0)
            state.decoder_stats["btcsniffer_active_laps"] = int(payload.get("active_laps") or 0)
            state.decoder_stats["btcsniffer_bins"] = int(payload.get("bins") or 0)
            state.decoder_stats["fhs_attempts"] = int(payload.get("fhs_attempts") or 0)
            state.decoder_stats["fhs_inquiry_attempts"] = int(payload.get("fhs_inquiry_attempts") or 0)
            state.decoder_stats["fhs_solved_lap_attempts"] = int(payload.get("fhs_solved_lap_attempts") or 0)
            state.decoder_stats["fhs_truncated"] = int(payload.get("fhs_truncated") or 0)
            state.decoder_stats["fhs_header_matches"] = int(payload.get("fhs_header_matches") or 0)
            state.decoder_stats["fhs_type_matches"] = int(payload.get("fhs_type_matches") or 0)
            state.decoder_stats["fhs_payload_decodes"] = int(payload.get("fhs_payload_decodes") or 0)
            state.decoder_stats["fhs_fec_rejects"] = int(payload.get("fhs_fec_rejects") or 0)
            state.decoder_stats["fhs_address_rejects"] = int(payload.get("fhs_address_rejects") or 0)
            state.decoder_stats["fhs_packet_types"] = list(payload.get("fhs_packet_types") or [])
            state.classic_bursts_seen = max(state.classic_bursts_seen, lap_events + resolved_events + fhs_events)
        return events, candidates

    if event_type == "config":
        with state_lock:
            state.decoder_stats["btcsniffer_bins"] = int(payload.get("bins") or 0)
            state.decoder_stats["btcsniffer_sample_rate"] = int(float(payload.get("sample_rate") or 0))
        return events, candidates

    try:
        bin_index = int(payload.get("channel"))
    except (TypeError, ValueError):
        bin_index = 0
    channel = bank_start_channel + bin_index
    freq_hz = _classic_center_for_channel(channel)
    lap = str(payload.get("lap") or "").upper()
    ts_us = int(payload.get("ts_us") or 0)
    rssi_dbfs = float(payload.get("rssi_dbfs", -120.0))

    base = {
        "kind": "classic_lap",
        "protocol": "BTC",
        "source": "btcexplorer-sniffer",
        "seen_at": now,
        "channel": channel,
        "btcsniffer_bin": bin_index,
        "center_freq_hz": freq_hz,
        "bank_center_freq_hz": center_freq_hz,
        "rssi_dbfs": round(rssi_dbfs, 1),
        "lap": lap,
        "ts_us": ts_us,
        "processed_packets": 1,
    }

    if event_type == "lap_initialized":
        event = {**base, "uap": None, "status": "initialized", "candidate_count": int(payload.get("candidate_count") or 32)}
        events.append(event)
        return events, candidates

    if event_type == "lap_narrowed":
        event = {**base, "uap": None, "status": "brute_forcing", "candidate_count": int(payload.get("candidate_count") or 0)}
        events.append(event)
        candidates.append({**event, "uap_hex": "Pending", "score": 0.68, "notes": [f"Remaining UAPs: {payload.get('uaps') or ''}"]})
        return events, candidates

    if event_type == "lap_two_uap_left":
        event = {
            **base,
            "uap": None,
            "status": "brute_forcing",
            "candidate_count": 2,
            "tracking_us": int(payload.get("tracking_us") or 0),
        }
        events.append(event)
        candidates.append({**event, "uap_hex": f"{payload.get('uap0')} / {payload.get('uap1')}", "score": 0.82})
        return events, candidates

    if event_type == "page_access_seen":
        event = {
            **base,
            "uap": None,
            "status": str(payload.get("status") or "page_access"),
            "candidate_count": int(payload.get("candidate_count") or 0),
            "detail": "page/inquiry access code observed",
            "notes": [f"Bluetooth Classic access code for LAP {lap} observed on channel {channel}."],
        }
        events.append(event)
        return events, candidates

    if event_type in {"lap_resolved", "lap_seen"}:
        uap = str(payload.get("uap") or "").upper()
        event = {
            **base,
            "uap": uap,
            "status": "seen" if event_type == "lap_seen" else "resolved",
            "candidate_count": 1,
            "tracking_us": int(payload.get("tracking_us") or 0),
        }
        events.append(event)
        if event_type == "lap_resolved":
            candidates.append({**event, "uap_hex": uap, "score": 0.99})
        return events, candidates

    if event_type == "passive_fhs_bdaddr":
        address = str(payload.get("address") or "").upper()
        event = {
            **base,
            "uap": str(payload.get("uap") or "").upper(),
            "nap": str(payload.get("nap") or "").upper(),
            "mac": address,
            "status": "passive_fhs",
            "candidate_count": 1,
        }
        events.append(event)
        candidates.append({**event, "uap_hex": event["uap"], "score": 1.0})
        return events, candidates

    return events, candidates


def _btcsniffer_loop(proc: subprocess.Popen[str], center_freq_hz: int, bank_start_channel: int) -> None:
    _btc_log(
        "sniffer loop attached center=%.3f MHz bank_start=%d pid=%s",
        float(center_freq_hz) / 1_000_000.0,
        bank_start_channel,
        proc.pid,
    )
    with state_lock:
        state.worker_alive_by_mode["classic"] = True
        state.worker_alive = True
        state.worker_errors["classic"] = ""
        state.worker_error = ""
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if btc_engine_stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            events: list[dict[str, Any]] = []
            candidates: list[dict[str, Any]] = []
            json_start = line.find("{")
            if json_start < 0:
                _btc_log("%s", line)
            if json_start > 0:
                text_part = line[:json_start].strip()
                json_part = line[json_start:].strip()
                if text_part:
                    _btc_log("%s", text_part)
                    text_events, text_candidates = _btcsniffer_event_from_line(text_part, center_freq_hz, bank_start_channel)
                    events.extend(text_events)
                    candidates.extend(text_candidates)
                try:
                    json_events, json_candidates = _btcsniffer_event_from_json(json.loads(json_part), center_freq_hz, bank_start_channel)
                    events.extend(json_events)
                    candidates.extend(json_candidates)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            elif line.startswith("{"):
                try:
                    events, candidates = _btcsniffer_event_from_json(json.loads(line), center_freq_hz, bank_start_channel)
                except (json.JSONDecodeError, TypeError, ValueError):
                    events, candidates = [], []
            else:
                events, candidates = _btcsniffer_event_from_line(line, center_freq_hz, bank_start_channel)
            with state_lock:
                state.chunks_seen += 1
                state.chunks_by_mode["classic"] = int(state.chunks_by_mode.get("classic", 0)) + 1
                state.last_rssi_dbfs = state.rssi_by_mode.get("classic", state.last_rssi_dbfs)
                state.decoder_stats["btcsniffer_lines"] = int(state.decoder_stats.get("btcsniffer_lines", 0)) + 1
            _append_detections(events, candidates)
    except Exception as exc:
        _btc_log("sniffer loop error: %s", exc, level=logging.ERROR)
        with state_lock:
            state.worker_errors["classic"] = f"btcsniffer error: {exc}"
            state.worker_error = f"btcsniffer error: {exc}"
    finally:
        rc = proc.poll()
        _btc_log("sniffer loop exiting pid=%s rc=%s stop=%s", proc.pid, rc, int(btc_engine_stop.is_set()))
        with state_lock:
            state.worker_alive_by_mode["classic"] = False
            state.worker_alive = any(state.worker_alive_by_mode.values())
            if not btc_engine_stop.is_set() and rc not in {None, 0}:
                state.worker_errors["classic"] = f"btcsniffer exited with code {rc}"
                state.worker_error = f"btcsniffer exited with code {rc}"


def _start_btcsniffer_engine(device_id: str, center_freq_hz: int, bandwidth_mhz: int, bank_start_channel: int) -> dict[str, Any]:
    global btc_engine_process, btc_engine_thread
    binary = _ensure_btcsniffer_binary()
    if not binary.exists():
        raise RuntimeError(f"btcsniffer binary not found: {binary}")
    BTC_SNIFFER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(binary),
        "--driver",
        _btcsniffer_driver_from_device(device_id),
        "--freq-mhz",
        f"{center_freq_hz / 1_000_000.0:.3f}MHz",
        "--bandwidth-mhz",
        f"{int(bandwidth_mhz)}MHz",
        "--log",
        str(BTC_SNIFFER_LOG_PATH),
        "--jsonl-stdout",
    ]
    _btc_log(
        "launch device=%s driver=%s center=%.3f MHz bandwidth=%d MHz bank_start=%d binary=%s",
        device_id,
        _btcsniffer_driver_from_device(device_id),
        float(center_freq_hz) / 1_000_000.0,
        int(bandwidth_mhz),
        int(bank_start_channel),
        binary,
    )
    _btc_log("command: %s", " ".join(cmd))
    btc_engine_stop.clear()
    proc = subprocess.Popen(
        cmd,
        cwd=str(BTC_SNIFFER_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    btc_engine_process = proc
    btc_engine_thread = threading.Thread(target=_btcsniffer_loop, args=(proc, center_freq_hz, bank_start_channel), daemon=True)
    btc_engine_thread.start()
    time.sleep(0.25)
    if proc.poll() not in {None, 0}:
        log_tail = _tail_text(BTC_SNIFFER_LOG_PATH)
        detail = f"btcsniffer exited immediately with code {proc.returncode}"
        if log_tail:
            detail = f"{detail}\n{log_tail}"
        _btc_log("launch failed: %s", detail, level=logging.ERROR)
        raise RuntimeError(detail)
    return {
        "engine": "btcsniffer",
        "stream_id": "btcsniffer",
        "device_id": device_id,
        "center_freq_hz": center_freq_hz,
        "sample_rate_sps": int(bandwidth_mhz) * 1_000_000,
        "lna_gain_db": 0,
        "vga_gain_db": 0,
        "channel": int(bank_start_channel),
        "body": {"engine": "btcsniffer", "command": cmd, "log": str(BTC_SNIFFER_LOG_PATH)},
    }


def _stop_btcsniffer_engine() -> None:
    global btc_engine_process, btc_engine_thread
    proc = btc_engine_process
    btc_engine_process = None
    btc_engine_stop.set()
    if proc is not None:
        _btc_log("stop requested pid=%s", proc.pid)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    if btc_engine_thread and btc_engine_thread.is_alive():
        btc_engine_thread.join(timeout=2)
    _btc_log("stop complete")
    btc_engine_thread = None


def _gateway_streams() -> list[dict[str, Any]]:
    try:
        resp = requests.get(f"{_gateway_base()}/streams", headers=_gateway_headers(), timeout=5)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        return body if isinstance(body, list) else []
    except requests.RequestException:
        return []


def _gateway_stream_for_device(device_id: str | None) -> dict[str, Any] | None:
    requested = str(device_id or "").strip()
    if not requested:
        return None
    for stream in _gateway_streams():
        cfg = stream.get("config", {}) or {}
        if str(cfg.get("device_id", "")).strip() == requested:
            return stream
    return None


def _gateway_iq_sweeps() -> list[dict[str, Any]]:
    body = _gateway_get_json("/iq-sweeps")
    return body if isinstance(body, list) else []


def _gateway_live_centers_by_device() -> dict[str, int]:
    centers: dict[str, int] = {}
    try:
        for stream in _gateway_streams():
            if str(stream.get("status") or "").lower() not in {"running", "retuning"}:
                continue
            cfg = stream.get("config", {}) or {}
            device_id = str(cfg.get("device_id") or "").strip()
            center_hz = int(cfg.get("center_freq_hz") or 0)
            if device_id and center_hz > 0:
                centers[device_id] = center_hz
    except Exception:
        pass
    try:
        for sweep in _gateway_iq_sweeps():
            if str(sweep.get("status") or "").lower() not in {"running", "retuning"}:
                continue
            cfg = sweep.get("config", {}) or {}
            device_id = str(cfg.get("device_id") or "").strip()
            center_hz = int(sweep.get("current_center_freq_hz") or cfg.get("center_freq_hz") or 0)
            if device_id and center_hz > 0:
                centers[device_id] = center_hz
    except Exception:
        pass
    return centers


def _sync_scanner_assignment_centers_from_gateway() -> dict[str, int]:
    centers_by_device = _gateway_live_centers_by_device()
    if not centers_by_device:
        return {}
    with state_lock:
        for assignment in state.scanner_assignments.values():
            device_id = str(assignment.get("device_id") or "").strip()
            center_hz = centers_by_device.get(device_id)
            if center_hz and center_hz > 0:
                assignment["last_center_freq_hz"] = center_hz
    return centers_by_device


def _stop_gateway_stream(stream_id: str | None) -> None:
    if not stream_id:
        return
    try:
        requests.post(f"{_gateway_base()}/streams/{stream_id}/stop", headers=_gateway_headers(), timeout=3)
    except requests.RequestException:
        pass


def _stop_duplicate_gateway_streams(device_id: str | None, keep_stream_id: str | None = None) -> None:
    if not device_id:
        return
    for stream in _gateway_streams():
        stream_id = str(stream.get("stream_id", "")).strip()
        cfg = stream.get("config", {}) or {}
        if stream_id and stream_id != keep_stream_id and str(cfg.get("device_id", "")).strip() == device_id:
            _stop_gateway_stream(stream_id)


def _gateway_get_json(path: str) -> Any:
    resp = requests.get(f"{_gateway_base()}{path}", headers=_gateway_headers(), timeout=5)
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _gateway_stop_path(path: str) -> None:
    try:
        requests.post(f"{_gateway_base()}{path}", headers=_gateway_headers(), timeout=3)
    except requests.RequestException:
        pass


def _force_release_gateway_device(device_id: str) -> None:
    requested = str(device_id or "").strip()
    if not requested:
        return
    stopped = 0
    devices = _gateway_get_json("/devices")
    if isinstance(devices, list):
        for device in devices:
            if str(device.get("id") or "").strip() != requested:
                continue
            owner = str(device.get("occupied_by") or "").strip()
            owner_id = str(device.get("occupied_id") or "").strip()
            if owner == "stream" and owner_id:
                _gateway_stop_path(f"/streams/{owner_id}/stop")
                stopped += 1
            elif owner == "sweep" and owner_id:
                _gateway_stop_path(f"/sweeps/{owner_id}/stop")
                stopped += 1
            elif owner == "iq_sweep" and owner_id:
                _gateway_stop_path(f"/iq-sweeps/{owner_id}/stop")
                stopped += 1
            elif owner == "tx" and owner_id:
                _gateway_stop_path(f"/tx/{owner_id}/stop")
                stopped += 1
    for path, id_key, stop_prefix in (
        ("/streams", "stream_id", "/streams"),
        ("/sweeps", "sweep_id", "/sweeps"),
        ("/iq-sweeps", "iq_sweep_id", "/iq-sweeps"),
        ("/tx", "tx_id", "/tx"),
    ):
        sessions = _gateway_get_json(path)
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            cfg = session.get("config") if isinstance(session.get("config"), dict) else {}
            if str(cfg.get("device_id") or "").strip() != requested:
                continue
            session_id = str(session.get(id_key) or "").strip()
            if session_id:
                _gateway_stop_path(f"{stop_prefix}/{session_id}/stop")
                stopped += 1
    if stopped:
        with state_lock:
            _append_scanner_log(f"[ui] force-released {requested} gateway sessions for FM playback")


def _drain_fm_audio_queue() -> None:
    while not fm_audio_q.empty():
        try:
            fm_audio_q.get_nowait()
        except queue.Empty:
            break


def _drain_walkie_audio_queue() -> None:
    while not walkie_audio_q.empty():
        try:
            walkie_audio_q.get_nowait()
        except queue.Empty:
            break


def _append_walkie_recent_audio(pcm: bytes) -> None:
    if not pcm:
        return
    now = time.time()
    with walkie_recent_lock:
        if not walkie_recent_audio:
            walkie_playback.recent_started_at = now
        walkie_recent_audio.append(bytes(pcm))
        walkie_playback.recent_chunks = len(walkie_recent_audio)
        walkie_playback.recent_updated_at = now


def _fm_playback_status_payload() -> dict[str, Any]:
    return {
        "running": fm_playback.running,
        "pending": fm_playback.pending,
        "pending_freq_mhz": fm_playback.pending_freq_mhz,
        "pending_device_id": fm_playback.pending_device_id,
        "device_id": fm_playback.device_id,
        "freq_mhz": fm_playback.freq_mhz,
        "sample_rate_sps": fm_playback.sample_rate_sps,
        "lna_gain_db": fm_playback.lna_gain_db,
        "vga_gain_db": fm_playback.vga_gain_db,
        "stream_id": fm_playback.stream_id,
        "worker_alive": fm_playback.worker_alive,
        "worker_error": fm_playback.worker_error,
        "last_audio_rms": fm_playback.last_audio_rms,
        "produced_chunks": fm_playback.produced_chunks,
        "served_chunks": fm_playback.served_chunks,
        "queued_chunks": fm_audio_q.qsize(),
    }


def _walkie_playback_status_payload() -> dict[str, Any]:
    return {
        "running": walkie_playback.running,
        "pending": walkie_playback.pending,
        "pending_freq_mhz": walkie_playback.pending_freq_mhz,
        "pending_device_id": walkie_playback.pending_device_id,
        "device_id": walkie_playback.device_id,
        "freq_mhz": walkie_playback.freq_mhz,
        "sample_rate_sps": walkie_playback.sample_rate_sps,
        "lna_gain_db": walkie_playback.lna_gain_db,
        "vga_gain_db": walkie_playback.vga_gain_db,
        "stream_id": walkie_playback.stream_id,
        "worker_alive": walkie_playback.worker_alive,
        "worker_error": walkie_playback.worker_error,
        "last_audio_rms": walkie_playback.last_audio_rms,
        "produced_chunks": walkie_playback.produced_chunks,
        "served_chunks": walkie_playback.served_chunks,
        "queued_chunks": walkie_audio_q.qsize(),
        "recent_chunks": walkie_playback.recent_chunks,
        "recent_started_at": walkie_playback.recent_started_at,
        "recent_updated_at": walkie_playback.recent_updated_at,
    }


def _fm_busy_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "resource busy" in text or "already in use" in text or "409" in text


def _runtime_enabled_protocols() -> set[str]:
    protocols = state.decoder_stats.get("enabled_protocols")
    if isinstance(protocols, list):
        live = {str(item).strip().lower() for item in protocols} & RF_SENTINEL_PROTOCOLS
        if live:
            return live
    control = _read_rf_sentinel_control()
    control_protocols = control.get("protocols")
    if isinstance(control_protocols, list):
        live = {str(item).strip().lower() for item in control_protocols} & RF_SENTINEL_PROTOCOLS
        if live:
            return live
    return set(_read_ui_config().get("protocols", [])) & RF_SENTINEL_PROTOCOLS


def _current_fm_scanner_device_id() -> str:
    assignments = dict(state.scanner_assignments or {})
    for assignment in assignments.values():
        if str(assignment.get("protocol") or "").lower() == "fm":
            device_id = str(assignment.get("device_id") or "").strip()
            if device_id:
                return device_id
    hop_device = str(state.device_ids.get("hop") or state.device_ids.get("radio_b") or "").strip()
    if hop_device:
        return hop_device
    return ""


def _current_walkie_scanner_device_id() -> str:
    assignments = dict(state.scanner_assignments or {})
    for protocol_name in ("walkie", "tpms"):
        for assignment in assignments.values():
            if str(assignment.get("protocol") or "").lower() == protocol_name:
                device_id = str(assignment.get("device_id") or "").strip()
                if device_id:
                    return device_id
    protocol_devices = _read_ui_config().get("protocol_devices", {})
    if isinstance(protocol_devices, dict):
        device_id = str(protocol_devices.get("tpms") or protocol_devices.get("walkie") or "").strip()
        if device_id:
            return device_id
    hop_device = str(state.device_ids.get("hop") or state.device_ids.get("radio_b") or "").strip()
    if hop_device:
        return hop_device
    return "hackrf:0"


def _pause_fm_scanner_for_playback() -> None:
    protocols = _runtime_enabled_protocols()
    if "fm" not in protocols:
        return
    protocols.discard("fm")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        fm_playback.scanner_protocol_paused = True
        _append_scanner_log("[ui] FM scanner paused for playback lock")


def _restore_fm_scanner_after_playback() -> None:
    if not fm_playback.scanner_protocol_paused:
        return
    fm_playback.scanner_protocol_paused = False
    protocols = _runtime_enabled_protocols()
    saved_protocols = set(_read_ui_config().get("protocols", [])) & RF_SENTINEL_PROTOCOLS
    if "fm" not in saved_protocols:
        return
    protocols.add("fm")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        _append_scanner_log("[ui] FM scanner restored after playback")


def _pause_walkie_scanner_for_playback() -> None:
    protocols = _runtime_enabled_protocols()
    if not ({"tpms", "walkie"} & protocols):
        return
    protocols.discard("tpms")
    protocols.discard("walkie")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        walkie_playback.scanner_protocol_paused = True
        _append_scanner_log("[ui] Sub-GHz scanner paused for walkie playback")


def _restore_walkie_scanner_after_playback() -> None:
    if not walkie_playback.scanner_protocol_paused:
        return
    walkie_playback.scanner_protocol_paused = False
    protocols = _runtime_enabled_protocols()
    saved_protocols = set(_read_ui_config().get("protocols", [])) & RF_SENTINEL_PROTOCOLS
    if "tpms" in saved_protocols:
        protocols.add("tpms")
    if "walkie" in saved_protocols:
        protocols.add("walkie")
    existing = _read_rf_sentinel_control()
    devices = existing.get("devices") if isinstance(existing.get("devices"), list) else None
    enabled_devices = {str(item).strip() for item in devices if str(item).strip()} if devices is not None else None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE,
    )
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = _follow_state_for_protocols(control, protocols)
        _append_scanner_log("[ui] Sub-GHz scanner restored after walkie playback")


def _device_available(device_id: str) -> bool:
    requested = str(device_id or "").strip()
    if not requested:
        return False
    for device in _available_devices():
        if str(device.get("id") or "").strip() == requested:
            return not bool(device.get("occupied"))
    return False


def _wait_for_device_available(device_id: str, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        if _device_available(device_id):
            return True
        time.sleep(0.15)
    return _device_available(device_id)


def _preferred_lock_device(prefer_free: bool = True) -> str:
    devices = _available_devices()
    for predicate in (_is_rtlsdr_device,):
        for device in devices:
            dev_id = str(device.get("id") or "").strip()
            if not dev_id or not predicate(device):
                continue
            if not prefer_free or not bool(device.get("occupied")):
                return dev_id
    return ""


def _preferred_fm_playback_device(requested_device_id: str = "") -> str:
    devices = _available_devices()
    requested = str(requested_device_id or "").strip()
    if requested:
        for device in devices:
            if str(device.get("id") or "").strip() == requested and not bool(device.get("occupied")):
                return requested
        raise RuntimeError(f"resource busy: SDR {requested} is not free for FM playback")
    rtl_device = _preferred_lock_device(prefer_free=True)
    if rtl_device:
        return rtl_device
    for preferred in ("hackrf", "sidekiq", "bladerf"):
        for device in devices:
            dev_id = str(device.get("id") or "").strip()
            haystack = f"{dev_id} {str(device.get('label') or '')}".lower()
            if preferred in haystack and not bool(device.get("occupied")):
                return dev_id
    for device in devices:
        dev_id = str(device.get("id") or "").strip()
        if dev_id and not bool(device.get("occupied")):
            return dev_id
    raise RuntimeError("No free SDR is available for FM playback")


def _start_fm_playback_now(freq_mhz: float, requested_device_id: str = "") -> None:
    global fm_worker_thread
    requested = str(requested_device_id or "").strip() or _preferred_lock_device(prefer_free=False) or _current_fm_scanner_device_id()
    active_stream = _gateway_stream_for_device(requested)
    if requested and not _device_available(requested):
        _pause_fm_scanner_for_playback()
        if active_stream is None:
            _force_release_gateway_device(requested)
            _wait_for_device_available(requested, timeout_s=2.0)
            active_stream = _gateway_stream_for_device(requested)
    picked_device_id = requested if active_stream is not None else _preferred_fm_playback_device(requested)
    target_freq_hz = int(round(float(freq_mhz) * 1_000_000.0))
    target_rate = 2_000_000
    target_lna = 32
    target_vga = 32
    if active_stream is not None:
        stream_id = str(active_stream.get("stream_id") or "").strip()
        body, actual_rate, actual_lna, actual_vga = _retune_gateway_stream(
            stream_id,
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
        )
    else:
        _stop_duplicate_gateway_streams(picked_device_id)
        body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
        )
    _drain_fm_audio_queue()
    stream_id = str(body.get("stream_id") or "")
    if fm_playback.running and fm_playback.stream_id == stream_id and fm_worker_thread and fm_worker_thread.is_alive():
        fm_playback.pending = False
        fm_playback.pending_freq_mhz = 0.0
        fm_playback.pending_device_id = ""
        fm_playback.device_id = picked_device_id
        fm_playback.freq_mhz = float(freq_mhz)
        fm_playback.sample_rate_sps = actual_rate
        fm_playback.lna_gain_db = actual_lna
        fm_playback.vga_gain_db = actual_vga
        fm_playback.worker_error = ""
        fm_playback.last_audio_rms = 0.0
        fm_playback.produced_chunks = 0
        fm_playback.served_chunks = 0
        fm_playback.empty_audio_polls = 0
        return
    fm_worker_stop.clear()
    fm_playback.running = True
    fm_playback.pending = False
    fm_playback.pending_freq_mhz = 0.0
    fm_playback.pending_device_id = ""
    fm_playback.device_id = picked_device_id
    fm_playback.freq_mhz = float(freq_mhz)
    fm_playback.sample_rate_sps = actual_rate
    fm_playback.lna_gain_db = actual_lna
    fm_playback.vga_gain_db = actual_vga
    fm_playback.stream_id = stream_id
    fm_playback.worker_error = ""
    fm_playback.last_audio_rms = 0.0
    fm_playback.produced_chunks = 0
    fm_playback.served_chunks = 0
    fm_playback.empty_audio_polls = 0
    fm_worker_thread = threading.Thread(target=_fm_worker_loop, args=(fm_playback.stream_id, actual_rate), daemon=True)
    fm_worker_thread.start()


def _stop_fm_playback() -> None:
    global fm_worker_thread, fm_request_serial
    fm_request_serial += 1
    fm_worker_stop.set()
    if fm_worker_thread and fm_worker_thread.is_alive():
        fm_worker_thread.join(timeout=2.0)
    fm_worker_thread = None
    if fm_playback.stream_id:
        _stop_gateway_stream(fm_playback.stream_id)
    _drain_fm_audio_queue()
    fm_playback.running = False
    fm_playback.pending = False
    fm_playback.pending_freq_mhz = 0.0
    fm_playback.pending_device_id = ""
    fm_playback.device_id = ""
    fm_playback.freq_mhz = 0.0
    fm_playback.stream_id = ""
    fm_playback.worker_alive = False
    fm_playback.worker_error = ""
    fm_playback.last_audio_rms = 0.0
    fm_playback.produced_chunks = 0
    fm_playback.served_chunks = 0
    fm_playback.empty_audio_polls = 0


def _fm_worker_loop(stream_id: str, sample_rate_sps: int) -> None:
    demod = FmAudioDemod(sample_rate_sps)
    pcm_accum = bytearray()
    target_chunk_bytes = 16384
    headers = []
    token = _gateway_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
        headers.append(f"x-api-key: {token}")
    fm_playback.worker_alive = True
    fm_playback.worker_error = ""
    try:
        while not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
            ws = websocket.WebSocket()
            try:
                ws.connect(_ws_url_for_stream(stream_id), timeout=8, header=headers)
                ws.settimeout(1.0)
                while not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        fm_playback.worker_error = "FM websocket closed"
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    pcm = demod.process_iq_i8(bytes(chunk))
                    if not pcm:
                        continue
                    pcm_accum.extend(pcm)
                    if len(pcm_accum) < target_chunk_bytes:
                        continue
                    out = bytes(pcm_accum)
                    pcm_accum.clear()
                    audio_i16 = np.frombuffer(out, dtype=np.int16)
                    if audio_i16.size:
                        fm_playback.last_audio_rms = float(np.sqrt(np.mean((audio_i16.astype(np.float32) / 32768.0) ** 2)))
                    fm_playback.produced_chunks += 1
                    try:
                        fm_audio_q.put(out, timeout=0.1)
                    except queue.Full:
                        try:
                            fm_audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            fm_audio_q.put_nowait(out)
                        except queue.Full:
                            pass
            except Exception as exc:
                fm_playback.worker_error = f"FM websocket error: {exc}"
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            if not fm_worker_stop.is_set() and fm_playback.stream_id == stream_id:
                fm_worker_stop.wait(0.5)
    finally:
        if fm_playback.stream_id == stream_id:
            fm_playback.worker_alive = False


def _fm_pending_loop(request_serial: int, freq_mhz: float, requested_device_id: str) -> None:
    while request_serial == fm_request_serial and not fm_worker_stop.is_set():
        try:
            _start_fm_playback_now(freq_mhz, requested_device_id)
            return
        except Exception as exc:
            if not _fm_busy_error(exc):
                if request_serial == fm_request_serial:
                    fm_playback.pending = False
                    fm_playback.worker_error = f"FM start failed: {exc}"
                    _restore_fm_scanner_after_playback()
                return
            if request_serial == fm_request_serial:
                fm_playback.pending = True
                fm_playback.pending_freq_mhz = float(freq_mhz)
                fm_playback.pending_device_id = str(requested_device_id or "")
                fm_playback.worker_error = "FM waiting for SDR availability"
            time.sleep(0.5)


def _start_fm_pending_thread(request_serial: int, freq_mhz: float, device_id: str) -> None:
    global fm_pending_thread
    fm_pending_thread = threading.Thread(
        target=_fm_pending_loop,
        args=(request_serial, float(freq_mhz), device_id),
        daemon=True,
    )
    fm_pending_thread.start()


def _preferred_walkie_playback_device(requested_device_id: str = "") -> str:
    devices = _available_devices()
    requested = str(requested_device_id or "").strip()
    if requested:
        for device in devices:
            if str(device.get("id") or "").strip() == requested and not bool(device.get("occupied")):
                return requested
        raise RuntimeError(f"resource busy: SDR {requested} is not free for walkie playback")
    current = _current_walkie_scanner_device_id()
    for device in devices:
        if str(device.get("id") or "").strip() == current and not bool(device.get("occupied")):
            return current
    for preferred in ("hackrf", "rtlsdr", "sidekiq", "bladerf"):
        for device in devices:
            dev_id = str(device.get("id") or "").strip()
            haystack = f"{dev_id} {str(device.get('label') or '')} {str(device.get('driver') or '')}".lower()
            if preferred in haystack and not bool(device.get("occupied")):
                return dev_id
    for device in devices:
        dev_id = str(device.get("id") or "").strip()
        if dev_id and not bool(device.get("occupied")):
            return dev_id
    raise RuntimeError("No free SDR is available for walkie playback")


def _start_walkie_playback_now(freq_mhz: float = 462.5, requested_device_id: str = "") -> None:
    global walkie_worker_thread
    requested = str(requested_device_id or "").strip() or _current_walkie_scanner_device_id()
    active_stream = _gateway_stream_for_device(requested)
    if requested and not _device_available(requested):
        _pause_walkie_scanner_for_playback()
        if active_stream is None:
            _force_release_gateway_device(requested)
            _wait_for_device_available(requested, timeout_s=2.0)
            active_stream = _gateway_stream_for_device(requested)
    picked_device_id = requested if active_stream is not None else _preferred_walkie_playback_device(requested)
    target_freq_hz = int(round(float(freq_mhz) * 1_000_000.0))
    target_rate = 1_000_000
    target_filter = 250_000
    target_lna = 16
    target_vga = 20
    if active_stream is not None:
        stream_id = str(active_stream.get("stream_id") or "").strip()
        body, actual_rate, actual_lna, actual_vga = _retune_gateway_stream(
            stream_id,
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
            baseband_filter_hz=target_filter,
        )
    else:
        _stop_duplicate_gateway_streams(picked_device_id)
        body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
            picked_device_id,
            target_freq_hz,
            target_rate,
            target_lna,
            target_vga,
            baseband_filter_hz=target_filter,
        )
    _drain_walkie_audio_queue()
    stream_id = str(body.get("stream_id") or "")
    walkie_worker_stop.clear()
    walkie_playback.running = True
    walkie_playback.pending = False
    walkie_playback.pending_freq_mhz = 0.0
    walkie_playback.pending_device_id = ""
    walkie_playback.device_id = picked_device_id
    walkie_playback.freq_mhz = float(freq_mhz)
    walkie_playback.sample_rate_sps = actual_rate
    walkie_playback.lna_gain_db = actual_lna
    walkie_playback.vga_gain_db = actual_vga
    walkie_playback.stream_id = stream_id
    walkie_playback.worker_error = ""
    walkie_playback.last_audio_rms = 0.0
    walkie_playback.produced_chunks = 0
    walkie_playback.served_chunks = 0
    walkie_worker_thread = threading.Thread(target=_walkie_worker_loop, args=(stream_id, actual_rate), daemon=True)
    walkie_worker_thread.start()


def _stop_walkie_playback(stop_stream: bool = True) -> None:
    global walkie_worker_thread, walkie_request_serial
    walkie_request_serial += 1
    walkie_worker_stop.set()
    if walkie_worker_thread and walkie_worker_thread.is_alive():
        walkie_worker_thread.join(timeout=2.0)
    walkie_worker_thread = None
    if stop_stream and walkie_playback.stream_id:
        _stop_gateway_stream(walkie_playback.stream_id)
    _drain_walkie_audio_queue()
    walkie_playback.running = False
    walkie_playback.pending = False
    walkie_playback.pending_freq_mhz = 0.0
    walkie_playback.pending_device_id = ""
    walkie_playback.device_id = ""
    walkie_playback.stream_id = ""
    walkie_playback.worker_alive = False
    walkie_playback.worker_error = ""
    walkie_playback.last_audio_rms = 0.0
    walkie_playback.produced_chunks = 0
    walkie_playback.served_chunks = 0
    _restore_walkie_scanner_after_playback()


def _walkie_worker_loop(stream_id: str, sample_rate_sps: int) -> None:
    demod = FmAudioDemod(sample_rate_sps, out_rate=48_000, channel_cutoff_hz=25_000.0)
    pcm_accum = bytearray()
    target_chunk_bytes = 8192
    headers = []
    token = _gateway_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
        headers.append(f"x-api-key: {token}")
    walkie_playback.worker_alive = True
    walkie_playback.worker_error = ""
    try:
        while not walkie_worker_stop.is_set() and walkie_playback.stream_id == stream_id:
            ws = websocket.WebSocket()
            try:
                ws.connect(_ws_url_for_stream(stream_id), timeout=8, header=headers)
                ws.settimeout(1.0)
                while not walkie_worker_stop.is_set() and walkie_playback.stream_id == stream_id:
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        walkie_playback.worker_error = "Walkie websocket closed"
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    pcm = demod.process_iq_i8(bytes(chunk))
                    if not pcm:
                        continue
                    pcm_accum.extend(pcm)
                    if len(pcm_accum) < target_chunk_bytes:
                        continue
                    out = bytes(pcm_accum)
                    pcm_accum.clear()
                    audio_i16 = np.frombuffer(out, dtype=np.int16)
                    if audio_i16.size:
                        walkie_playback.last_audio_rms = float(np.sqrt(np.mean((audio_i16.astype(np.float32) / 32768.0) ** 2)))
                    walkie_playback.produced_chunks += 1
                    _append_walkie_recent_audio(out)
                    try:
                        walkie_audio_q.put(out, timeout=0.1)
                    except queue.Full:
                        try:
                            walkie_audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            walkie_audio_q.put_nowait(out)
                        except queue.Full:
                            pass
            except Exception as exc:
                walkie_playback.worker_error = f"Walkie websocket error: {exc}"
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            if not walkie_worker_stop.is_set() and walkie_playback.stream_id == stream_id:
                walkie_worker_stop.wait(0.5)
    finally:
        if walkie_playback.stream_id == stream_id:
            walkie_playback.worker_alive = False


def _walkie_pending_loop(request_serial: int, freq_mhz: float, requested_device_id: str) -> None:
    while request_serial == walkie_request_serial and not walkie_worker_stop.is_set():
        try:
            _start_walkie_playback_now(freq_mhz, requested_device_id)
            return
        except Exception as exc:
            if not _fm_busy_error(exc):
                if request_serial == walkie_request_serial:
                    walkie_playback.pending = False
                    walkie_playback.worker_error = f"Walkie start failed: {exc}"
                    _restore_walkie_scanner_after_playback()
                return
            if request_serial == walkie_request_serial:
                walkie_playback.pending = True
                walkie_playback.pending_freq_mhz = float(freq_mhz)
                walkie_playback.pending_device_id = str(requested_device_id or "")
                walkie_playback.worker_error = "Walkie waiting for SDR availability"
            time.sleep(0.5)


def _start_walkie_pending_thread(request_serial: int, freq_mhz: float, device_id: str) -> None:
    global walkie_pending_thread
    walkie_pending_thread = threading.Thread(
        target=_walkie_pending_loop,
        args=(request_serial, float(freq_mhz), device_id),
        daemon=True,
    )
    walkie_pending_thread.start()


def _printable_hex_text(hex_text: Any) -> str:
    try:
        raw = bytes.fromhex(str(hex_text or ""))
    except ValueError:
        return ""
    cleaned = bytes(value for value in raw if value in (9, 10, 13) or 32 <= value <= 126)
    return cleaned.decode("utf-8", errors="ignore").strip()


def _is_broadcast_or_multicast_mac(mac: str) -> bool:
    normalized = mac.strip().lower()
    if not normalized or normalized in {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}:
        return True
    try:
        first_octet = int(normalized.split(":", 1)[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first_octet & 0x01)


def _wifi_role(frame_type: str, source_mac: str, destination_mac: str, bssid: str, ssid: str) -> str:
    lowered = frame_type.lower()
    if "beacon" in lowered or "probe_response" in lowered:
        return "ap"
    if bssid and source_mac and source_mac.lower() == bssid.lower():
        return "ap"
    if "probe_request" in lowered:
        return "station"
    if source_mac and not _is_broadcast_or_multicast_mac(source_mac):
        return "station"
    if destination_mac and not _is_broadcast_or_multicast_mac(destination_mac):
        return "station"
    return "ap" if ssid or bssid else "station"


def _clean_wifi_ssid(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        return ""
    return "".join(char for char in text if char in "\t " or 32 <= ord(char) <= 126).strip()


def _real_rssi(value: Any) -> float | None:
    try:
        rssi = float(value)
    except (TypeError, ValueError):
        return None
    if rssi <= -119.9:
        return None
    return rssi


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _iso_from_epoch(value: Any) -> str:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        epoch = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
    millis = int((epoch - int(epoch)) * 1000)
    return f"{base}.{millis:03d}Z"


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _csv_protocol_key(event: dict[str, Any]) -> str:
    protocol = str(event.get("protocol") or "").strip().upper()
    if protocol == "BLE":
        return "BTLE"
    if protocol:
        return protocol
    return {
        "ble_adv": "BTLE",
        "classic_lap": "BTC",
        "zigbee_frame": "ZIGBEE",
        "tpms_frame": "TPMS",
        "walkie_signal": "WALKIE",
        "wifi_frame": "WIFI",
        "fm_station": "FM",
        "lfmf_signal": "LFMF",
        "cellular_signal": "CELLULAR",
    }.get(str(event.get("kind") or ""), "")


def _csv_loggable_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("kind") or "").strip()
    if kind not in CSV_LOGGABLE_KINDS:
        return False
    event_type = str(event.get("type") or "").strip().lower()
    if event_type in {"status", "control", "info", "debug"}:
        return False
    if kind == "ble_adv":
        return bool(event.get("address") or event.get("mac"))
    if kind == "classic_lap":
        return bool(event.get("lap"))
    if kind == "zigbee_frame":
        return bool(event.get("identity") or event.get("source_address") or event.get("destination_address") or event.get("payload_hex") or event.get("psdu_hex"))
    if kind == "tpms_frame":
        return bool(event.get("identity") or event.get("mac") or event.get("payload_hex"))
    if kind == "walkie_signal":
        return bool(event.get("identity") or event.get("center_freq_hz") or event.get("classification"))
    if kind == "wifi_frame":
        return bool(event.get("identity") or event.get("source_address") or event.get("destination_address") or event.get("bssid") or event.get("ssid"))
    if kind == "fm_station":
        return bool(event.get("frequency_hz") or event.get("center_freq_hz") or event.get("identity"))
    if kind == "lfmf_signal":
        return bool(event.get("frequency_hz") or event.get("center_freq_hz") or event.get("identity"))
    if kind == "cellular_signal":
        return bool(event.get("frequency_hz") or event.get("center_freq_hz") or event.get("identity"))
    return True


def _csv_event_row(event: dict[str, Any], columns: list[str]) -> dict[str, str]:
    observed_at = float(event.get("seen_at") or time.time())
    protocol = _csv_protocol_key(event)
    manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else {}
    appearance = event.get("appearance") if isinstance(event.get("appearance"), dict) else {}
    row_values: dict[str, Any] = {
        "run_id": state.csv_run_id,
        "observed_at_iso": _iso_from_epoch(observed_at),
        "observed_at_epoch": f"{observed_at:.6f}",
        "logged_at_iso": _iso_from_epoch(time.time()),
        "scanner_source": event.get("scanner_source"),
        "protocol": protocol,
        "kind": event.get("kind"),
        "identity": event.get("identity") or event.get("name") or event.get("address") or event.get("mac"),
        "device_type": event.get("device_type"),
        "device_type_detail": event.get("device_type_detail") or event.get("protocol_variant"),
        "mac": event.get("mac") or event.get("address") or event.get("full_mac"),
        "name": event.get("name"),
        "source_address": event.get("source_address"),
        "destination_address": event.get("destination_address"),
        "bssid": event.get("bssid"),
        "ssid": event.get("ssid"),
        "wifi_role": event.get("wifi_role"),
        "channel": event.get("channel"),
        "center_freq_hz": event.get("center_freq_hz"),
        "frequency_hz": event.get("frequency_hz"),
        "frequency_mhz": event.get("frequency_mhz"),
        "rssi_dbfs": event.get("rssi_dbfs") or event.get("last_rssi_dbfs"),
        "rssi_dbm": event.get("rssi_dbm"),
        "confidence": event.get("confidence"),
        "detail": event.get("detail") or event.get("status"),
        "payload_hex": event.get("payload_hex") or event.get("psdu_hex") or event.get("hex"),
        "raw_json": event,
        "address": event.get("address") or event.get("mac"),
        "address_type": event.get("address_type"),
        "uuid16": event.get("uuid16"),
        "uuid16_names": event.get("uuid16_names"),
        "manufacturer_id": manufacturer.get("id"),
        "manufacturer_name": manufacturer.get("name"),
        "appearance_category": appearance.get("category"),
        "appearance_name": appearance.get("name"),
        "lap": event.get("lap"),
        "uap": event.get("uap"),
        "nap": event.get("nap"),
        "full_mac": event.get("full_mac"),
        "status": event.get("status"),
        "target": event.get("target"),
        "candidate_count": event.get("candidate_count"),
        "processed_packets": event.get("processed_packets"),
        "broken_packets": event.get("broken_packets"),
        "repaired": event.get("repaired"),
        "repair_distance": event.get("repair_distance"),
        "pan_id": event.get("pan_id"),
        "fcs_ok": event.get("fcs_ok"),
        "fcs_hex": event.get("fcs_hex"),
        "decoded_text": event.get("decoded_text"),
        "sequence_number": event.get("sequence_number"),
        "psdu_hex": event.get("psdu_hex"),
        "protocol_variant": event.get("protocol_variant") or event.get("device_type_detail"),
        "sensor_id": event.get("sensor_id") or event.get("mac") or event.get("identity"),
        "ssid_visible": event.get("ssid_visible"),
        "count": event.get("count"),
        "power_dbfs": event.get("power_dbfs"),
        "noise_dbfs": event.get("noise_dbfs"),
        "excess_db": event.get("excess_db"),
        "audio_rms": event.get("audio_rms"),
        "pilot_db": event.get("pilot_db"),
        "rds_subcarrier_db": event.get("rds_subcarrier_db"),
        "stereo_likely": event.get("stereo_likely"),
        "rds_likely": event.get("rds_likely"),
        "frequency_khz": event.get("frequency_khz"),
        "carrier_dbfs": event.get("carrier_dbfs"),
        "carrier_snr_db": event.get("carrier_snr_db"),
        "audio_dbfs": event.get("audio_dbfs"),
        "modulation_pct": event.get("modulation_pct"),
        "band": event.get("band"),
        "band_label": event.get("band_label"),
        "active": event.get("active"),
        "link": event.get("link"),
        "cellular_type": event.get("cellular_type"),
        "technology": event.get("technology"),
        "likely_operator": event.get("likely_operator"),
        "operator_confidence": event.get("operator_confidence"),
        "likely_mcc": event.get("likely_mcc"),
        "likely_mnc": event.get("likely_mnc"),
        "likely_plmn": event.get("likely_plmn"),
        "plmn_source": event.get("plmn_source"),
        "decoded_mcc": event.get("decoded_mcc"),
        "decoded_mnc": event.get("decoded_mnc"),
        "decoded_plmn": event.get("decoded_plmn"),
        "noise_floor_dbfs": event.get("noise_floor_dbfs"),
        "occupied_width_hz": event.get("occupied_width_hz"),
        "passive_only": event.get("passive_only"),
        "content_decoded": event.get("content_decoded"),
    }
    return {column: _csv_cell(row_values.get(column)) for column in columns}


def _write_csv_schema(run_dir: Path, run_id: str) -> None:
    schema = {
        "run_id": run_id,
        "created_at": _iso_from_epoch(time.time()),
        "description": "RF Sentinel observation CSVs. Each row is one normalized protocol observation.",
        "folder": str(run_dir),
        "files": {
            "combined.csv": {
                "protocols": sorted(CSV_PROTOCOL_FILE_NAMES),
                "columns": CSV_COMBINED_COLUMNS,
            },
            **{
                file_name: {
                    "protocol": protocol,
                    "columns": CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), []),
                }
                for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items()
            },
        },
    }
    (run_dir / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv_header(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore").writeheader()


def _initialize_csv_files(run_dir: Path) -> None:
    _write_csv_header(run_dir / "combined.csv", CSV_COMBINED_COLUMNS)
    for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items():
        columns = CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), [])
        _write_csv_header(run_dir / file_name, columns)


def _csv_run_epoch(path: Path) -> float:
    run_id = path.name.split("-", 1)[0]
    try:
        return float(calendar.timegm(time.strptime(run_id, "%Y%m%dT%H%M%SZ")))
    except ValueError:
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()


def _archive_run_dir(run_dir: Path) -> Path | None:
    RF_SENTINEL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = RF_SENTINEL_ARCHIVE_DIR / f"{run_dir.name}.zip"
    if archive_path.exists():
        suffix = 1
        while archive_path.exists():
            suffix += 1
            archive_path = RF_SENTINEL_ARCHIVE_DIR / f"{run_dir.name}-{suffix:02d}.zip"
    tmp_path = archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for child in sorted(run_dir.rglob("*")):
                if child.is_file():
                    archive.write(child, arcname=str(Path(run_dir.name) / child.relative_to(run_dir)))
        tmp_path.replace(archive_path)
        shutil.rmtree(run_dir)
        app.logger.info("csv_run_archived source=%s archive=%s", run_dir, archive_path)
        return archive_path
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        app.logger.warning("csv_run_archive_failed path=%s error=%s", run_dir, exc)
        return None


def _trim_csv_archives(max_mb: int = RF_SENTINEL_CSV_ARCHIVE_MAX_MB) -> None:
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    try:
        archives = [path for path in RF_SENTINEL_ARCHIVE_DIR.glob("*.zip") if path.is_file()]
    except OSError as exc:
        app.logger.warning("csv_archive_list_failed path=%s error=%s", RF_SENTINEL_ARCHIVE_DIR, exc)
        return
    archive_stats: list[tuple[float, int, Path]] = []
    for path in archives:
        try:
            stat = path.stat()
        except OSError:
            continue
        archive_stats.append((stat.st_mtime, stat.st_size, path))
    total_bytes = sum(size for _, size, _ in archive_stats)
    for _, size, path in sorted(archive_stats, key=lambda item: item[0]):
        if total_bytes <= max_bytes:
            break
        try:
            path.unlink()
            total_bytes -= size
            app.logger.info("csv_archive_pruned path=%s max_mb=%s", path, max_mb)
        except OSError as exc:
            app.logger.warning("csv_archive_prune_failed path=%s error=%s", path, exc)


def _archive_old_csv_runs(
    retention_days: int = RF_SENTINEL_CSV_RETENTION_DAYS,
    max_archive_mb: int = RF_SENTINEL_CSV_ARCHIVE_MAX_MB,
) -> None:
    cutoff = time.time() - (max(1, int(retention_days)) * 86400)
    try:
        entries = list(RF_SENTINEL_RUNS_DIR.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        app.logger.warning("csv_retention_list_failed path=%s error=%s", RF_SENTINEL_RUNS_DIR, exc)
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if _csv_run_epoch(entry) >= cutoff:
            continue
        _archive_run_dir(entry)
    _trim_csv_archives(max_archive_mb)


def _start_csv_run() -> None:
    _archive_old_csv_runs()
    base_run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = base_run_id
    run_dir = RF_SENTINEL_RUNS_DIR / run_id
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_id = f"{base_run_id}-{suffix:02d}"
        run_dir = RF_SENTINEL_RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_csv_schema(run_dir, run_id)
    _initialize_csv_files(run_dir)
    state.csv_run_id = run_id
    state.csv_log_dir = str(run_dir)


def _append_csv_rows(events: list[dict[str, Any]]) -> None:
    loggable_events = [event for event in events if _csv_loggable_event(event)]
    if not loggable_events or not state.csv_log_dir:
        return
    run_dir = Path(state.csv_log_dir)
    with csv_log_lock:
        for file_name, columns, rows in _csv_batches(loggable_events):
            path = run_dir / file_name
            needs_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                if needs_header:
                    writer.writeheader()
                for row in rows:
                    writer.writerow(row)


def _csv_batches(events: list[dict[str, Any]]) -> list[tuple[str, list[str], list[dict[str, str]]]]:
    combined_rows = [_csv_event_row(event, CSV_COMBINED_COLUMNS) for event in events]
    batches: list[tuple[str, list[str], list[dict[str, str]]]] = [("combined.csv", CSV_COMBINED_COLUMNS, combined_rows)]
    for protocol, file_name in CSV_PROTOCOL_FILE_NAMES.items():
        protocol_events = [event for event in events if _csv_protocol_key(event) == protocol]
        if not protocol_events:
            continue
        columns = CSV_COMMON_COLUMNS + CSV_PROTOCOL_COLUMNS.get(protocol.lower(), [])
        batches.append((file_name, columns, [_csv_event_row(event, columns) for event in protocol_events]))
    return batches


def _scanner_json_to_events(source: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    now = float(payload.get("timestamp") or payload.get("seen_at") or time.time())
    protocol = str(payload.get("protocol") or "").lower()
    kind = str(payload.get("kind") or "").lower()
    source_protocol = str(source or "").rsplit(":", 1)[-1].lower()

    if protocol in {"ble", "btle"}:
        event_type = str(payload.get("type") or "").strip().lower()
        if event_type in {"status", "config", "raw", "metrics"}:
            return []
        if kind in {"ble_burst", "burst"}:
            payload["kind"] = "ble_burst"
        else:
            payload.setdefault("kind", "ble_adv")
        payload.setdefault("seen_at", now)
        return [payload]

    if kind == "classic_lap" or protocol in {"btc", "bluetooth_classic", "classic"} or (source_protocol == "btc" and payload.get("lap")):
        payload.setdefault("kind", "classic_lap")
        payload.setdefault("seen_at", now)
        event_type = str(payload.get("type") or "")
        if event_type == "page_access_seen":
            payload.setdefault("status", payload.get("status") or "page_access")
            payload.setdefault("detail", "page/inquiry access code observed")
        elif event_type in {"lap_initialized", "lap_resolved", "lap_seen", "lap_narrowed", "lap_two_uap_left"}:
            payload.setdefault("active_piconet", True)
            payload.setdefault("status", "active_piconet" if event_type == "lap_seen" else event_type.replace("lap_", ""))
            payload.setdefault("detail", "active Bluetooth Classic piconet traffic observed")
        else:
            payload.setdefault("status", event_type or "observed")
        return [payload]

    if protocol == "ieee802154" or source_protocol == "zigbee":
        mac = payload.get("mac") if isinstance(payload.get("mac"), dict) else {}
        payload_hex = str(mac.get("payload_hex") or payload.get("payload_hex") or "")
        fcs_ok = _optional_bool(payload.get("fcs_ok"))
        if fcs_ok is False and not RF_SENTINEL_KEEP_BAD_FCS:
            return []
        source_address = str(mac.get("source_address") or "").strip()
        destination_address = str(mac.get("destination_address") or "").strip()
        pan_id = mac.get("source_pan_id") or mac.get("destination_pan_id")
        text = _printable_hex_text(payload_hex)
        return [
            {
                "kind": "zigbee_frame",
                "protocol": "ZIGBEE",
                "seen_at": now,
                "identity": source_address or destination_address or f"802.15.4 CH {payload.get('channel') or '?'}",
                "mac": source_address or destination_address or "",
                "source_address": source_address,
                "destination_address": destination_address,
                "pan_id": pan_id,
                "detail": text or str(mac.get("frame_type") or "802.15.4 frame"),
                "decoded_text": text,
                "device_type": "802.15.4",
                "device_type_detail": str(mac.get("frame_type") or ""),
                "channel": payload.get("channel"),
                "center_freq_hz": payload.get("center_freq_hz"),
                "last_rssi_dbfs": _real_rssi(payload.get("rssi_dbfs")),
                "confidence": payload.get("confidence"),
                "fcs_ok": fcs_ok,
                "fcs_hex": mac.get("fcs_hex"),
                "payload_hex": payload_hex,
                "psdu_hex": payload.get("psdu_hex"),
                "sequence_number": mac.get("sequence_number"),
            }
        ]

    if protocol in {"tpms", "subghz"} or source_protocol == "tpms":
        fields = payload.get("decoded_fields") if isinstance(payload.get("decoded_fields"), dict) else {}
        sensor_id = str(fields.get("sensor_id") or fields.get("id") or payload.get("hex") or "").strip()
        detail_bits = [
            str(payload.get("protocol_variant") or "TPMS"),
            f"confidence={payload.get('confidence')}" if payload.get("confidence") is not None else "",
        ]
        return [
            {
                "kind": "tpms_frame",
                "protocol": "TPMS",
                "seen_at": now,
                "identity": sensor_id or "TPMS sensor",
                "mac": sensor_id,
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "TPMS",
                "device_type_detail": str(payload.get("protocol_variant") or ""),
                "center_freq_hz": payload.get("center_freq_hz"),
                "last_rssi_dbfs": payload.get("rssi_dbfs") or payload.get("burst_peak_dbfs"),
                "confidence": payload.get("confidence"),
                "payload_hex": payload.get("hex"),
            }
        ]

    if protocol == "walkie" or source_protocol == "walkie":
        frequency_hz = payload.get("frequency_hz") or payload.get("center_freq_hz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            frequency_hz_int = 0
        label = str(payload.get("identity") or "").strip() or (
            f"Walkie {frequency_hz_int / 1_000_000:.3f} MHz" if frequency_hz_int else "Walkie activity"
        )
        classification = str(payload.get("classification") or "walkie_activity").strip()
        detail_bits = [
            str(payload.get("modulation") or "NBFM").strip(),
            classification.replace("_", " "),
            f"audio {float(payload.get('audio_rms_dbfs')):.1f} dBFS" if payload.get("audio_rms_dbfs") is not None else "",
            f"bw {float(payload.get('audio_bandwidth_hz')):.0f} Hz" if payload.get("audio_bandwidth_hz") is not None else "",
        ]
        return [
            {
                "kind": "walkie_signal",
                "protocol": "WALKIE",
                "seen_at": now,
                "identity": label,
                "mac": str(frequency_hz_int or label),
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "Walkie-talkie",
                "device_type_detail": classification.replace("_", " "),
                "center_freq_hz": frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_mhz": payload.get("frequency_mhz"),
                "last_rssi_dbfs": payload.get("last_rssi_dbfs") or payload.get("rssi_dbfs") or payload.get("signal_dbfs"),
                "rssi_dbfs": payload.get("rssi_dbfs") or payload.get("signal_dbfs"),
                "signal_dbfs": payload.get("signal_dbfs"),
                "classification": classification,
                "modulation": payload.get("modulation"),
                "audio_rms_dbfs": payload.get("audio_rms_dbfs"),
                "audio_bandwidth_hz": payload.get("audio_bandwidth_hz"),
                "voice_band_ratio": payload.get("voice_band_ratio"),
                "voice_activity_ratio": payload.get("voice_activity_ratio"),
                "occupied_ratio": payload.get("occupied_ratio"),
                "freq_std_hz": payload.get("freq_std_hz"),
                "saved_iq_path": payload.get("saved_iq_path"),
                "saved_meta_path": payload.get("saved_meta_path"),
                "saved_wav_path": payload.get("saved_wav_path"),
                "confidence": payload.get("confidence"),
            }
        ]

    if protocol == "wifi" or source_protocol == "wifi":
        source_mac = str(payload.get("source") or payload.get("mac_sa") or "").strip()
        destination_mac = str(payload.get("destination") or payload.get("mac_da") or "").strip()
        bssid = str(payload.get("bssid") or "").strip()
        ssid = _clean_wifi_ssid(payload.get("ssid"))
        frame_type = str(payload.get("kind") or "wifi").strip()
        role = _wifi_role(frame_type, source_mac, destination_mac, bssid, ssid)
        ssid_visible = bool(ssid)
        station_label = str(payload.get("identity") or payload.get("name") or "").strip()
        ap_identifier = bssid or source_mac or destination_mac
        identity = ssid if role == "ap" and ssid_visible else (
            f"Hidden SSID {ap_identifier}" if role == "ap" and ap_identifier else (
                "Hidden SSID" if role == "ap" else (station_label or source_mac or destination_mac or bssid or "WiFi station")
            )
        )
        device_type = "Access Point" if role == "ap" else "Station"
        identity_source = (
            "SSID advertised in 802.11 management frame."
            if role == "ap" and ssid_visible
            else "No SSID value observed; showing BSSID/MAC identifier."
            if role == "ap"
            else "Observed as WiFi station/client traffic."
        )
        return [
            {
                "kind": "wifi_frame",
                "protocol": "WIFI",
                "seen_at": now,
                "identity": identity,
                "mac": source_mac or bssid or destination_mac,
                "wifi_role": role,
                "source_address": source_mac,
                "destination_address": destination_mac,
                "bssid": bssid,
                "ssid": ssid,
                "ssid_visible": ssid_visible,
                "identity_source": identity_source,
                "detail": str(payload.get("raw") or frame_type),
                "device_type": device_type,
                "device_type_detail": frame_type,
                "channel": payload.get("channel"),
                "center_freq_hz": int(payload.get("frequency_mhz") or 0) * 1_000_000 if payload.get("frequency_mhz") else None,
                "last_rssi_dbfs": payload.get("rssi_dbm"),
                "rssi_dbm": payload.get("rssi_dbm"),
                "count": payload.get("count"),
            }
        ]

    if protocol == "fm" or source_protocol == "fm":
        frequency_hz = payload.get("frequency_hz")
        frequency_mhz = payload.get("frequency_mhz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            try:
                frequency_hz_int = int(float(frequency_mhz) * 1_000_000)
            except (TypeError, ValueError):
                frequency_hz_int = 0
        label = str(payload.get("identity") or "").strip()
        if not label and frequency_hz_int:
            label = f"FM {frequency_hz_int / 1_000_000:.1f} MHz"
        detail_bits = [
            f"power {float(payload.get('power_dbfs')):.1f} dBFS" if payload.get("power_dbfs") is not None else "",
            f"pilot {float(payload.get('pilot_db')):.1f} dB" if payload.get("pilot_db") is not None else "",
            f"RDS {float(payload.get('rds_subcarrier_db')):.1f} dB" if payload.get("rds_subcarrier_db") is not None else "",
        ]
        return [
            {
                "kind": "fm_station",
                "protocol": "FM",
                "seen_at": now,
                "identity": label or "FM station",
                "mac": f"{frequency_hz_int}" if frequency_hz_int else label,
                "detail": " · ".join(bit for bit in detail_bits if bit) or "FM broadcast station",
                "device_type": "Broadcast FM",
                "device_type_detail": "Stereo pilot detected" if payload.get("stereo_likely") else "Mono/unknown stereo",
                "center_freq_hz": frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_mhz": payload.get("frequency_mhz"),
                "last_rssi_dbfs": payload.get("rssi_dbfs") or payload.get("power_dbfs"),
                "power_dbfs": payload.get("power_dbfs"),
                "noise_dbfs": payload.get("noise_dbfs"),
                "excess_db": payload.get("excess_db"),
                "audio_rms": payload.get("audio_rms"),
                "pilot_db": payload.get("pilot_db"),
                "rds_subcarrier_db": payload.get("rds_subcarrier_db"),
                "stereo_likely": payload.get("stereo_likely"),
                "rds_likely": payload.get("rds_likely"),
            }
        ]

    if protocol == "lfmf" or source_protocol == "lfmf":
        frequency_hz = payload.get("frequency_hz") or payload.get("freq_hz")
        frequency_khz = payload.get("frequency_khz") or payload.get("freq_khz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            try:
                frequency_hz_int = int(float(frequency_khz) * 1000)
            except (TypeError, ValueError):
                frequency_hz_int = 0
        label = f"{frequency_hz_int / 1000:.1f} kHz" if frequency_hz_int else "VLF/LF/MF signal"
        band_label = str(payload.get("band_label") or payload.get("band") or "VLF/LF/MF").strip()
        detail_bits = [
            band_label,
            f"carrier {float(payload.get('carrier_dbfs')):.1f} dBFS" if payload.get("carrier_dbfs") is not None else "",
            f"SNR {float(payload.get('carrier_snr_db')):.1f} dB" if payload.get("carrier_snr_db") is not None else "",
            f"excess {float(payload.get('excess_db')):.1f} dB" if payload.get("excess_db") is not None else "",
            f"mod {float(payload.get('modulation_pct')):.1f}%" if payload.get("modulation_pct") is not None else "",
        ]
        return [
            {
                "kind": "lfmf_signal",
                "protocol": "LFMF",
                "seen_at": now,
                "identity": f"{band_label} {label}",
                "mac": str(frequency_hz_int or label),
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "VLF/LF/MF signal",
                "device_type_detail": band_label,
                "center_freq_hz": frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_khz": frequency_khz,
                "last_rssi_dbfs": payload.get("carrier_dbfs") or payload.get("power_dbfs"),
                "power_dbfs": payload.get("power_dbfs"),
                "carrier_dbfs": payload.get("carrier_dbfs"),
                "carrier_snr_db": payload.get("carrier_snr_db"),
                "excess_db": payload.get("excess_db"),
                "audio_dbfs": payload.get("audio_dbfs"),
                "modulation_pct": payload.get("modulation_pct"),
                "band": payload.get("band"),
                "band_label": band_label,
                "active": payload.get("active"),
            }
        ]

    if protocol in {"cellular awareness", "cellular"} or source_protocol == "cellular":
        frequency_hz = payload.get("frequency_hz") or payload.get("center_freq_hz")
        frequency_mhz = payload.get("frequency_mhz")
        try:
            frequency_hz_int = int(frequency_hz)
        except (TypeError, ValueError):
            try:
                frequency_hz_int = int(float(frequency_mhz) * 1_000_000)
            except (TypeError, ValueError):
                frequency_hz_int = 0
        label = f"{frequency_hz_int / 1_000_000:.3f} MHz" if frequency_hz_int else "Cellular activity"
        band = str(payload.get("band") or "Cellular spectrum").strip()
        link = str(payload.get("link") or "unknown").strip()
        cellular_type = str(payload.get("cellular_type") or payload.get("technology") or "Cellular").strip()
        technology = str(payload.get("technology") or cellular_type).strip()
        likely_operator = str(payload.get("likely_operator") or "").strip()
        operator_confidence = str(payload.get("operator_confidence") or "").strip()
        likely_plmn = str(payload.get("likely_plmn") or "").strip()
        plmn_source = str(payload.get("plmn_source") or "").strip()
        decoded_plmn = str(payload.get("decoded_plmn") or "").strip()
        lte_pss_detected = bool(payload.get("lte_pss_detected"))
        lte_n_id_2 = payload.get("lte_n_id_2")
        lte_sync_status = str(payload.get("lte_sync_status") or "").strip()
        classification = str(payload.get("classification") or "Passive cellular spectrum activity").strip()
        detail_bits = [
            technology,
            cellular_type if cellular_type != technology else "",
            likely_operator if likely_operator else "",
            f"PLMN {decoded_plmn}" if decoded_plmn else (f"likely PLMN {likely_plmn}" if likely_plmn else ""),
            f"LTE PSS N_id_2={lte_n_id_2}" if lte_pss_detected and lte_n_id_2 is not None else (lte_sync_status.replace("_", " ") if lte_sync_status and lte_sync_status not in {"not_attempted", "not_lte_band"} else ""),
            band,
            link,
            f"excess {float(payload.get('excess_db')):.1f} dB" if payload.get("excess_db") is not None else "",
            "target" if payload.get("target") else "",
        ]
        return [
            {
                "kind": "cellular_signal",
                "protocol": "CELLULAR",
                "seen_at": now,
                "identity": f"{label} {link}".strip(),
                "mac": str(frequency_hz_int or label),
                "detail": " · ".join(bit for bit in detail_bits if bit),
                "device_type": "Cellular",
                "device_type_detail": cellular_type,
                "center_freq_hz": payload.get("center_freq_hz") or frequency_hz_int or None,
                "frequency_hz": frequency_hz_int or None,
                "frequency_mhz": payload.get("frequency_mhz"),
                "last_rssi_dbfs": payload.get("power_dbfs"),
                "rssi_dbfs": payload.get("power_dbfs"),
                "power_dbfs": payload.get("power_dbfs"),
                "noise_floor_dbfs": payload.get("noise_floor_dbfs"),
                "excess_db": payload.get("excess_db"),
                "occupied_width_hz": payload.get("occupied_width_hz"),
                "band": band,
                "link": link,
                "cellular_type": cellular_type,
                "technology": technology,
                "likely_operator": likely_operator,
                "operator_confidence": operator_confidence,
                "likely_mcc": payload.get("likely_mcc"),
                "likely_mnc": payload.get("likely_mnc"),
                "likely_plmn": likely_plmn,
                "plmn_source": plmn_source,
                "decoded_mcc": payload.get("decoded_mcc"),
                "decoded_mnc": payload.get("decoded_mnc"),
                "decoded_plmn": decoded_plmn,
                "decoded_plmn_source": payload.get("decoded_plmn_source"),
                "lte_sync_status": lte_sync_status,
                "lte_pss_detected": lte_pss_detected,
                "lte_n_id_2": lte_n_id_2,
                "lte_pss_metric": payload.get("lte_pss_metric"),
                "lte_pss_freq_offset_hz": payload.get("lte_pss_freq_offset_hz"),
                "lte_cell_id_status": payload.get("lte_cell_id_status"),
                "lte_mib_status": payload.get("lte_mib_status"),
                "lte_sib1_status": payload.get("lte_sib1_status"),
                "classification": classification,
                "target": payload.get("target"),
                "passive_only": payload.get("passive_only", True),
                "content_decoded": payload.get("content_decoded", False),
            }
        ]

    return []


PAGE_ACTIVITY_UI_INTERVAL_S = 0.75


def _is_page_access_event(event: dict[str, Any]) -> bool:
    if str(event.get("kind") or "") != "classic_lap":
        return False
    status = str(event.get("status") or event.get("type") or "").lower()
    return status.startswith("page_access")


def _update_page_activity(event: dict[str, Any]) -> bool:
    lap = re.sub(r"[^0-9A-Fa-f]", "", str(event.get("lap") or "")).upper()
    if len(lap) != 6:
        return True
    now = float(event.get("seen_at") or time.time())
    channel = event.get("channel")
    key = f"{lap}:{channel}"
    previous = state.page_activity.get(key) or {}
    hits = int(previous.get("hits") or 0) + 1
    first_seen = float(previous.get("first_seen") or now)
    last_emit = float(previous.get("last_emit") or 0.0)
    try:
        rssi = float(event.get("rssi_dbfs", event.get("last_rssi_dbfs", previous.get("rssi_dbfs", -120.0))))
    except (TypeError, ValueError):
        rssi = float(previous.get("rssi_dbfs", -120.0))
    state.page_activity[key] = {
        "lap": lap,
        "channel": channel,
        "rssi_dbfs": round(rssi, 1),
        "hits": hits,
        "first_seen": first_seen,
        "last_seen": now,
        "last_emit": now if now - last_emit >= PAGE_ACTIVITY_UI_INTERVAL_S else last_emit,
        "status": str(event.get("status") or "page_access"),
    }
    # Keep detections/UI updates bounded during page/inquiry storms while the
    # TUI page panel still gets accurate hit counts from page_activity.
    return now - last_emit >= PAGE_ACTIVITY_UI_INTERVAL_S


def _coalesce_detection_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return events
    filtered: list[dict[str, Any]] = []
    for event in events:
        if _is_page_access_event(event):
            if _update_page_activity(event):
                filtered.append(event)
            continue
        filtered.append(event)
    cutoff = time.time() - 120.0
    stale_keys = [key for key, item in state.page_activity.items() if float(item.get("last_seen") or 0.0) < cutoff]
    for key in stale_keys:
        state.page_activity.pop(key, None)
    return filtered


def _append_detections(events: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    if not events and not candidates:
        return
    for item in [*events, *candidates]:
        if item.get("kind") == "classic_lap" or item.get("lap"):
            uap_value = item.get("uap")
            uap_hex = str(item.get("uap_hex") or "")
            if uap_value in {None, "", "Pending"} and re.fullmatch(r"[0-9A-Fa-f]{2}", uap_hex):
                uap_value = uap_hex.upper()
            item.setdefault("nap", "XXXX")
            item["uap"] = str(uap_value or "XX").upper()
            item["full_mac"] = _classic_full_mac(item.get("nap"), item.get("uap"), item.get("lap"))
            item.setdefault("mac", item["full_mac"])
    with state_lock:
        events = _coalesce_detection_events(events)
    if not events and not candidates:
        return
    _append_csv_rows(events)
    with state_lock:
        for event in events:
            state.bursts_seen += 1 if event["kind"].endswith("burst") else 0
            state.ble_packets_seen += 1 if event["kind"] in {"ble_adv", "ble_burst"} else 0
            state.classic_bursts_seen += 1 if event["kind"] in {"classic_burst", "classic_lap"} else 0
            mode_key = {
                "ble_adv": "ble",
                "ble_burst": "ble",
                "classic_burst": "classic",
                "classic_lap": "classic",
                "zigbee_frame": "zigbee",
                "tpms_frame": "tpms",
                "walkie_signal": "walkie",
                "wifi_frame": "wifi",
                "fm_station": "fm",
                "lfmf_signal": "lfmf",
                "cellular_signal": "cellular",
            }.get(str(event.get("kind") or ""))
            if mode_key:
                rssi = _real_rssi(event.get("rssi_dbfs", event.get("last_rssi_dbfs")))
                if rssi is not None:
                    state.rssi_by_mode[mode_key] = round(rssi, 1)
                    state.last_rssi_dbfs = round(rssi, 1)
            if event["kind"] in {"classic_burst", "classic_lap"}:
                try:
                    rssi = float(event.get("rssi_dbfs"))
                    if rssi > -119.9:
                        state.rssi_by_mode["classic"] = round(rssi, 1)
                        state.last_rssi_dbfs = round(rssi, 1)
                        state.noise_floor_dbfs = round((state.noise_floor_dbfs * 0.92) + (rssi * 0.08), 1)
                except (TypeError, ValueError):
                    pass
            if event["kind"] in {"ble_adv", "ble_burst", "classic_lap", "zigbee_frame", "tpms_frame", "walkie_signal", "wifi_frame", "fm_station", "lfmf_signal", "cellular_signal"}:
                _upsert_discovery_row(event)
            if event["kind"] == "classic_lap":
                _upsert_classic_address(event)
            if event["kind"] in {"classic_burst", "classic_lap"}:
                _upsert_channel_activity(event)
        visible_events = [event for event in events if event.get("kind") != "ble_burst"]
        state.detections = (visible_events + state.detections)[:240]
        if candidates:
            state.classic_candidates = (candidates + state.classic_candidates)[:64]
    if not CONSOLE_DASHBOARD:
        _console_render()


SHARED_BT_DETECTOR_DB = os.getenv("BT_DETECTOR_DB", "/tmp/bt-detections.sqlite3")
SHARED_BT_DETECTOR_POLL_S = 2.0


def _shared_bt_detector_poll_loop() -> None:
    """Feed discovery_table from the always-on shared detector
    (bt_detector_service, rf-iq-gateway/scripts) so it's populated
    regardless of whether this app's own on-demand scan is running - this
    used to only exist while a user had manually started a scan in this
    specific app, matching neither "always shows detections" nor "same
    detections as sdr-shark, which reads the same shared database."

    _scanner_json_to_events() (used for this app's own rf_sentinel_scan
    subprocess output) classifies purely from each event's own protocol/
    kind fields - the shared detector emits the identical JSON shape
    (same bluetooth_scanner engine), so events from either source funnel
    through the same _append_detections()/discovery_table pipeline and
    render as the same cards.
    """
    last_id = 0
    while True:
        try:
            conn = sqlite3.connect(f"file:{SHARED_BT_DETECTOR_DB}?mode=ro", uri=True, timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT id, raw_json FROM events WHERE id > ? ORDER BY id ASC LIMIT 200",
                    (last_id,),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
            events: list[dict[str, Any]] = []
            for row_id, raw_json in rows:
                last_id = max(last_id, int(row_id))
                try:
                    payload = json.loads(raw_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                events.extend(_scanner_json_to_events("shared", payload))
            if events:
                _append_detections(events, [])
        except sqlite3.OperationalError:
            pass  # DB not created yet (detector service still starting up).
        except Exception as exc:
            print(f"[shared-bt] poll error: {exc}", file=sys.stderr, flush=True)
        time.sleep(SHARED_BT_DETECTOR_POLL_S)


def _classic_full_mac(nap: Any = None, uap: Any = None, lap: Any = None) -> str:
    nap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(nap or "")).upper()
    uap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(uap or "")).upper()
    lap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(lap or "")).upper()
    nap_hex = nap_clean[:4] if len(nap_clean) >= 4 else "XXXX"
    uap_hex = uap_clean[:2] if len(uap_clean) >= 2 else "XX"
    lap_hex = lap_clean[:6] if len(lap_clean) >= 6 else "XXXXXX"
    return f"{nap_hex[0:2]}:{nap_hex[2:4]}:{uap_hex}:{lap_hex[0:2]}:{lap_hex[2:4]}:{lap_hex[4:6]}"


def _classic_probe_mac(uap: Any = None, lap: Any = None) -> str:
    uap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(uap or "")).upper()
    lap_clean = re.sub(r"[^0-9A-Fa-f]", "", str(lap or "")).upper()
    if len(uap_clean) < 2 or len(lap_clean) < 6:
        return ""
    return f"00:00:{uap_clean[:2]}:{lap_clean[0:2]}:{lap_clean[2:4]}:{lap_clean[4:6]}"


def _apply_btc_name_to_rows(lap: str, name: str, source_address: str) -> None:
    clean_lap = re.sub(r"[^0-9A-Fa-f]", "", str(lap or "")).upper()
    clean_name = str(name or "").strip()
    if len(clean_lap) != 6 or not clean_name:
        return
    source_clean = _classic_mac_key(source_address)
    source_uap = source_clean[4:6] if source_clean else ""
    _remember_btc_name(clean_name, source_clean, clean_lap, source_uap)
    with state_lock:
        for row in state.discovery_table:
            if row.get("protocol") != "BTC" or str(row.get("lap") or "").upper() != clean_lap:
                continue
            row["name"] = clean_name
            row["identity"] = clean_name
            row["identity_source"] = f"Bluetooth remote name via hcitool name {source_address}"
            row.setdefault("device_type", "Bluetooth Classic")


def _btc_name_lookup_worker(address: str, lap: str) -> None:
    name = ""
    ok = False
    try:
        resp = requests.post(
            f"{_gateway_base()}/bluetooth/name",
            headers=_gateway_headers(),
            json={"controller": "hci0", "address": address, "timeout_seconds": 6.0},
            timeout=8,
        )
        if resp.status_code < 400:
            payload = resp.json()
            name = str(payload.get("name") or "").strip()
            ok = bool(payload.get("ok")) and bool(name)
    except requests.RequestException:
        ok = False
    with btc_name_cache_lock:
        address_clean = _classic_mac_key(address)
        row = {"name": name, "address": address_clean, "lap": _classic_lap_key(lap), "uap": address_clean[4:6] if address_clean else "", "checked_at": time.time(), "last_seen_at": time.time(), "pending": False, "ok": ok}
        for key in _btc_name_keys(address_clean, lap, row["uap"]):
            btc_name_cache[key] = dict(row)
    if ok:
        _apply_btc_name_to_rows(lap, name, address)


def _maybe_schedule_btc_name_lookup(row: dict[str, Any]) -> None:
    bluetooth_classic = _clean_bluetooth_classic_config(_read_ui_config().get("bluetooth_classic"))
    if not (RF_SENTINEL_BTC_NAME_LOOKUP or bluetooth_classic.get("remote_name_lookup")):
        return
    if row.get("protocol") != "BTC" or row.get("name"):
        return
    address = _classic_probe_mac(row.get("uap"), row.get("lap"))
    if not address:
        return
    now = time.time()
    with btc_name_cache_lock:
        cached = btc_name_cache.get(address)
        if cached:
            if cached.get("name"):
                name = str(cached.get("name") or "").strip()
                row["name"] = name
                row["identity"] = name
                row["identity_source"] = f"Bluetooth remote name via hcitool name {address}"
                row.setdefault("device_type", "Bluetooth Classic")
                return
            if cached.get("pending") or now - float(cached.get("checked_at") or 0.0) < 300.0:
                return
        btc_name_cache[address] = {"name": "", "checked_at": now, "pending": True, "ok": False}
    threading.Thread(target=_btc_name_lookup_worker, args=(address, str(row.get("lap") or "")), daemon=True).start()


def _utc_date_key(timestamp_s: Any) -> str:
    try:
        value = float(timestamp_s)
    except (TypeError, ValueError):
        value = time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(value))


def _seen_day_stats(dates: list[str]) -> dict[str, Any]:
    clean_dates = sorted({str(date) for date in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date))})
    if not clean_dates:
        clean_dates = [_utc_date_key(time.time())]
    last_date = clean_dates[-1]
    streak = 0
    span_days = 1
    weekly_cadence = False
    weekday_names: list[str] = []
    weekend_only = False
    try:
        date_stamps = [calendar.timegm(time.strptime(date, "%Y-%m-%d")) for date in clean_dates]
        cursor = date_stamps[-1]
        date_set = set(clean_dates)
        while _utc_date_key(cursor) in date_set:
            streak += 1
            cursor -= 86400
        span_days = max(1, int((date_stamps[-1] - date_stamps[0]) / 86400))
        deltas = [int((right - left) / 86400) for left, right in zip(date_stamps, date_stamps[1:])]
        weekly_cadence = bool(deltas) and all(6 <= delta <= 8 for delta in deltas)
        weekdays = [time.gmtime(stamp).tm_wday for stamp in date_stamps]
        weekday_names = [
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][day]
            for day in sorted(set(weekdays))
        ]
        weekend_only = bool(weekdays) and all(day in {5, 6} for day in weekdays)
    except (TypeError, ValueError):
        streak = 1
    return {
        "seen_dates": clean_dates[-30:],
        "seen_days": streak,
        "seen_day_count": len(clean_dates),
        "seen_span_days": span_days,
        "seen_weekly_cadence": weekly_cadence,
        "seen_weekend_only": weekend_only,
        "seen_weekday_names": weekday_names,
        "first_seen_date": clean_dates[0],
        "last_seen_date": last_date,
    }


def _classic_lap_key(value: Any) -> str:
    clean = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return clean[-6:] if len(clean) >= 6 else ""


def _classic_mac_key(value: Any) -> str:
    clean = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return clean if len(clean) == 12 else ""


def _classic_uap_resolved(value: Any) -> bool:
    clean = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return bool(re.fullmatch(r"[0-9A-F]{2}", clean)) and clean not in {"XX"}


def _classic_evidence_label(status: Any, event_type: Any = "") -> str:
    status_text = str(status or "").strip().lower()
    event_text = str(event_type or "").strip().lower()
    if event_text == "passive_fhs_bdaddr" or status_text == "passive_fhs":
        return "Passive FHS BD_ADDR"
    if event_text == "page_access_seen" or status_text.startswith("page_access"):
        return "Page / inquiry access"
    if event_text in {"lap_resolved", "lap_seen"} or status_text in {"resolved", "seen", "active_piconet"}:
        return "Active piconet"
    if event_text in {"lap_initialized", "lap_narrowed", "lap_two_uap_left"} or status_text in {"initialized", "brute_forcing"}:
        return "Active piconet candidate"
    return "Bluetooth Classic evidence"


def _classic_display_identity(lap: Any, uap: Any, nap: Any, full_mac: Any, status: Any, name: Any = "") -> str:
    clean_name = str(name or "").strip()
    if clean_name:
        return clean_name
    lap_key = _classic_lap_key(lap)
    if _classic_uap_resolved(uap):
        return str(full_mac or _classic_full_mac(nap, uap, lap)).upper()
    label = _classic_evidence_label(status)
    if label.startswith("Active piconet") and lap_key:
        return f"Active piconet {lap_key}"
    if lap_key:
        return f"LAP {lap_key}"
    return "Bluetooth Classic"


def _classic_identity_source(event: dict[str, Any], lap: str, uap: str) -> str:
    event_type = str(event.get("type") or "")
    status = str(event.get("status") or "")
    if _classic_uap_resolved(uap):
        return "LAP extracted from Classic access code; UAP validated from packet header HEC across active traffic."
    if event_type == "page_access_seen" or status.startswith("page_access"):
        return f"Bluetooth Classic access code for LAP {lap} observed during page/inquiry style traffic."
    candidate_count = event.get("candidate_count")
    suffix = f"; {candidate_count} UAP candidates remain" if candidate_count not in {None, "", 0} else ""
    return f"LAP extracted from connected Bluetooth Classic piconet access code{suffix}."


btc_name_cache.update(_load_btc_name_cache())


def _history_date_from_row(row: dict[str, str]) -> str:
    observed = str(row.get("observed_at_epoch") or "").strip()
    if observed:
        return _utc_date_key(observed)
    raw_json = str(row.get("raw_json") or "").strip()
    if raw_json.startswith("{"):
        try:
            payload = json.loads(raw_json)
            return _utc_date_key(payload.get("seen_at") or payload.get("timestamp"))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return ""


def _clean_mac_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", text) else ""


def _frequency_key(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    return str(int(round(numeric))) if numeric > 0 else ""


def _identity_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _history_keys_for_protocol(protocol: str, row: dict[str, str]) -> list[str]:
    protocol_key = str(protocol or "").strip().upper()
    raw_json = str(row.get("raw_json") or "").strip()
    payload: dict[str, Any] = {}
    if raw_json.startswith("{"):
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    keys: list[str] = []
    if protocol_key == "BTC":
        lap = _classic_lap_key(row.get("lap") or payload.get("lap") or payload.get("full_mac") or payload.get("mac"))
        mac = _classic_mac_key(row.get("full_mac") or row.get("mac") or row.get("address") or payload.get("full_mac") or payload.get("mac") or payload.get("address"))
        if lap:
            keys.append(f"BTC:lap:{lap}")
        if mac:
            keys.append(f"BTC:mac:{mac}")
    elif protocol_key == "BTLE":
        mac = _clean_mac_key(row.get("address") or row.get("mac") or payload.get("address") or payload.get("mac"))
        name = _identity_key(row.get("name") or payload.get("name"))
        if mac:
            keys.append(f"BTLE:mac:{mac}")
        if name:
            keys.append(f"BTLE:name:{name}")
    elif protocol_key == "ZIGBEE":
        channel = str(row.get("channel") or payload.get("channel") or "").strip()
        if channel:
            keys.append(f"ZIGBEE:channel:{channel}")
    elif protocol_key == "FM":
        freq = _frequency_key(row.get("frequency_hz") or row.get("center_freq_hz") or payload.get("frequency_hz") or payload.get("center_freq_hz"))
        if freq:
            keys.append(f"FM:freq:{freq}")
    elif protocol_key == "CELLULAR":
        identity = _identity_key(row.get("identity") or payload.get("identity"))
        freq = _frequency_key(row.get("frequency_hz") or payload.get("frequency_hz"))
        if identity:
            keys.append(f"CELLULAR:identity:{identity}")
        if freq:
            keys.append(f"CELLULAR:freq:{freq}")
    return list(dict.fromkeys(keys))


def _record_history_row(protocol: str, row: dict[str, str], dates_by_key: dict[str, set[str]]) -> None:
    date_key = _history_date_from_row(row)
    if not date_key:
        return
    for key in _history_keys_for_protocol(protocol, row):
        dates_by_key.setdefault(key, set()).add(date_key)


def _load_seen_history() -> dict[str, Any]:
    dates_by_key: dict[str, set[str]] = {}
    protocols = {
        "BTC": "btc.csv",
        "BTLE": "btle.csv",
        "ZIGBEE": "zigbee.csv",
        "FM": "fm.csv",
        "CELLULAR": "cellular.csv",
    }
    for protocol, file_name in protocols.items():
        for csv_path in sorted(RF_SENTINEL_RUNS_DIR.glob(f"*/{file_name}")):
            try:
                with csv_path.open(newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        _record_history_row(protocol, row, dates_by_key)
            except (OSError, csv.Error):
                continue
    for archive_path in sorted(RF_SENTINEL_ARCHIVE_DIR.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for name in archive.namelist():
                    file_name = Path(name).name
                    protocol = next((proto for proto, expected in protocols.items() if file_name == expected), "")
                    if not protocol:
                        continue
                    with archive.open(name) as raw_handle:
                        text_handle = io.TextIOWrapper(raw_handle, encoding="utf-8", newline="")
                        for row in csv.DictReader(text_handle):
                            _record_history_row(protocol, row, dates_by_key)
        except (OSError, zipfile.BadZipFile, csv.Error):
            continue
    return {"loaded_at": time.time(), "dates": dates_by_key}


def _history_dates_for_keys(keys: list[str]) -> list[str]:
    clean_keys = [key for key in keys if key]
    if not clean_keys:
        return []
    with seen_history_lock:
        loaded_at = float(seen_history_cache.get("loaded_at") or 0.0)
        if time.time() - loaded_at > 60.0:
            seen_history_cache.clear()
            seen_history_cache.update(_load_seen_history())
        dates: set[str] = set()
        dates_by_key = seen_history_cache.get("dates", {})
        for key in clean_keys:
            dates.update(dates_by_key.get(key, set()))
        return sorted(dates)


def _row_history_keys(row: dict[str, Any]) -> list[str]:
    protocol = str(row.get("protocol") or "").strip().upper()
    keys: list[str] = []
    if protocol == "BTC":
        lap = _classic_lap_key(row.get("lap") or row.get("full_mac") or row.get("mac"))
        mac = _classic_mac_key(row.get("full_mac") or row.get("mac"))
        if lap:
            keys.append(f"BTC:lap:{lap}")
        if mac:
            keys.append(f"BTC:mac:{mac}")
    elif protocol == "BTLE":
        mac = _clean_mac_key(row.get("mac") or row.get("address"))
        name = _identity_key(row.get("name"))
        if mac:
            keys.append(f"BTLE:mac:{mac}")
        if name:
            keys.append(f"BTLE:name:{name}")
    elif protocol == "ZIGBEE":
        channel = str(row.get("channel") or "").strip()
        if channel:
            keys.append(f"ZIGBEE:channel:{channel}")
    elif protocol == "FM":
        freq = _frequency_key(row.get("frequency_hz") or row.get("center_freq_hz") or row.get("mac"))
        if freq:
            keys.append(f"FM:freq:{freq}")
    elif protocol == "CELLULAR":
        identity = _identity_key(row.get("identity"))
        freq = _frequency_key(row.get("frequency_hz") or row.get("mac"))
        if identity:
            keys.append(f"CELLULAR:identity:{identity}")
        if freq:
            keys.append(f"CELLULAR:freq:{freq}")
    return list(dict.fromkeys(keys))


def _seen_history_dates_for_row(row: dict[str, Any]) -> list[str]:
    return _history_dates_for_keys(_row_history_keys(row))


def _btc_seen_history_dates(lap: Any, full_mac: Any = None) -> list[str]:
    keys: list[str] = []
    lap_key = _classic_lap_key(lap)
    mac_key = _classic_mac_key(full_mac)
    if lap_key:
        keys.append(f"BTC:lap:{lap_key}")
    if mac_key:
        keys.append(f"BTC:mac:{mac_key}")
    return _history_dates_for_keys(keys)


def _upsert_discovery_row(event: dict[str, Any]) -> None:
    now = float(event.get("seen_at", time.time()))
    seen_date = _utc_date_key(now)
    if event.get("kind") == "ble_burst":
        row = {
            "key": "ble:activity",
            "protocol": "BTLE",
            "identity": "Bluetooth Low Energy",
            "mac": "",
            "detail": "wideband BLE RF activity",
            "device_type": "BLE activity",
            "device_type_detail": "burst activity",
            "identity_source": "Observed BLE RF bursts; decoded advertisements appear as individual devices.",
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs") or event.get("last_rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "ble_adv":
        mac = str(event.get("address") or "unknown").strip()
        if not mac or mac == "unknown":
            return
        name = str(event.get("name") or "").strip()
        address_type = str(event.get("address_type") or "")
        uuid16 = event.get("uuid16") if isinstance(event.get("uuid16"), list) else []
        manufacturer = event.get("manufacturer") if isinstance(event.get("manufacturer"), dict) else None
        appearance = event.get("appearance") if isinstance(event.get("appearance"), dict) else None
        cached = _remember_ble_identity(mac, name, address_type, now, uuid16, manufacturer, appearance)
        name = name or str(cached.get("name") or "").strip()
        uuid16 = list(cached.get("uuid16") or uuid16)
        uuid16_names = list(cached.get("uuid16_names") or _uuid16_names(uuid16))
        manufacturer = cached.get("manufacturer") or manufacturer
        appearance = cached.get("appearance") if isinstance(cached.get("appearance"), dict) else appearance
        if not manufacturer:
            manufacturer = _manufacturer_from_uuid16(uuid16)
        identity = _ble_identity_label(name, uuid16_names, manufacturer, mac)
        identity_source = _ble_identity_source(name, uuid16_names, manufacturer)
        device_type = _ble_device_type_label(name, uuid16_names, manufacturer, appearance)
        device_type_detail = _ble_device_type_detail(uuid16_names, manufacturer, appearance)
        row = {
            "key": f"ble:{mac}",
            "protocol": "BTLE",
            "identity": identity,
            "mac": mac,
            "name": name,
            "uuid16": uuid16,
            "uuid16_names": uuid16_names,
            "manufacturer": manufacturer,
            "appearance": appearance,
            "device_type": device_type,
            "device_type_detail": device_type_detail,
            "identity_source": identity_source,
            "detail": address_type,
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "classic_lap":
        lap = str(event.get("lap") or "").strip()
        if not lap:
            return
        uap = str(event.get("uap") or "XX").upper()
        nap = str(event.get("nap") or "XXXX").upper()
        full_mac = _classic_full_mac(nap, uap, lap)
        event_type = str(event.get("type") or "")
        status = str(event.get("status") or event_type or "observed")
        if event_type in {"lap_initialized", "lap_resolved", "lap_seen", "lap_narrowed", "lap_two_uap_left"} and status == event_type:
            status = "active_piconet" if event_type == "lap_seen" else event_type.replace("lap_", "")
        evidence_label = _classic_evidence_label(status, event_type)
        target = _classic_test_match(lap, uap)
        identity = _classic_display_identity(lap, uap, nap, full_mac, status, event.get("name"))
        detail = str(event.get("detail") or evidence_label)
        if target:
            identity = f"TEST DONGLE {identity}"
            detail = "target-match" if not detail else f"target-match · {detail}"
        row = {
            "key": f"btc:{lap}",
            "protocol": "BTC",
            "identity": identity,
            "mac": full_mac if _classic_uap_resolved(uap) else f"LAP {lap}",
            "nap": nap,
            "uap": uap,
            "lap": lap,
            "full_mac": full_mac,
            "detail": detail,
            "status": status,
            "event_type": event_type,
            "active_piconet": bool(event.get("active_piconet") or evidence_label.startswith("Active piconet")),
            "device_type": "Bluetooth Classic",
            "device_type_detail": evidence_label,
            "identity_source": _classic_identity_source(event, lap, uap),
            "target": bool(target),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
            "candidate_count": event.get("candidate_count"),
            "tracking_us": event.get("tracking_us"),
            "processed_packets": event.get("processed_packets"),
            "broken_packets": event.get("broken_packets"),
            "access_word": event.get("access_word"),
            "btcsniffer_bin": event.get("btcsniffer_bin"),
            "uaps": event.get("uaps"),
        }
    elif event.get("kind") == "zigbee_frame":
        source_address = str(event.get("source_address") or "").strip()
        destination_address = str(event.get("destination_address") or "").strip()
        identity = str(event.get("identity") or source_address or destination_address or "802.15.4 frame")
        pan_id = event.get("pan_id")
        key_bits = [source_address or destination_address or identity, str(pan_id or ""), str(event.get("channel") or "")]
        row = {
            "key": f"zigbee:{':'.join(key_bits)}",
            "protocol": "ZIGBEE",
            "identity": identity,
            "mac": source_address or destination_address,
            "source_address": source_address,
            "destination_address": destination_address,
            "pan_id": pan_id,
            "detail": str(event.get("detail") or "802.15.4 frame"),
            "decoded_text": str(event.get("decoded_text") or ""),
            "device_type": str(event.get("device_type") or "802.15.4"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": _real_rssi(event.get("last_rssi_dbfs", event.get("rssi_dbfs"))),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
            "confidence": event.get("confidence"),
            "fcs_ok": event.get("fcs_ok"),
            "fcs_hex": event.get("fcs_hex"),
            "payload_hex": event.get("payload_hex") or event.get("psdu_hex"),
        }
    elif event.get("kind") == "tpms_frame":
        identity = str(event.get("identity") or "TPMS sensor")
        row = {
            "key": f"tpms:{identity}",
            "protocol": "TPMS",
            "identity": identity,
            "mac": str(event.get("mac") or identity),
            "detail": str(event.get("detail") or "TPMS frame"),
            "device_type": str(event.get("device_type") or "TPMS"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("rssi_dbfs"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
            "confidence": event.get("confidence"),
            "payload_hex": event.get("payload_hex"),
        }
    elif event.get("kind") == "walkie_signal":
        identity = str(event.get("identity") or "Walkie activity")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        row = {
            "key": f"walkie:{frequency_hz or identity}",
            "protocol": "WALKIE",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "Walkie-talkie activity"),
            "device_type": str(event.get("device_type") or "Walkie-talkie"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("rssi_dbfs") or event.get("signal_dbfs"),
            "center_freq_hz": frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_mhz": event.get("frequency_mhz"),
            "confidence": event.get("confidence"),
            "classification": event.get("classification"),
            "modulation": event.get("modulation"),
            "signal_dbfs": event.get("signal_dbfs"),
            "audio_rms_dbfs": event.get("audio_rms_dbfs"),
            "audio_bandwidth_hz": event.get("audio_bandwidth_hz"),
            "voice_band_ratio": event.get("voice_band_ratio"),
            "voice_activity_ratio": event.get("voice_activity_ratio"),
            "occupied_ratio": event.get("occupied_ratio"),
            "freq_std_hz": event.get("freq_std_hz"),
            "saved_iq_path": event.get("saved_iq_path"),
            "saved_meta_path": event.get("saved_meta_path"),
            "saved_wav_path": event.get("saved_wav_path"),
        }
    elif event.get("kind") == "wifi_frame":
        identity = str(event.get("identity") or event.get("ssid") or event.get("mac") or "WiFi frame")
        ssid = _clean_wifi_ssid(event.get("ssid"))
        ssid_visible = bool(event.get("ssid_visible")) and bool(ssid)
        frame_type = str(event.get("device_type_detail") or event.get("detail") or "wifi").strip()
        mac = str(event.get("mac") or "").strip()
        bssid = str(event.get("bssid") or "").strip()
        role = str(event.get("wifi_role") or "station").strip().lower()
        if role not in {"ap", "station"}:
            role = "station"
        source_address = str(event.get("source_address") or "").strip()
        destination_address = str(event.get("destination_address") or "").strip()
        key_mac = (bssid or mac or source_address or identity) if role == "ap" else (source_address or mac or destination_address or identity)
        device_type = "Access Point" if role == "ap" else "Station"
        if role == "ap" and not ssid_visible:
            identity = f"Hidden SSID {key_mac}".strip()
        detail = (
            f"Hidden/unnamed SSID · {frame_type}"
            if role == "ap" and not ssid_visible
            else f"{device_type} {frame_type}".strip()
        )
        identity_source = str(
            event.get("identity_source")
            or ("No SSID value observed; showing BSSID/MAC identifier." if role == "ap" and not ssid_visible else "")
        )
        row = {
            "key": f"wifi:{role}:{key_mac}:{ssid if role == 'ap' else ''}",
            "protocol": "WIFI",
            "identity": identity,
            "mac": mac or bssid,
            "wifi_role": role,
            "source_address": source_address,
            "destination_address": destination_address,
            "bssid": bssid,
            "ssid": ssid,
            "ssid_visible": ssid_visible,
            "name": ssid if role == "ap" and ssid_visible else "",
            "detail": detail,
            "device_type": device_type,
            "device_type_detail": f"Hidden/unnamed SSID · {frame_type}" if role == "ap" and not ssid_visible else frame_type,
            "identity_source": identity_source,
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("rssi_dbm"),
            "rssi_dbm": event.get("rssi_dbm"),
            "channel": event.get("channel"),
            "center_freq_hz": event.get("center_freq_hz"),
        }
    elif event.get("kind") == "fm_station":
        identity = str(event.get("identity") or "FM station")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        row = {
            "key": f"fm:{frequency_hz or identity}",
            "protocol": "FM",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "FM broadcast station"),
            "device_type": str(event.get("device_type") or "Broadcast FM"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("power_dbfs"),
            "center_freq_hz": frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_mhz": event.get("frequency_mhz"),
            "power_dbfs": event.get("power_dbfs"),
            "noise_dbfs": event.get("noise_dbfs"),
            "excess_db": event.get("excess_db"),
            "audio_rms": event.get("audio_rms"),
            "pilot_db": event.get("pilot_db"),
            "rds_subcarrier_db": event.get("rds_subcarrier_db"),
            "stereo_likely": event.get("stereo_likely"),
            "rds_likely": event.get("rds_likely"),
        }
    elif event.get("kind") == "lfmf_signal":
        identity = str(event.get("identity") or "VLF/LF/MF signal")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        row = {
            "key": f"lfmf:{frequency_hz or identity}",
            "protocol": "LFMF",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "VLF/LF/MF signal"),
            "device_type": str(event.get("device_type") or "VLF/LF/MF signal"),
            "device_type_detail": str(event.get("device_type_detail") or ""),
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("carrier_dbfs") or event.get("power_dbfs"),
            "center_freq_hz": frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_khz": event.get("frequency_khz"),
            "power_dbfs": event.get("power_dbfs"),
            "carrier_dbfs": event.get("carrier_dbfs"),
            "carrier_snr_db": event.get("carrier_snr_db"),
            "excess_db": event.get("excess_db"),
            "audio_dbfs": event.get("audio_dbfs"),
            "modulation_pct": event.get("modulation_pct"),
            "band": event.get("band"),
            "band_label": event.get("band_label"),
            "active": event.get("active"),
        }
    elif event.get("kind") == "cellular_signal":
        identity = str(event.get("identity") or "Cellular activity")
        frequency_hz = event.get("frequency_hz") or event.get("center_freq_hz")
        band = str(event.get("band") or "Cellular spectrum").strip()
        cellular_type = str(event.get("cellular_type") or event.get("technology") or "Cellular").strip()
        operator = str(event.get("likely_operator") or "").strip()
        row = {
            "key": f"cellular:{frequency_hz or identity}:{event.get('link') or 'unknown'}",
            "protocol": "CELLULAR",
            "identity": identity,
            "mac": str(frequency_hz or identity),
            "detail": str(event.get("detail") or "Passive cellular spectrum awareness"),
            "device_type": str(event.get("device_type") or "Cellular"),
            "device_type_detail": cellular_type,
            "manufacturer": {"name": operator, "company_name": operator} if operator else None,
            "detections": 1,
            "last_seen_at": now,
            "last_rssi_dbfs": event.get("last_rssi_dbfs") or event.get("power_dbfs") or event.get("rssi_dbfs"),
            "center_freq_hz": event.get("center_freq_hz") or frequency_hz,
            "frequency_hz": frequency_hz,
            "frequency_mhz": event.get("frequency_mhz"),
            "power_dbfs": event.get("power_dbfs"),
            "noise_floor_dbfs": event.get("noise_floor_dbfs"),
            "excess_db": event.get("excess_db"),
            "occupied_width_hz": event.get("occupied_width_hz"),
            "band": band,
            "link": event.get("link"),
            "cellular_type": cellular_type,
            "technology": event.get("technology"),
            "likely_operator": operator,
            "operator_confidence": event.get("operator_confidence"),
            "likely_mcc": event.get("likely_mcc"),
            "likely_mnc": event.get("likely_mnc"),
            "likely_plmn": event.get("likely_plmn"),
            "plmn_source": event.get("plmn_source"),
            "decoded_mcc": event.get("decoded_mcc"),
            "decoded_mnc": event.get("decoded_mnc"),
            "decoded_plmn": event.get("decoded_plmn"),
            "decoded_plmn_source": event.get("decoded_plmn_source"),
            "lte_sync_status": event.get("lte_sync_status"),
            "lte_pss_detected": event.get("lte_pss_detected"),
            "lte_n_id_2": event.get("lte_n_id_2"),
            "lte_pss_metric": event.get("lte_pss_metric"),
            "lte_pss_freq_offset_hz": event.get("lte_pss_freq_offset_hz"),
            "lte_cell_id_status": event.get("lte_cell_id_status"),
            "lte_mib_status": event.get("lte_mib_status"),
            "lte_sib1_status": event.get("lte_sib1_status"),
            "classification": event.get("classification"),
            "target": event.get("target"),
            "passive_only": event.get("passive_only", True),
            "content_decoded": event.get("content_decoded", False),
        }
    else:
        return

    event_seen_dates = [
        str(date)
        for date in (event.get("seen_dates") or [])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date))
    ]
    row.setdefault("first_seen_at", now)
    historical_dates = [*event_seen_dates, *_seen_history_dates_for_row(row)]
    row.update(_seen_day_stats([*historical_dates, seen_date]))
    for idx, existing in enumerate(state.discovery_table):
        same_classic_lap = row.get("protocol") == "BTC" and existing.get("protocol") == "BTC" and existing.get("lap") == row.get("lap")
        if existing.get("key") != row["key"] and not same_classic_lap:
            continue
        row["detections"] = int(existing.get("detections") or 0) + 1
        row["first_seen_at"] = float(existing.get("first_seen_at") or row.get("first_seen_at") or now)
        prior_dates = list(existing.get("seen_dates") or [])
        if not prior_dates and existing.get("last_seen_at"):
            prior_dates.append(_utc_date_key(existing.get("last_seen_at")))
        prior_dates.extend(event_seen_dates)
        prior_dates.extend(_seen_history_dates_for_row({**existing, **row}))
        prior_dates.append(seen_date)
        row.update(_seen_day_stats(prior_dates))
        if row.get("protocol") == "BTC":
            existing_uap = str(existing.get("uap") or "").upper()
            row_uap = str(row.get("uap") or "").upper()
            if existing_uap not in {"", "XX", "XXX", "NONE"} and row_uap in {"", "XX", "XXX", "NONE"}:
                row["uap"] = existing_uap
            existing_nap = str(existing.get("nap") or "").upper()
            row_nap = str(row.get("nap") or "").upper()
            if existing_nap not in {"", "XXXX", "XX:XX", "NONE"} and row_nap in {"", "XXXX", "XX:XX", "NONE"}:
                row["nap"] = existing_nap
            row["full_mac"] = _classic_full_mac(row.get("nap"), row.get("uap"), row.get("lap"))
            row["mac"] = row["full_mac"] if _classic_uap_resolved(row.get("uap")) else f"LAP {row.get('lap')}"
            row["identity"] = _classic_display_identity(
                row.get("lap"),
                row.get("uap"),
                row.get("nap"),
                row.get("full_mac"),
                row.get("status"),
                row.get("name") or existing.get("name"),
            )
            row.setdefault("device_type", existing.get("device_type") or "Bluetooth Classic")
            row.setdefault("device_type_detail", existing.get("device_type_detail") or _classic_evidence_label(row.get("status"), row.get("event_type")))
            row.setdefault("identity_source", existing.get("identity_source") or _classic_identity_source(row, str(row.get("lap") or ""), str(row.get("uap") or "")))
            if existing.get("active_piconet") and "active_piconet" not in row:
                row["active_piconet"] = existing.get("active_piconet")
            if existing.get("target") and not row.get("target"):
                row["target"] = True
            if row.get("target"):
                row["identity"] = f"TEST DONGLE {row['identity']}"
            if not row.get("detail") and existing.get("detail"):
                row["detail"] = existing["detail"]
        if not row.get("name") and existing.get("name"):
            row["name"] = existing["name"]
        if row.get("protocol") == "BTC" and row.get("name"):
            row["identity"] = str(row.get("name") or "")
            row.setdefault("device_type", "Bluetooth Classic")
        if row.get("protocol") == "BTLE" and row.get("key") != "ble:activity":
            row["uuid16"] = list(dict.fromkeys([*(existing.get("uuid16") or []), *(row.get("uuid16") or [])]))
            row["uuid16_names"] = _uuid16_names(row["uuid16"])
            if not row.get("manufacturer") and existing.get("manufacturer"):
                row["manufacturer"] = existing["manufacturer"]
            if not row.get("manufacturer"):
                row["manufacturer"] = _manufacturer_from_uuid16(row["uuid16"])
            if not row.get("appearance") and existing.get("appearance"):
                row["appearance"] = existing["appearance"]
            row["identity_source"] = _ble_identity_source(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
            )
            row["device_type"] = _ble_device_type_label(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
            )
            row["device_type_detail"] = _ble_device_type_detail(
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                row.get("appearance") if isinstance(row.get("appearance"), dict) else None,
            )
        if row.get("key") == "ble:activity":
            row["identity"] = "Bluetooth Low Energy"
        elif row.get("protocol") == "BTLE" and row.get("name"):
            row["identity"] = row["name"]
        elif row.get("protocol") == "BTLE":
            row["identity"] = _ble_identity_label(
                str(row.get("name") or ""),
                row.get("uuid16_names") or [],
                row.get("manufacturer") if isinstance(row.get("manufacturer"), dict) else None,
                str(row.get("mac") or ""),
            )
        _maybe_schedule_btc_name_lookup(row)
        state.discovery_table[idx] = row
        break
    else:
        _maybe_schedule_btc_name_lookup(row)
        state.discovery_table.insert(0, row)
    state.discovery_table.sort(key=lambda item: float(item.get("last_seen_at") or 0), reverse=True)
    state.discovery_table = state.discovery_table[:RF_SENTINEL_DISCOVERY_TABLE_MAX_ROWS]


def _upsert_classic_address(event: dict[str, Any]) -> None:
    lap = str(event.get("lap", "")).strip()
    if not lap:
        return
    now = float(event.get("seen_at", time.time()))
    uap = str(event.get("uap") or "XX")
    nap = str(event.get("nap") or "XXXX")
    full_mac = _classic_full_mac(nap, uap, lap)
    target = _classic_test_match(lap, uap)
    row = {
        "lap": lap,
        "uap": uap,
        "nap": nap,
        "full_mac": full_mac,
        "mac": full_mac,
        "status": "target-match" if target else str(event.get("status") or "observed"),
        "target": bool(target),
        "candidate_count": int(event.get("candidate_count") or 0),
        "processed_packets": int(event.get("processed_packets") or 0),
        "broken_packets": int(event.get("broken_packets") or 0),
        "cannot_init": int(event.get("cannot_init") or 0),
        "repaired": bool(event.get("repaired", False)),
        "repair_distance": int(event.get("repair_distance") or 0),
        "header_perfect_triplets": int(event.get("header_perfect_triplets") or 0),
        "header_relaxed": bool(event.get("header_relaxed", False)),
        "channel": event.get("channel"),
        "center_freq_hz": event.get("center_freq_hz"),
        "rssi_dbfs": event.get("rssi_dbfs"),
        "first_seen_at": now,
        "last_seen_at": now,
        "seen_count": 1,
    }
    for idx, existing in enumerate(state.classic_addresses):
        if existing.get("lap") != lap:
            continue
        row["first_seen_at"] = existing.get("first_seen_at", now)
        row["seen_count"] = int(existing.get("seen_count") or 0) + 1
        if existing.get("uap") not in {"", None, "XX", "XXX"} and row["uap"] in {"XX", "XXX"}:
            row["uap"] = existing["uap"]
        if existing.get("nap") not in {"", None, "XXXX", "XX:XX"} and row["nap"] == "XXXX":
            row["nap"] = existing["nap"]
        row["full_mac"] = _classic_full_mac(row.get("nap"), row.get("uap"), row.get("lap"))
        row["mac"] = row["full_mac"]
        row["processed_packets"] = max(int(existing.get("processed_packets") or 0), row["processed_packets"])
        row["broken_packets"] = max(int(existing.get("broken_packets") or 0), row["broken_packets"])
        row["cannot_init"] = max(int(existing.get("cannot_init") or 0), row["cannot_init"])
        state.classic_addresses[idx] = row
        break
    else:
        state.classic_addresses.insert(0, row)
    state.classic_addresses.sort(key=lambda item: float(item.get("last_seen_at") or 0), reverse=True)
    state.classic_addresses = state.classic_addresses[:96]


def _upsert_channel_activity(event: dict[str, Any]) -> None:
    try:
        channel = int(event.get("channel"))
    except (TypeError, ValueError):
        return
    if channel not in BT_CLASSIC_CHANNELS:
        return
    row = state.channel_activity.get(channel, {"channel": channel, "hits": 0, "rssi_dbfs": -120.0})
    row["hits"] = int(row.get("hits") or 0) + 1
    row["rssi_dbfs"] = event.get("rssi_dbfs", row.get("rssi_dbfs", -120.0))
    row["last_seen_at"] = event.get("seen_at", time.time())
    state.channel_activity[channel] = row


def _classic_test_match(lap: str, uap: str = "XXX") -> dict[str, Any] | None:
    target = state.test_target or {}
    if target.get("protocol") != "BTC":
        return None
    if str(target.get("lap") or "").upper() != str(lap or "").upper():
        return None
    target_uap = str(target.get("uap") or "").upper()
    observed_uap = str(uap or "XXX").upper()
    if observed_uap not in {"", "XXX"} and target_uap and observed_uap != target_uap:
        return None
    return target


def _classic_target_from_mac(mac: str) -> dict[str, Any]:
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(clean) != 12:
        raise ValueError(f"Invalid Bluetooth MAC: {mac}")
    return {
        "protocol": "BTC",
        "mac": ":".join(clean[idx : idx + 2] for idx in range(0, 12, 2)),
        "nap": clean[0:4],
        "uap": clean[4:6],
        "lap": clean[6:12],
        "enabled_at": time.time(),
    }


def _bluetooth_controllers() -> list[dict[str, str]]:
    proc = subprocess.run(
        ["bluetoothctl", "list"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    controllers: list[dict[str, str]] = []
    for match in re.finditer(r"Controller\s+([0-9A-Fa-f:]{17})(?:\s+(.+))?", output):
        controllers.append({"mac": match.group(1).upper(), "name": (match.group(2) or "").strip()})
    return controllers


def _enable_discoverable_controller(controller_mac: str | None = None) -> tuple[dict[str, Any], str]:
    select_cmd = [f"select {controller_mac}"] if controller_mac else []
    commands = "\n".join(
        select_cmd
        + [
            "power on",
            "agent on",
            "default-agent",
            "pairable on",
            "discoverable-timeout 0",
            "discoverable on",
            "show",
            "exit",
        ]
    )
    proc = subprocess.run(
        ["bluetoothctl"],
        input=commands + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})", output)
    if not match:
        raise RuntimeError(output.strip() or "bluetoothctl did not report a controller address")
    target = _classic_target_from_mac(match.group(1))
    target["controller"] = match.group(1).upper()
    target["discoverable"] = "Discoverable: yes" in output or "Changing discoverable on succeeded" in output
    target["bluetoothctl_returncode"] = proc.returncode
    return target, output


def _start_bredr_inquiry(exclude_controller: str | None = None) -> tuple[dict[str, Any] | None, str]:
    global inquiry_process
    _stop_bredr_inquiry()
    controllers = _bluetooth_controllers()
    helper = next(
        (controller for controller in controllers if controller["mac"].upper() != str(exclude_controller or "").upper()),
        None,
    )
    if helper is None:
        return None, "No second Bluetooth controller available for active BR/EDR inquiry"
    commands = "\n".join(
        [
            f"select {helper['mac']}",
            "power on",
            "scan bredr on",
        ]
    )
    inquiry_process = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if inquiry_process.stdin:
        inquiry_process.stdin.write(commands + "\n")
        inquiry_process.stdin.flush()
    return helper, f"BR/EDR inquiry running on {helper['mac']}"


def _stop_bredr_inquiry() -> None:
    global inquiry_process
    proc = inquiry_process
    inquiry_process = None
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.write("scan off\nexit\n")
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.terminate()


def _stream_active(stream_id: str) -> bool:
    return state.stream_id == stream_id or stream_id in set(state.stream_ids.values())


def _worker_loop(
    stream_id: str,
    sample_rate_sps: int,
    mode: str,
    center_freq_hz: int,
    channel: int,
    stop_event: threading.Event | None = None,
) -> None:
    stop = stop_event or worker_stop
    if mode == "both" and sample_rate_sps >= 10_000_000:
        detector = CombinedBluetoothDetector(sample_rate_sps, center_freq_hz, channel)
    elif mode == "classic" and sample_rate_sps >= 10_000_000:
        detector = WideClassicDetector(sample_rate_sps, center_freq_hz, channel)
    else:
        detector = BluetoothDetector(sample_rate_sps, mode, center_freq_hz, channel)
    headers = []
    token = _gateway_token()
    if token:
        headers.append(f"Authorization: Bearer {token}")
    with state_lock:
        state.worker_alive = True
        state.worker_alive_by_mode[mode] = True
        state.worker_error = "Worker starting"
        state.worker_errors[mode] = "Worker starting"
    try:
        while not stop.is_set():
            ws = websocket.WebSocket()
            try:
                ws.connect(_ws_url_for_stream(stream_id), timeout=8, header=headers)
                ws.settimeout(1.0)
                with state_lock:
                    state.worker_error = ""
                    state.worker_errors[mode] = ""
                while not stop.is_set() and _stream_active(stream_id):
                    try:
                        chunk = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except WebSocketConnectionClosedException:
                        with state_lock:
                            state.worker_error = "Gateway websocket closed; reconnecting"
                            state.worker_errors[mode] = "Gateway websocket closed; reconnecting"
                        break
                    except Exception as exc:
                        with state_lock:
                            state.worker_error = f"Worker recv error: {exc}; reconnecting"
                            state.worker_errors[mode] = f"Worker recv error: {exc}; reconnecting"
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    try:
                        rssi, events, candidates = detector.process_iq_i8(bytes(chunk))
                    except Exception as exc:
                        with state_lock:
                            state.worker_error = f"Detector error: {exc}"
                            state.worker_errors[mode] = f"Detector error: {exc}"
                        break
                    with state_lock:
                        state.chunks_seen += 1
                        state.bytes_seen += len(chunk)
                        state.chunks_by_mode[mode] = int(state.chunks_by_mode.get(mode, 0)) + 1
                        state.bytes_by_mode[mode] = int(state.bytes_by_mode.get(mode, 0)) + len(chunk)
                        state.rssi_by_mode[mode] = round(rssi, 1)
                        state.last_rssi_dbfs = round(rssi, 1)
                        state.noise_floor_dbfs = round((state.noise_floor_dbfs * 0.92) + (rssi * 0.08), 1)
                        if hasattr(detector, "stats"):
                            for key, value in detector.stats.items():
                                if key == "target_access_best_distance":
                                    current = int(state.decoder_stats.get(key, 68))
                                    state.decoder_stats[key] = min(current, int(value))
                                    detector.stats[key] = 68
                                    continue
                                state.decoder_stats[key] = int(state.decoder_stats.get(key, 0)) + int(value)
                                detector.stats[key] = 0
                    _append_detections(events, candidates)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            if not stop.is_set() and _stream_active(stream_id):
                stop.wait(0.75)
    finally:
        with state_lock:
            state.worker_alive_by_mode[mode] = False
            state.worker_alive = any(state.worker_alive_by_mode.values())
            if _stream_active(stream_id) and not stop.is_set() and not state.worker_errors.get(mode):
                state.worker_errors[mode] = "Worker exited unexpectedly"
                state.worker_error = "Worker exited unexpectedly"


def _rf_sentinel_scan_bin() -> str:
    candidate = Path(sys.executable).parent / "rf_sentinel_scan"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("rf_sentinel_scan")
    if found:
        return found
    return str(candidate)


def _read_rf_sentinel_control() -> dict[str, Any]:
    try:
        payload = json.loads(RF_SENTINEL_CONTROL_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_rf_sentinel_control(
    enabled_protocols: set[str] | None = None,
    *,
    enabled_devices: set[str] | None = None,
    protocol_devices: dict[str, str] | None = None,
    wifi_channels: list[int] | None = None,
    bluetooth_classic: dict[str, Any] | None = None,
    zigbee_follow_channel: int | None | object = RF_SENTINEL_NO_CHANGE,
    zigbee_follow_device_id: str | None | object = RF_SENTINEL_NO_CHANGE,
) -> dict[str, Any]:
    RF_SENTINEL_CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_rf_sentinel_control()
    if enabled_protocols is not None:
        payload["protocols"] = sorted(enabled_protocols & RF_SENTINEL_PROTOCOLS)
    if enabled_devices is not None:
        payload["devices"] = sorted(str(item).strip() for item in enabled_devices if str(item).strip())
    elif enabled_protocols is not None:
        payload.pop("devices", None)
    if protocol_devices is not None:
        payload["protocol_devices"] = {
            str(protocol).strip().lower(): str(device_id).strip()
            for protocol, device_id in protocol_devices.items()
            if str(protocol).strip().lower() in PROTOCOL_DEVICE_OVERRIDES and str(device_id).strip()
        }
    if wifi_channels is not None:
        payload["wifi_channels"] = [
            int(channel)
            for channel in wifi_channels
            if int(channel) in WIFI_SUPPORTED_CHANNELS
        ] or [1, 6, 11]
    if bluetooth_classic is not None:
        payload["bluetooth_classic"] = _clean_bluetooth_classic_config(bluetooth_classic)
    if zigbee_follow_channel is not RF_SENTINEL_NO_CHANGE:
        follow = payload.get("follow")
        if not isinstance(follow, dict):
            follow = {}
        if isinstance(zigbee_follow_channel, int):
            existing_zigbee = follow.get("zigbee") if isinstance(follow.get("zigbee"), dict) else {}
            follow["zigbee"] = {**existing_zigbee, "channel": zigbee_follow_channel}
        else:
            follow.pop("zigbee", None)
        payload["follow"] = follow
    if zigbee_follow_device_id is not RF_SENTINEL_NO_CHANGE:
        follow = payload.get("follow")
        if not isinstance(follow, dict):
            follow = {}
        zigbee = follow.get("zigbee") if isinstance(follow.get("zigbee"), dict) else {}
        device_id = str(zigbee_follow_device_id or "").strip()
        if device_id:
            zigbee["device_id"] = device_id
            follow["zigbee"] = zigbee
        elif zigbee:
            zigbee.pop("device_id", None)
            if zigbee:
                follow["zigbee"] = zigbee
            else:
                follow.pop("zigbee", None)
        payload["follow"] = follow
    tmp_path = RF_SENTINEL_CONTROL_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(RF_SENTINEL_CONTROL_PATH)
    return payload


def _clean_bluetooth_classic_config(raw: Any) -> dict[str, bool]:
    value = raw if isinstance(raw, dict) else {}
    return {
        "log_passive_fhs_bdaddr": bool(value.get("log_passive_fhs_bdaddr")),
        "periodic_page_scan": bool(value.get("periodic_page_scan")),
        "band_hop": bool(value.get("band_hop")),
        "remote_name_lookup": bool(value.get("remote_name_lookup")),
    }


def _follow_state_for_protocols(control: dict[str, Any], protocols: set[str]) -> dict[str, Any]:
    if "zigbee" not in protocols:
        return {}
    follow = control.get("follow") if isinstance(control.get("follow"), dict) else {}
    zigbee = follow.get("zigbee") if isinstance(follow.get("zigbee"), dict) else None
    return {"zigbee": zigbee} if zigbee else {}


def _read_ui_config() -> dict[str, Any]:
    try:
        payload = json.loads(RF_SENTINEL_UI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    protocols = payload.get("protocols")
    if not isinstance(protocols, list):
        protocols = sorted(RF_SENTINEL_PROTOCOLS)
    disabled = payload.get("disabled_devices")
    if not isinstance(disabled, list):
        disabled = []
    channels = payload.get("wifi_channels")
    if not isinstance(channels, list):
        channels = [1, 6, 11]
    wifi_channels: list[int] = []
    for channel in channels:
        try:
            channel_int = int(channel)
        except (TypeError, ValueError):
            continue
        if channel_int in WIFI_SUPPORTED_CHANNELS:
            wifi_channels.append(channel_int)
    protocol_devices_raw = payload.get("protocol_devices")
    protocol_devices: dict[str, str] = {}
    if isinstance(protocol_devices_raw, dict):
        for protocol, device_id in protocol_devices_raw.items():
            protocol_key = str(protocol).strip().lower()
            device_text = str(device_id).strip()
            if protocol_key in PROTOCOL_DEVICE_OVERRIDES and device_text:
                protocol_devices[protocol_key] = device_text
    bluetooth_classic = _clean_bluetooth_classic_config(payload.get("bluetooth_classic"))
    return {
        "protocols": sorted({str(item).strip().lower() for item in protocols} & RF_SENTINEL_PROTOCOLS),
        "disabled_devices": sorted({str(item).strip() for item in disabled if str(item).strip()}),
        "wifi_channels": wifi_channels or [1, 6, 11],
        "protocol_devices": protocol_devices,
        "bluetooth_classic": bluetooth_classic,
    }


def _clean_wifi_channels(channels: Any) -> list[int]:
    clean: list[int] = []
    for channel in channels if isinstance(channels, list) else []:
        try:
            channel_int = int(channel)
        except (TypeError, ValueError):
            continue
        if channel_int in WIFI_SUPPORTED_CHANNELS and channel_int not in clean:
            clean.append(channel_int)
    return clean or [1, 6, 11]


def _write_ui_config(
    protocols: set[str],
    disabled_devices: set[str],
    *,
    wifi_channels: list[int] | None = None,
    protocol_devices: dict[str, str] | None = None,
    bluetooth_classic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    RF_SENTINEL_UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_ui_config()
    selected_wifi_channels = wifi_channels if wifi_channels is not None else existing.get("wifi_channels", [1, 6, 11])
    clean_wifi_channels = _clean_wifi_channels(selected_wifi_channels)
    selected_protocol_devices = protocol_devices if protocol_devices is not None else existing.get("protocol_devices", {})
    clean_protocol_devices = {
        str(protocol).strip().lower(): str(device_id).strip()
        for protocol, device_id in dict(selected_protocol_devices or {}).items()
        if str(protocol).strip().lower() in PROTOCOL_DEVICE_OVERRIDES and str(device_id).strip()
    }
    selected_bluetooth_classic = bluetooth_classic if bluetooth_classic is not None else existing.get("bluetooth_classic", {})
    payload = {
        "protocols": sorted(protocols & RF_SENTINEL_PROTOCOLS),
        "disabled_devices": sorted(str(item).strip() for item in disabled_devices if str(item).strip()),
        "wifi_channels": clean_wifi_channels,
        "protocol_devices": clean_protocol_devices,
        "bluetooth_classic": _clean_bluetooth_classic_config(selected_bluetooth_classic),
        "updated_at": time.time(),
    }
    tmp_path = RF_SENTINEL_UI_CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp_path.replace(RF_SENTINEL_UI_CONFIG_PATH)
    return payload


def _enabled_devices_from_disabled(devices: list[dict[str, Any]], disabled_devices: set[str]) -> set[str]:
    return {
        str(item.get("id") or "").strip()
        for item in devices
        if str(item.get("id") or "").strip() and str(item.get("id") or "").strip() not in disabled_devices
    }


def _clean_protocol_devices(raw: Any, enabled_devices: set[str], reserved_devices: set[str] | None = None) -> dict[str, str]:
    reserved = {str(item).strip() for item in (reserved_devices or set()) if str(item).strip()}
    clean: dict[str, str] = {}
    if not isinstance(raw, dict):
        return clean
    for protocol, device_id in raw.items():
        protocol_key = str(protocol).strip().lower()
        device_text = str(device_id).strip()
        if protocol_key not in PROTOCOL_DEVICE_OVERRIDES or not device_text:
            continue
        if device_text not in enabled_devices or device_text in reserved:
            continue
        clean[protocol_key] = device_text
    return clean


def _has_wifi_device(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> bool:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        text = f"{device_id} {item.get('label') or ''} {item.get('driver') or ''}".lower()
        if "wlan" in text or "wifi" in text or "802.11" in text:
            return True
    return False


def _wifi_interface_from_devices(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> str:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        text = f"{device_id} {item.get('label') or ''} {item.get('driver') or ''}".lower()
        if "wlan" in text or "wifi" in text or "802.11" in text:
            return device_id
    return ""


def _is_sdrplay_device(item: dict[str, Any]) -> bool:
    text = f"{item.get('id') or ''} {item.get('label') or ''} {item.get('driver') or ''}".lower()
    return "sdrplay" in text and ("rsp2" in text or str(item.get("id") or "").lower().startswith("sdrplay:"))


def _is_rtlsdr_device(item: dict[str, Any]) -> bool:
    text = f"{item.get('id') or ''} {item.get('label') or ''} {item.get('driver') or ''}".lower()
    return "rtlsdr" in text or "rtl-sdr" in text or str(item.get("id") or "").lower().startswith("rtlsdr:")


def _is_bladerf_device(item: dict[str, Any]) -> bool:
    text = f"{item.get('id') or ''} {item.get('label') or ''} {item.get('driver') or ''}".lower()
    return "bladerf" in text or str(item.get("id") or "").lower().startswith("bladerf:")


def _fm_device_from_devices(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> str:
    for predicate in (_is_sdrplay_device, _is_rtlsdr_device):
        for item in devices:
            device_id = str(item.get("id") or "").strip()
            if enabled_devices is not None and device_id not in enabled_devices:
                continue
            if predicate(item):
                return device_id
    return ""


def _fm_device_for_sentinel(
    devices: list[dict[str, Any]],
    enabled_devices: set[str] | None,
    services_device_id: str,
) -> str:
    services_device_id = str(services_device_id or "").strip()
    if services_device_id:
        item = next((dev for dev in devices if str(dev.get("id") or "").strip() == services_device_id), None)
        if item is not None and _is_bladerf_device(item):
            return services_device_id
    return _fm_device_from_devices(devices, enabled_devices)


def _has_lfmf_device(devices: list[dict[str, Any]], enabled_devices: set[str] | None = None) -> bool:
    for item in devices:
        device_id = str(item.get("id") or "").strip()
        if enabled_devices is not None and device_id not in enabled_devices:
            continue
        if _is_sdrplay_device(item):
            return True
    return False


def _terminate_process_group(proc: subprocess.Popen[str], timeout_s: float = 4.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        proc.terminate()
    deadline = time.time() + timeout_s
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            proc.kill()
        proc.wait(timeout=1)


def _parse_rf_sentinel_line(line: str) -> tuple[str, str]:
    match = re.match(r"^\[(?P<source>[^\]]+)\]\s*(?P<body>.*)$", line.strip())
    if not match:
        return "scanner", line.strip()
    return match.group("source").strip(), match.group("body").strip()


def _append_scanner_log(line: str) -> None:
    text = str(line or "").rstrip()
    if not text:
        return
    state.scanner_log.append(text)
    state.scanner_log = state.scanner_log[-300:]
    assignment = _parse_scanner_assignment(text)
    if assignment:
        state.scanner_assignments[assignment["device_id"]] = assignment
    _update_scanner_assignment_from_log(text)
    _console_append_log(text)


def _touch_scanner_assignment(source: str, payload: dict[str, Any], events: list[dict[str, Any]]) -> None:
    source_name = str(source or "").strip()
    if not source_name:
        return
    now = time.time()
    protocol = ""
    if events:
        protocol = str(events[0].get("protocol") or "").strip().lower()
    if not protocol:
        protocol = str(payload.get("protocol") or "").strip().lower()
    if protocol == "btle":
        protocol = "ble"
    if protocol == "ieee802154":
        protocol = "zigbee"
    with state_lock:
        for assignment in state.scanner_assignments.values():
            if str(assignment.get("job_name") or "") != source_name:
                continue
            assignment["seen_at"] = now
            if protocol:
                assignment["last_protocol"] = protocol
            if payload.get("center_freq_hz") is not None:
                assignment["last_center_freq_hz"] = payload.get("center_freq_hz")
            if payload.get("frequency_hz") is not None:
                assignment["last_frequency_hz"] = payload.get("frequency_hz")
            return


def _update_scanner_assignment_from_log(line: str) -> None:
    source, body = _parse_rf_sentinel_line(line)
    center_match = re.search(r"\bretuned\s+center=(?P<mhz>[0-9.]+)MHz\b", body, re.IGNORECASE)
    if not center_match:
        return
    try:
        center_hz = int(round(float(center_match.group("mhz")) * 1_000_000.0))
    except (TypeError, ValueError):
        return
    now = time.time()
    with state_lock:
        for assignment in state.scanner_assignments.values():
            if str(assignment.get("job_name") or "").strip() != source:
                continue
            assignment["seen_at"] = now
            assignment["last_center_freq_hz"] = center_hz
            return


def _scanner_protocol_from_job_name(job_name: str) -> str:
    if "zigbee-follow" in str(job_name or "").lower():
        return "zigbee"
    parts = [part for part in str(job_name or "").split(":") if part]
    if not parts:
        return ""
    protocol = parts[-1]
    if protocol == "bluetooth":
        protocol = "btc"
    if protocol.startswith("follow"):
        protocol = "zigbee"
    return protocol.lower()


def _scanner_band_from_command(command: str, protocol: str) -> str:
    text = str(command or "")
    if protocol in {"btc", "btle+btc", "btc+btle"}:
        match = re.search(r"--center-mhz\s+([0-9.]+)\s+--bandwidth-mhz\s+([0-9]+)", text)
        if match:
            if "bluetooth_scanner" in text or protocol in {"btle+btc", "btc+btle"}:
                return f"2.4 GHz ISM shared BTC+BLE · {match.group(1)} MHz / {match.group(2)} MHz BW"
            return f"{match.group(1)} MHz / {match.group(2)} MHz BW"
    if protocol == "ble":
        if "iq-sweep" in text:
            return "BLE adv 37/38/39"
        match = re.search(r"--channel\s+([0-9]+)", text)
        if match:
            return f"BLE CH {match.group(1)}"
    if protocol == "zigbee":
        match = re.search(r"--channel\s+([0-9]+)", text)
        if match:
            return f"Zigbee CH {match.group(1)}"
        match = re.search(r"--sample-rate-sps\s+([0-9]+)", text)
        if match:
            return f"Zigbee wideband {int(match.group(1)) / 1_000_000:.1f} Msps"
        return "Zigbee wideband"
    if protocol == "tpms":
        if "--auto-hop-known" in text:
            return "315 / 433.92 MHz"
    if protocol == "walkie":
        match = re.search(r"--center-freq-hz\s+([0-9]+)", text)
        if match:
            return f"{int(match.group(1)) / 1_000_000.0:.3f} MHz".replace(".000 MHz", " MHz")
        return "462.500 MHz"
    if protocol == "fm":
        return "87.7-107.9 MHz"
    if protocol == "lfmf":
        match = re.search(r"--band\s+(\S+)", text)
        if match:
            band = match.group(1)
            if band == "vlf-lf-mf":
                return "VLF/LF/MF 3 kHz-3 MHz"
            if band == "1khz-1mhz":
                return "VLF/LF/lower-MF 1 kHz-1 MHz"
            return band.upper()
        return "VLF/LF/MF"
    if protocol == "wifi":
        match = re.search(r"--channels\s+([0-9,]+)", text)
        if match:
            return f"WiFi CH {match.group(1)}"
        return "WiFi monitor"
    return ""


def _console_center_from_assignment(assignment: dict[str, Any], protocol: str) -> str:
    command = str(assignment.get("command") or "")
    band = str(assignment.get("band") or "")
    protocol = str(protocol or "").lower()
    last_hz = assignment.get("last_center_freq_hz") or assignment.get("last_frequency_hz")
    try:
        if last_hz is not None:
            return f"{float(last_hz) / 1_000_000.0:.3f} MHz".replace(".000 MHz", " MHz")
    except (TypeError, ValueError):
        pass
    match = re.search(r"--center-mhz\s+([0-9.]+)", command)
    if match:
        return f"{float(match.group(1)):.3f} MHz".replace(".000 MHz", " MHz")
    match = re.search(r"--center-freq-hz\s+([0-9]+)", command)
    if match:
        return f"{int(match.group(1)) / 1_000_000.0:.3f} MHz".replace(".000 MHz", " MHz")
    match = re.search(r"--target-freq-hz\s+([0-9]+)", command)
    if match:
        return f"{int(match.group(1)) / 1_000_000.0:.3f} MHz".replace(".000 MHz", " MHz")
    match = re.search(r"--channel\s+([0-9]+)", command)
    if match and protocol == "zigbee":
        channel = int(match.group(1))
        return f"CH {channel} / {2405 + ((channel - 11) * 5)} MHz"
    match = re.search(r"CH\s*([0-9,]+)", band, re.IGNORECASE)
    if match:
        return f"CH {match.group(1)}"
    if protocol == "fm":
        return "87.7-107.9 MHz"
    if protocol == "lfmf":
        return "1 kHz-3 MHz"
    return "-"


def _console_append_log(line: str) -> None:
    text = str(line or "").strip()
    if not text:
        return
    if not CONSOLE_DASHBOARD:
        print(text, flush=True)
        return
    timestamp = time.strftime("%H:%M:%S")
    with console_lock:
        console_log_lines.append(f"{timestamp} {text}")
        del console_log_lines[:-CONSOLE_LOG_BUFFER_LINES]
    if console_textual_active.is_set():
        return
    _console_render()


def _console_protocol_count(protocol: str) -> int:
    protocol = str(protocol or "").upper()
    if "+" in protocol:
        return sum(_console_protocol_count(part) for part in protocol.split("+") if part)
    aliases = {
        "BLE": {"BTLE"},
        "BTLE": {"BTLE"},
        "BTC": {"BTC"},
        "ZIGBEE": {"ZIGBEE"},
        "TPMS": {"TPMS"},
        "WALKIE": {"WALKIE"},
        "WIFI": {"WIFI"},
        "FM": {"FM"},
        "LFMF": {"LFMF"},
        "CELLULAR": {"CELLULAR"},
    }.get(protocol, {protocol})
    total = 0
    for row in state.discovery_table:
        if str(row.get("protocol") or "").upper() in aliases:
            total += max(1, int(row.get("detections") or 0))
    if protocol == "BTC":
        total = max(total, int(state.classic_bursts_seen or 0))
    elif protocol in {"BLE", "BTLE"}:
        total = max(total, int(state.ble_packets_seen or 0))
    if total > 0 and protocol not in {"BTC", "BLE", "BTLE"}:
        return total
    source_needles = {
        "BTC": ("btc", "classic"),
        "BLE": ("ble", "btle"),
        "BTLE": ("ble", "btle"),
        "ZIGBEE": ("zigbee", "802154"),
        "TPMS": ("tpms", "subghz"),
        "WALKIE": ("walkie",),
        "WIFI": ("wifi",),
        "FM": ("fm",),
        "LFMF": ("lfmf", "lowfreq"),
        "CELLULAR": ("cellular",),
    }.get(protocol, (protocol.lower(),))
    activity = 0
    for source, count in state.chunks_by_mode.items():
        source_l = str(source or "").lower()
        if any(needle in source_l for needle in source_needles):
            activity += int(count or 0)
    return max(total, activity)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _c(code: str, text: str) -> str:
    if not CONSOLE_COLOR:
        return str(text)
    return f"\033[{code}m{text}\033[0m"


def _console_protocol_color(protocol: str) -> str:
    protocol = str(protocol or "").upper().split("+", 1)[0]
    return {
        "BTC": "96;1",
        "BLE": "34;1",
        "BTLE": "34;1",
        "ZIGBEE": "35;1",
        "TPMS": "33;1",
        "WALKIE": "33;1",
        "WALKIE-AUDIO": "33;1",
        "WIFI": "32;1",
        "FM": "33;1",
        "FM-AUDIO": "33;1",
        "LFMF": "36;1",
        "CELLULAR": "31;1",
    }.get(protocol, "37;1")


def _console_page_activity_lines(*, color: bool = True, limit: int = 6) -> list[str]:
    now = time.time()
    items = sorted(
        list(state.page_activity.values()),
        key=lambda item: float(item.get("last_seen") or 0.0),
        reverse=True,
    )[:limit]

    def style(code: str, text: str) -> str:
        return _c(code, text) if color else str(text)

    lines = [style("90;1", "Page / Inquiry Activity:")]
    if not items:
        lines.append(style("90", "  no page/inquiry access codes yet"))
        return lines
    for item in items:
        lap = str(item.get("lap") or "------")
        channel = str(item.get("channel") if item.get("channel") is not None else "?")
        hits = int(item.get("hits") or 0)
        age = max(0.0, now - float(item.get("last_seen") or now))
        try:
            rssi = float(item.get("rssi_dbfs"))
            rssi_text = f"{rssi:.1f} dBFS"
        except (TypeError, ValueError):
            rssi_text = "RSSI ?"
        lines.append(
            f"  LAP {style('96;1', lap)}  CH {channel:>2}  "
            f"{style('32;1', str(hits))} hits  {rssi_text}  {age:.1f}s ago"
        )
    return lines


def _console_log_style(line: str) -> str:
    if re.search(r"\b(ERROR|failed|exception|traceback)\b", line, re.IGNORECASE):
        return _c("31;1", line)
    if re.search(r"\b(WARNING|WARN|busy|conflict)\b", line, re.IGNORECASE):
        return _c("33;1", line)
    if re.search(r"\b(started|running|active|restored|released)\b", line, re.IGNORECASE):
        return _c("32", line)
    return _c("37", line)


def _console_is_wifi_device(device: dict[str, Any]) -> bool:
    text = f"{device.get('id') or ''} {device.get('label') or ''} {device.get('driver') or ''}".lower()
    return "wlan" in text or "wifi" in text or "802.11" in text


def _console_first_enabled(enabled: set[str], choices: tuple[str, ...]) -> str:
    for choice in choices:
        if choice.lower() in enabled:
            return choice.upper()
    return ""


def _console_assignment_rows() -> list[dict[str, Any]]:
    rows_by_device: dict[str, dict[str, Any]] = {}
    assignments = dict(state.scanner_assignments or {})
    for device_id in sorted(assignments):
        assignment = assignments[device_id]
        protocol = str(assignment.get("protocol") or "rf").upper()
        band = str(assignment.get("band") or "scanning")
        detections = _console_protocol_count(protocol)
        age = max(0, int(time.time() - float(assignment.get("seen_at") or time.time())))
        center = _console_center_from_assignment(assignment, protocol)
        rows_by_device[device_id] = {"device": device_id, "protocol": protocol, "center": center, "count": detections, "band": f"{band} ({age}s)", "active": True}
    if fm_playback.running or fm_playback.pending:
        status = "pending" if fm_playback.pending else "playing"
        device_id = fm_playback.pending_device_id or fm_playback.device_id or "fm-sdr"
        freq = fm_playback.pending_freq_mhz or fm_playback.freq_mhz
        rows_by_device[f"{device_id}:fm-audio"] = {"device": device_id, "protocol": "FM-AUDIO", "center": f"{freq:.1f} MHz", "count": fm_playback.produced_chunks, "band": status, "active": True}
    if walkie_playback.running or walkie_playback.pending:
        status = "pending" if walkie_playback.pending else "playing"
        device_id = walkie_playback.pending_device_id or walkie_playback.device_id or "subghz-sdr"
        freq = walkie_playback.pending_freq_mhz or walkie_playback.freq_mhz
        rows_by_device[f"{device_id}:walkie-audio"] = {"device": device_id, "protocol": "WALKIE-AUDIO", "center": f"{freq:.3f} MHz", "count": walkie_playback.produced_chunks, "band": status, "active": True}
    enabled = {str(item).lower() for item in state.decoder_stats.get("enabled_protocols", [])}
    protocol_devices = _read_ui_config().get("protocol_devices", {})
    if not isinstance(protocol_devices, dict):
        protocol_devices = {}
    configured_subghz_device = str(protocol_devices.get("tpms") or protocol_devices.get("walkie") or "").strip()
    configured_fm_device = str(protocol_devices.get("fm") or "").strip()
    devices, _ = _cached_gateway_devices()
    auto_fm_device = configured_fm_device
    if not auto_fm_device and "fm" in enabled:
        for candidate in devices:
            candidate_id = str(candidate.get("id") or "").strip()
            if candidate_id and candidate_id != configured_subghz_device and _is_sdrplay_device(candidate):
                auto_fm_device = candidate_id
                break
        if not auto_fm_device:
            for candidate in devices:
                candidate_id = str(candidate.get("id") or "").strip()
                if candidate_id and candidate_id != configured_subghz_device and _is_rtlsdr_device(candidate):
                    auto_fm_device = candidate_id
                    break
    for device in devices:
        device_id = str(device.get("id") or "").strip()
        if not device_id or device_id in rows_by_device:
            continue
        if _console_is_wifi_device(device):
            if "wifi" in enabled:
                rows_by_device[device_id] = {"device": device_id, "protocol": "WIFI", "center": "CH 1,6,11", "count": _console_protocol_count("WIFI"), "band": "WiFi monitor (ready)", "active": False}
            continue
        if _is_sdrplay_device(device):
            protocol = _console_first_enabled(enabled, ("fm", "lfmf"))
            if protocol:
                capability = "FM first; LFMF next" if {"fm", "lfmf"} <= enabled else ("FM broadcast" if protocol == "FM" else "VLF/LF/MF survey")
                center = "87.7-107.9 MHz" if protocol == "FM" else "1 kHz-3 MHz"
                rows_by_device[device_id] = {"device": device_id, "protocol": protocol, "center": center, "count": _console_protocol_count(protocol), "band": f"{capability} (ready)", "active": False}
            continue
        driver_text = f"{device_id} {device.get('label') or ''} {device.get('driver') or ''}".lower()
        if "bladerf" in driver_text:
            role = str((state.device_ids or {}).get("radio_b") or (state.device_ids or {}).get("hop") or "").strip()
            if device_id == role:
                protocol = _console_first_enabled(enabled, ("zigbee", "tpms", "walkie", "cellular", "fm"))
                if protocol:
                    center = {
                        "ZIGBEE": "2405-2480 MHz",
                        "TPMS": "315/433.92 MHz",
                        "WALKIE": "462.500 MHz",
                        "CELLULAR": "751 MHz",
                        "FM": "87.7-107.9 MHz",
                    }.get(protocol, "-")
                    rows_by_device[device_id] = {
                        "device": device_id,
                        "protocol": protocol,
                        "center": center,
                        "count": _console_protocol_count(protocol),
                        "band": "FM / cellular / walkie / Zigbee / TPMS rotation (ready)",
                        "active": False,
                    }
                continue
            protocol = _console_first_enabled(enabled, ("btc", "ble"))
            if protocol:
                capability = "2.4 GHz ISM shared BTC+BLE" if {"btc", "ble"} <= enabled else ("Bluetooth Classic" if protocol == "BTC" else "BLE advertisements")
                rows_by_device[device_id] = {"device": device_id, "protocol": protocol, "center": "2442 MHz", "count": _console_protocol_count(protocol), "band": f"{capability} (ready)", "active": False}
            continue
        if "hackrf" in driver_text:
            protocol = _console_first_enabled(enabled, ("zigbee", "tpms", "walkie", "cellular"))
            if protocol:
                center = {"ZIGBEE": "2405-2480 MHz", "TPMS": "315/433.92 MHz", "WALKIE": "462.500 MHz", "CELLULAR": "751 MHz"}.get(protocol, "-")
                rows_by_device[device_id] = {"device": device_id, "protocol": protocol, "center": center, "count": _console_protocol_count(protocol), "band": "Zigbee / TPMS / walkie / cellular hop stack (ready)", "active": False}
            continue
        if "rtlsdr" in driver_text or "rtl-sdr" in driver_text:
            if configured_subghz_device and device_id == configured_subghz_device and {"tpms", "walkie"} & enabled:
                protocol = _console_first_enabled(enabled, ("tpms", "walkie"))
            elif auto_fm_device and device_id == auto_fm_device and "fm" in enabled:
                protocol = "FM"
            elif configured_subghz_device or configured_fm_device:
                protocol = ""
            else:
                protocol = _console_first_enabled(enabled, ("tpms", "walkie", "fm"))
            if protocol:
                center = "315/433.92 MHz" if protocol == "TPMS" else ("462.500 MHz" if protocol == "WALKIE" else "87.7-107.9 MHz")
                rows_by_device[device_id] = {"device": device_id, "protocol": protocol, "center": center, "count": _console_protocol_count(protocol), "band": "TPMS / walkie / FM capable (ready)", "active": False}
    if not rows_by_device:
        return [{"device": "No active scanner assignments yet.", "protocol": "", "center": "", "count": "", "band": ""}]
    def sort_key(row: dict[str, Any]) -> tuple[int, str]:
        protocol = str(row.get("protocol") or "")
        device = str(row.get("device") or "")
        priority = 0 if row.get("active") else 1
        if protocol == "WIFI":
            priority = min(priority, 1)
        return (priority, device)
    rows = list(rows_by_device.values())
    return sorted(rows, key=sort_key)


def _console_term_width() -> int:
    return max(72, min(180, shutil.get_terminal_size((120, 32)).columns - 1))


def _console_term_height() -> int:
    return max(24, min(80, shutil.get_terminal_size((120, 32)).lines))


def _console_fit(text: str, width: int) -> str:
    clean = str(text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    visible = ANSI_RE.sub("", clean)
    if len(visible) > width:
        clean = visible[: max(0, width - 3)] + "..."
        visible = clean
    return clean + (" " * max(0, width - len(visible)))


def _console_scrollbar(total: int, visible: int) -> list[str]:
    visible = max(1, int(visible))
    if total <= visible:
        return [" "] * visible
    thumb = max(1, min(visible, int(round((visible / max(1, total)) * visible))))
    top = visible - thumb
    return ["█" if top <= idx < top + thumb else "│" for idx in range(visible)]


def _console_box(
    title: str,
    lines: list[str],
    width: int,
    height: int | None = None,
    scrollbar: bool = False,
    total_lines: int | None = None,
    border_color: str = "36;1",
) -> list[str]:
    width = max(40, int(width))
    title_text = f" {title.strip()} "
    top = _c(border_color, "┏" + title_text + ("━" * max(0, width - len(title_text) - 2)) + "┓")
    bottom = _c(border_color, "┗" + ("━" * max(0, width - 2)) + "┛")
    side = _c(border_color, "┃")
    if height is not None:
        body = list(lines[-height:])
        while len(body) < height:
            body.insert(0, "")
    else:
        body = list(lines)
    total = int(total_lines if total_lines is not None else len(lines))
    show_scrollbar = bool(scrollbar and total > len(body))
    bar = _console_scrollbar(total, len(body)) if show_scrollbar else [""] * len(body)
    content_width = width - (5 if show_scrollbar else 4)
    rendered = [top]
    for idx, line in enumerate(body):
        scroll = _c(border_color, bar[idx]) if bar[idx] == "█" else _c("90", bar[idx])
        if show_scrollbar:
            rendered.append(f"{side} {_console_fit(line, content_width)}{scroll}{side}")
        else:
            rendered.append(f"{side} {_console_fit(line, content_width)} {side}")
    rendered.append(bottom)
    return rendered


def _console_join_columns(left: list[str], right: list[str], gap: int = 2) -> list[str]:
    height = max(len(left), len(right))
    left_width = max((len(ANSI_RE.sub("", line)) for line in left), default=0)
    blank_left = " " * left_width
    joined: list[str] = []
    for idx in range(height):
        left_line = left[idx] if idx < len(left) else blank_left
        right_line = right[idx] if idx < len(right) else ""
        left_visible = len(ANSI_RE.sub("", left_line))
        joined.append(f"{left_line}{' ' * max(0, left_width - left_visible + gap)}{right_line}")
    return joined


def _console_packet_info_lines(running: str, enabled: str, logs: list[str]) -> list[str]:
    total_detections = sum(max(1, int(row.get("detections") or 0)) for row in state.discovery_table)
    lines = [
        _c("90;1", "=== RF Activity ==="),
        f"Total Detections:  {_c('32;1', str(total_detections))}",
        f"Classic Bursts:    {_c('96;1', str(int(state.classic_bursts_seen or 0)))}",
        f"BLE Packets:       {_c('34;1', str(int(state.ble_packets_seen or 0)))}",
        f"Chunks Seen:       {_c('37;1', str(int(state.chunks_seen or 0)))}",
        f"Last RSSI:         {_c('33;1', f'{float(state.last_rssi_dbfs or -120.0):.1f} dBFS')}",
        "",
        _c("90;1", "Settings:"),
        f"  state: {running}",
        f"  enabled: {enabled}",
        f"  csv run: {state.csv_run_id or '-'}",
        "",
        _c("90;1", "Audio:"),
        f"  FM: {_c('32;1', 'playing') if fm_playback.running else ('pending' if fm_playback.pending else 'idle')}",
        f"  Walkie: {_c('32;1', 'playing') if walkie_playback.running else ('pending' if walkie_playback.pending else 'idle')}",
        f"  Walkie recent chunks: {walkie_playback.recent_chunks}",
        "",
        *_console_page_activity_lines(color=True),
        "",
        _c("90;1", "Recent Backend Log:"),
    ]
    lines.extend(logs or [_c("90", "  (no backend log messages yet)")])
    return lines


def _console_channel_stats_lines() -> list[str]:
    protocol_order = ["BTC", "BTLE", "ZIGBEE", "TPMS", "WALKIE", "WIFI", "FM", "LFMF", "CELLULAR"]
    lines = [_c("90;1", "=== Protocol Counters ===")]
    for protocol in protocol_order:
        count = _console_protocol_count(protocol)
        if count <= 0 and protocol not in {str(item).upper() for item in state.decoder_stats.get("enabled_protocols", [])}:
            continue
        label = _c(_console_protocol_color(protocol), f"{protocol:<8}")
        lines.append(f"  {label} | {count:>8}")
    if len(lines) == 1:
        lines.append(_c("90", "  no protocol activity yet"))
    lines.append("")
    lines.append(_c("90;1", "Mode Counters:"))
    for mode, chunks in sorted((state.chunks_by_mode or {}).items(), key=lambda item: str(item[0])):
        rssi = state.rssi_by_mode.get(mode, -120.0)
        lines.append(f"  {str(mode)[:18]:<18} | {int(chunks or 0):>7} | {float(rssi):>6.1f} dBFS")
    if not state.chunks_by_mode:
        lines.append(_c("90", "  no stream chunks yet"))
    return lines


def _textual_default_scan_payload() -> dict[str, Any]:
    config = _read_ui_config()
    devices_available = _available_devices()
    disabled_devices = {str(item).strip() for item in config.get("disabled_devices", []) if str(item).strip()}
    enabled_devices = _enabled_devices_from_disabled(devices_available, disabled_devices)
    protocols = sorted((set(config.get("protocols", [])) or RF_SENTINEL_PROTOCOLS) & RF_SENTINEL_PROTOCOLS)
    btc_device_id = _pick_ism24_bluetooth_device(devices_available, enabled_devices)
    if not btc_device_id:
        btc_device_id = _pick_device(devices_available, "bladerf")
    hop_device_id = _pick_non_bluetooth_hop_device(devices_available, btc_device_id, enabled_devices)
    if not hop_device_id:
        hop_device_id = _pick_device(devices_available, "hackrf", btc_device_id or "sidekiq")
    return {
        "device_id": btc_device_id or hop_device_id,
        "btc_device_id": btc_device_id,
        "btle_device_id": hop_device_id or btc_device_id,
        "btc_engine": BTC_ENGINE_DEFAULT,
        "btc_center_mhz": 2442.0,
        "btc_target_mac": "",
        "mode": "sentinel",
        "hop_device_id": hop_device_id or btc_device_id,
        "channel": 37,
        "ble_channel": 37,
        "sample_rate_sps": 60_000_000,
        "lna_gain_db": 40,
        "vga_gain_db": 32,
        "btc_lna_gain_db": 40,
        "btc_vga_gain_db": 32,
        "btle_lna_gain_db": 40,
        "btle_vga_gain_db": 16,
        "preserve_detections": False,
        "protocols": protocols,
        "devices": sorted(enabled_devices),
        "wifi_channels": config.get("wifi_channels", [1, 6, 11]),
        "protocol_devices": config.get("protocol_devices", {}),
        "sweep_both_radios": False,
    }


def _console_sdr_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        _c("90;1", f"{'DEVICE':<18} | {'PROTO':<12} | {'CENTER':<18} | {'COUNT':>8} | BAND"),
        _c("32;1", "━" * 78),
    ]
    for row in rows:
        device = str(row.get("device") or "")
        protocol = str(row.get("protocol") or "")
        center = str(row.get("center") or "-")
        count = row.get("count")
        band = str(row.get("band") or "")
        if not protocol:
            lines.append(_c("90", device))
            continue
        chip = _c(_console_protocol_color(protocol), f"{protocol:<12}")
        center_text = _c("36", f"{center:<18}")
        count_text = f"{int(count):>8}" if isinstance(count, int) else f"{str(count):>8}"
        lines.append(f"{_c('37;1', f'{device:<18}')} | {chip} | {center_text} | {_c('32;1', count_text)} | {_c('37', band)}")
    return lines


def _console_render(force: bool = False) -> None:
    global console_last_render
    if not CONSOLE_DASHBOARD:
        return
    now = time.time()
    if not force and now - console_last_render < CONSOLE_REFRESH_S:
        return
    with console_lock:
        console_last_render = now
        all_logs = list(console_log_lines)
        logs = all_logs[-CONSOLE_LOG_VIEW_LINES:]
    with state_lock:
        rows = _console_assignment_rows()
        running = "running" if state.running else "idle"
        enabled = ", ".join(str(item).upper() for item in state.decoder_stats.get("enabled_protocols", [])) or "-"
    width = _console_term_width()
    height = _console_term_height()
    log_lines = [_console_log_style(line) for line in (logs or ["(no backend log messages yet)"])]
    top_panel_height = max(9, min(18, (height - 8) // 2))
    bottom_panel_height = max(8, height - top_panel_height - 7)
    gap = 2
    left_width = max(40, (width - gap) // 2)
    right_width = max(40, width - left_width - gap)
    packet_lines = _console_packet_info_lines(running, enabled, log_lines)
    channel_lines = _console_channel_stats_lines()
    sdr_lines = _console_sdr_lines(rows)
    left_panel = _console_box(
        "RF Activity",
        packet_lines,
        left_width,
        height=top_panel_height,
        scrollbar=True,
        total_lines=len(packet_lines),
        border_color="34;1",
    )
    right_panel = _console_box(
        "Protocol Counters",
        channel_lines,
        right_width,
        height=top_panel_height,
        scrollbar=True,
        total_lines=len(channel_lines),
        border_color="35;1",
    )
    bottom_panel = _console_box(
        "Radios / Current Work",
        sdr_lines,
        width,
        height=bottom_panel_height,
        scrollbar=True,
        total_lines=len(sdr_lines),
        border_color="32;1",
    )
    output = [
        "\033[2J\033[H",
        _c("36;1", "RF Sentinel backend").ljust(width),
        f"{_c('90', 'State:')} {_c('32;1' if running == 'running' else '33;1', running)}   {_c('90', 'Enabled:')} {_c('37', enabled)}",
        *_console_join_columns(left_panel, right_panel, gap=gap),
        *bottom_panel,
        "",
    ]
    try:
        sys.stdout.write("\n".join(output))
        sys.stdout.flush()
    except Exception:
        pass


def _console_dashboard_loop() -> None:
    while not console_dashboard_stop.wait(CONSOLE_REFRESH_S):
        _console_render(force=True)


if TextualApp is not None:
    class RFSentinelConsoleApp(TextualApp[None]):
        CSS = """
        Screen {
            layout: vertical;
            background: #071011;
            color: #d7e7df;
        }

        Header, Footer {
            background: #0b1d1f;
            color: #d7e7df;
        }

        #summary {
            height: 3;
            padding: 0 1;
            content-align: left middle;
            background: #10282b;
            color: #d7e7df;
        }

        #top-row {
            height: 1fr;
            min-height: 12;
        }

        #bottom-row {
            height: 2fr;
            min-height: 10;
        }

        .pane {
            height: 100%;
            border: solid $accent;
            padding: 0 1;
        }

        #activity-pane {
            width: 1fr;
            border-title-color: #7aa2ff;
            border: solid #406ee8;
        }

        #protocol-pane {
            width: 1fr;
            border-title-color: #ff7ad9;
            border: solid #a84fd7;
        }

        #log-pane {
            width: 3fr;
            border-title-color: #7aa2ff;
            border: solid #406ee8;
        }

        #radios-pane {
            width: 2fr;
            border-title-color: #58d68d;
            border: solid #2fae66;
        }

        Static {
            scrollbar-color: #58d68d #10282b;
            scrollbar-size: 1 1;
        }

        RichLog {
            background: #081719;
            scrollbar-color: #7aa2ff #10282b;
            scrollbar-size: 1 1;
            overflow-x: scroll;
            overflow-y: scroll;
        }

        DataTable {
            background: #081719;
            color: #d7e7df;
            scrollbar-color: #58d68d #10282b;
            scrollbar-size: 1 1;
        }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "toggle_scan", "Start/Stop"),
            ("u", "refresh_now", "Refresh"),
            ("l", "focus_log", "Log"),
            ("d", "focus_radios", "Radios"),
            ("=", "widen_log", "Widen Log"),
            ("-", "narrow_log", "Narrow Log"),
            ("b", "toggle_bottom_space", "Bottom Size"),
        ]

        def __init__(self, *, host: str = "", port: int | str = "") -> None:
            super().__init__()
            self.host = str(host or "")
            self.port = str(port or "")
            self._log_seen = 0
            self._log_weight = 3
            self._radios_weight = 2
            self._bottom_large = True
            self._scan_toggle_pending = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("Starting RF Sentinel backend...", id="summary")
            with Horizontal(id="top-row"):
                yield Static("", id="activity-pane", classes="pane")
                yield DataTable(id="protocol-pane", classes="pane")
            with Horizontal(id="bottom-row"):
                yield RichLog(id="log-pane", classes="pane", highlight=True, markup=True, wrap=False, min_width=140)
                yield DataTable(id="radios-pane", classes="pane")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "RF Sentinel Console"
            self.sub_title = f"http://{self.host or '127.0.0.1'}:{self.port or '5050'}"
            self.query_one("#activity-pane", Static).border_title = "RF Activity"
            self.query_one("#protocol-pane", DataTable).border_title = "Protocol Counters"
            log_pane = self.query_one("#log-pane", RichLog)
            log_pane.border_title = "Backend Log"
            log_pane.wrap = False
            log_pane.min_width = 140
            log_pane.styles.overflow_x = "scroll"
            log_pane.styles.overflow_y = "scroll"
            log_pane.show_horizontal_scrollbar = True
            log_pane.show_vertical_scrollbar = True
            self.query_one("#radios-pane", DataTable).border_title = "Radios / Current Work"
            protocols = self.query_one("#protocol-pane", DataTable)
            protocols.cursor_type = "row"
            protocols.zebra_stripes = True
            protocols.add_columns("Protocol", "Count", "Status")
            radios = self.query_one("#radios-pane", DataTable)
            radios.cursor_type = "row"
            radios.zebra_stripes = True
            radios.add_columns("Device", "Protocol", "Center", "Count", "Band")
            self._apply_layout_weights()
            self.set_interval(0.5, self._refresh)
            self._refresh()

        def action_refresh_now(self) -> None:
            self._refresh()

        def action_toggle_scan(self) -> None:
            if self._scan_toggle_pending:
                _console_append_log("[tui] scan toggle already in progress")
                return
            self._scan_toggle_pending = True
            threading.Thread(target=self._toggle_scan_worker, daemon=True).start()

        def action_focus_log(self) -> None:
            self.query_one("#log-pane", RichLog).focus()

        def action_focus_radios(self) -> None:
            self.query_one("#radios-pane", DataTable).focus()

        def action_widen_log(self) -> None:
            self._log_weight = min(6, self._log_weight + 1)
            self._radios_weight = max(1, self._radios_weight - 1) if self._log_weight >= 4 else self._radios_weight
            self._apply_layout_weights()

        def action_narrow_log(self) -> None:
            self._log_weight = max(1, self._log_weight - 1)
            self._radios_weight = min(5, self._radios_weight + 1)
            self._apply_layout_weights()

        def action_toggle_bottom_space(self) -> None:
            self._bottom_large = not self._bottom_large
            self._apply_layout_weights()

        def _apply_layout_weights(self) -> None:
            top_row = self.query_one("#top-row")
            bottom_row = self.query_one("#bottom-row")
            log_pane = self.query_one("#log-pane", RichLog)
            radios_pane = self.query_one("#radios-pane", DataTable)
            top_row.styles.height = "1fr"
            bottom_row.styles.height = "3fr" if self._bottom_large else "2fr"
            log_pane.styles.width = f"{self._log_weight}fr"
            radios_pane.styles.width = f"{self._radios_weight}fr"

        def _toggle_scan_worker(self) -> None:
            try:
                with state_lock:
                    running = bool(state.running)
                if running:
                    _stop_scan()
                    _console_append_log("[tui] stopped RF Sentinel scan")
                    return
                payload = _textual_default_scan_payload()
                _console_append_log("[tui] starting RF Sentinel scan")
                with app.test_request_context("/api/scan/start", method="POST", json=payload):
                    response = app.make_response(start_scan())
                data = response.get_json(silent=True) or {}
                if response.status_code >= 400:
                    detail = data.get("detail") or data.get("error") or f"HTTP {response.status_code}"
                    raise RuntimeError(str(detail))
                mode = data.get("mode") or payload.get("mode", "sentinel")
                _console_append_log(f"[tui] started RF Sentinel scan mode={mode}")
            except Exception as exc:
                _console_append_log(f"[tui] scan toggle failed: {exc}")
            finally:
                with contextlib.suppress(Exception):
                    self.call_from_thread(self._scan_toggle_done)

        def _scan_toggle_done(self) -> None:
            self._scan_toggle_pending = False
            self._refresh()

        def _refresh(self) -> None:
            with console_lock:
                logs = list(console_log_lines)
            with state_lock:
                running = "running" if state.running else "idle"
                enabled_items = [str(item).upper() for item in state.decoder_stats.get("enabled_protocols", [])]
                enabled = ", ".join(enabled_items) or "-"
                rows = _console_assignment_rows()
                activity = {
                    "total_detections": sum(max(1, int(row.get("detections") or 0)) for row in state.discovery_table),
                    "classic_bursts": int(state.classic_bursts_seen or 0),
                    "ble_packets": int(state.ble_packets_seen or 0),
                    "chunks_seen": int(state.chunks_seen or 0),
                    "last_rssi": float(state.last_rssi_dbfs or -120.0),
                    "csv_run_id": state.csv_run_id or "-",
                    "fm_state": "playing" if fm_playback.running else ("pending" if fm_playback.pending else "idle"),
                    "walkie_state": "playing" if walkie_playback.running else ("pending" if walkie_playback.pending else "idle"),
                    "walkie_recent": int(walkie_playback.recent_chunks or 0),
                    "chunks_by_mode": dict(state.chunks_by_mode or {}),
                    "page_lines": _console_page_activity_lines(color=False),
                }
            self.query_one("#summary", Static).update(
                f"RF Sentinel {running} | enabled {enabled} | web http://{self.host or '127.0.0.1'}:{self.port or '5050'} | "
                "r start/stop, u refresh, Tab focus, =/- resize log, b bottom size, q quit"
            )
            self.query_one("#activity-pane", Static).update(self._activity_text(activity, running, enabled))
            self._refresh_protocol_table(enabled_items, activity)
            self._refresh_radio_table(rows)
            self._refresh_log(logs)

        def _activity_text(self, activity: dict[str, Any], running: str, enabled: str) -> str:
            return "\n".join(
                [
                    "=== RF Activity ===",
                    f"Total Detections:  {activity['total_detections']}",
                    f"Classic Bursts:    {activity['classic_bursts']}",
                    f"BLE Packets:       {activity['ble_packets']}",
                    f"Chunks Seen:       {activity['chunks_seen']}",
                    f"Last RSSI:         {activity['last_rssi']:.1f} dBFS",
                    "",
                    "Settings:",
                    f"  state: {running}",
                    f"  enabled: {enabled}",
                    f"  csv run: {activity['csv_run_id']}",
                    "",
                    "Audio:",
                    f"  FM: {activity['fm_state']}",
                    f"  Walkie: {activity['walkie_state']}",
                    f"  Walkie recent chunks: {activity['walkie_recent']}",
                    "",
                    *activity.get("page_lines", []),
                ]
            )

        def _refresh_protocol_table(self, enabled_items: list[str], activity: dict[str, Any]) -> None:
            protocols = self.query_one("#protocol-pane", DataTable)
            protocols.clear(columns=False)
            enabled_set = set(enabled_items)
            for protocol in ["BTC", "BTLE", "ZIGBEE", "TPMS", "WALKIE", "WIFI", "FM", "LFMF", "CELLULAR"]:
                count = _console_protocol_count(protocol)
                if count <= 0 and protocol not in enabled_set:
                    continue
                status = "enabled" if protocol in enabled_set else "seen"
                protocols.add_row(protocol, str(count), status)
            for mode, chunks in sorted(activity.get("chunks_by_mode", {}).items(), key=lambda item: str(item[0])):
                protocols.add_row(str(mode)[:22], str(int(chunks or 0)), "chunks")

        def _refresh_radio_table(self, rows: list[dict[str, Any]]) -> None:
            radios = self.query_one("#radios-pane", DataTable)
            radios.clear(columns=False)
            for row in rows:
                radios.add_row(
                    str(row.get("device") or ""),
                    str(row.get("protocol") or ""),
                    str(row.get("center") or ""),
                    str(row.get("count") or ""),
                    str(row.get("band") or ""),
                )

        def _refresh_log(self, logs: list[str]) -> None:
            log = self.query_one("#log-pane", RichLog)
            if self._log_seen > len(logs):
                log.clear()
                self._log_seen = 0
            for line in logs[self._log_seen :]:
                text = str(line)
                if re.search(r"\b(ERROR|failed|exception|traceback)\b", text, re.IGNORECASE):
                    log.write(Text.from_markup(f"[bold red]{text}[/]"))
                elif re.search(r"\b(WARNING|WARN|busy|conflict)\b", text, re.IGNORECASE):
                    log.write(Text.from_markup(f"[yellow]{text}[/]"))
                elif re.search(r"\b(started|running|active|restored|released)\b", text, re.IGNORECASE):
                    log.write(Text.from_markup(f"[green]{text}[/]"))
                else:
                    log.write(text)
            self._log_seen = len(logs)


def run_textual_console_dashboard(host: str = "", port: int | str = "") -> bool:
    if TextualApp is None or not CONSOLE_DASHBOARD:
        return False
    console_textual_active.set()
    try:
        label = f"RF Sentinel UI starting on {host}:{port}" if host or port else "RF Sentinel UI starting"
        _console_append_log(label)
        RFSentinelConsoleApp(host=host, port=port).run(mouse=True)
    finally:
        console_textual_active.clear()
    return True


def start_console_dashboard(host: str = "", port: int | str = "") -> None:
    global console_dashboard_thread
    if not CONSOLE_DASHBOARD:
        return
    if console_dashboard_thread and console_dashboard_thread.is_alive():
        return
    label = f"RF Sentinel UI starting on {host}:{port}" if host or port else "RF Sentinel UI starting"
    _console_append_log(label)
    console_dashboard_stop.clear()
    console_dashboard_thread = threading.Thread(target=_console_dashboard_loop, daemon=True)
    console_dashboard_thread.start()
    _console_render(force=True)


def stop_console_dashboard() -> None:
    console_dashboard_stop.set()


class _ConsoleDashboardLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _console_append_log(self.format(record))
        except Exception:
            self.handleError(record)


if CONSOLE_DASHBOARD:
    _console_handler = _ConsoleDashboardLogHandler()
    _console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    app.logger.handlers = [_console_handler]
    app.logger.propagate = False
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.handlers = [_console_handler]
    werkzeug_logger.propagate = False


def _parse_scanner_assignment(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    auto_match = re.search(
        r"^\[rf-sentinel\]\s+auto\s+device=(?P<device>\S+)\s+job=(?P<job>\S+)\s+dwell_s=(?P<dwell>[0-9.]+):\s+(?P<command>.+)$",
        text,
    )
    if auto_match:
        job_name = auto_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        command = auto_match.group("command")
        if "bluetooth_scanner" in command:
            protocol = "btle+btc"
        return {
            "device_id": auto_match.group("device"),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": float(auto_match.group("dwell")),
            "seen_at": time.time(),
            "mode": "auto",
        }
    cont_match = re.search(r"^\[rf-sentinel\]\s+starting\s+continuous\s+(?P<job>\S+):\s+(?P<command>.+)$", text)
    if cont_match:
        command = cont_match.group("command")
        device_match = re.search(r"--device-id\s+(\S+)", command)
        if not device_match:
            return None
        job_name = cont_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        if "bluetooth_scanner" in command:
            protocol = "btle+btc"
        return {
            "device_id": device_match.group(1),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": 0.0,
            "seen_at": time.time(),
            "mode": "continuous",
        }
    hop_match = re.search(
        r"^\[rf-sentinel\]\s+hop\s+group=(?P<group>\S+)\s+job=(?P<job>\S+)\s+dwell_s=(?P<dwell>[0-9.]+):\s+(?P<command>.+)$",
        text,
    )
    if hop_match:
        command = hop_match.group("command")
        job_name = hop_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        device_match = re.search(r"--device-id\s+(\S+)", command)
        if protocol == "wifi" and not device_match:
            device_match = re.search(r"--interface\s+(\S+)", command)
        if not device_match:
            return None
        return {
            "device_id": device_match.group(1),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": float(hop_match.group("dwell")),
            "seen_at": time.time(),
            "mode": "hop",
        }
    sidecar_match = re.search(
        r"^\[rf-sentinel\]\s+sidecar\s+group=(?P<group>\S+)\s+job=(?P<job>\S+)\s+dwell_s=(?P<dwell>\S+):\s+(?P<command>.+)$",
        text,
    )
    if sidecar_match:
        command = sidecar_match.group("command")
        job_name = sidecar_match.group("job")
        protocol = _scanner_protocol_from_job_name(job_name)
        device_match = re.search(r"--device-id\s+(\S+)", command)
        if not device_match:
            return None
        dwell_text = sidecar_match.group("dwell")
        dwell_s = 0.0 if dwell_text == "continuous" else float(dwell_text)
        return {
            "device_id": device_match.group(1),
            "job_name": job_name,
            "protocol": protocol,
            "band": _scanner_band_from_command(command, protocol),
            "command": command,
            "dwell_s": dwell_s,
            "seen_at": time.time(),
            "mode": "sidecar",
        }
    return None


def _clean_device_id(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text or lowered in {"loading...", "loading", "no sdr devices"}:
        return ""
    if "sdr-gateway is unavailable" in lowered or "no devices" in lowered:
        return ""
    return text


def _rf_sentinel_loop(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    with state_lock:
        state.worker_alive = True
        state.worker_alive_by_mode["scanner"] = True
        state.worker_error = ""
        state.worker_errors["scanner"] = ""
    try:
        for raw_line in proc.stdout:
            if rf_sentinel_stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            source, body = _parse_rf_sentinel_line(line)
            payload = None
            if body.startswith("{"):
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
            # JSON lines are detections/metrics. Keep ingesting them, but do
            # not print every packet into the backend console.
            noisy_packet_line = payload is not None
            with state_lock:
                if not noisy_packet_line:
                    _append_scanner_log(line)
                state.chunks_by_mode[source] = int(state.chunks_by_mode.get(source, 0)) + 1
                state.chunks_seen += 1
            if payload is None:
                continue
            events = _scanner_json_to_events(source, payload)
            for event in events:
                event.setdefault("scanner_source", source)
            _touch_scanner_assignment(source, payload, events)
            if events:
                _append_detections(events, [])
        rc = proc.wait()
        if not rf_sentinel_stop.is_set() and rc not in (0, -signal.SIGTERM):
            with state_lock:
                state.worker_error = f"RF Sentinel scanner exited with code {rc}"
                state.worker_errors["scanner"] = state.worker_error
                _append_scanner_log(state.worker_error)
    finally:
        with state_lock:
            state.worker_alive = False
            state.worker_alive_by_mode["scanner"] = False


def _start_rf_sentinel_engine(
    btc_device_id: str,
    hop_device_id: str,
    btc_center_mhz: float,
    btc_bandwidth_mhz: int,
    btc_lna_gain_db: int,
    btc_vga_gain_db: int,
    hop_lna_gain_db: int,
    hop_vga_gain_db: int,
    enabled_protocols: set[str] | None = None,
    enabled_devices: set[str] | None = None,
    sweep_both_radios: bool = False,
    fm_device_id: str = "",
) -> dict[str, Any]:
    global rf_sentinel_process, rf_sentinel_thread
    rf_sentinel_stop.clear()
    cmd = [
        _rf_sentinel_scan_bin(),
        "--btc-device-id",
        btc_device_id,
        "--hop-device-id",
        hop_device_id,
        "--btc-center-mhz",
        f"{btc_center_mhz:.3f}",
        "--btc-bandwidth-mhz",
        str(btc_bandwidth_mhz),
        "--btc-lna-gain-db",
        str(btc_lna_gain_db),
        "--btc-vga-gain-db",
        str(btc_vga_gain_db),
        "--ble-lna-gain-db",
        str(hop_lna_gain_db),
        "--ble-vga-gain-db",
        str(hop_vga_gain_db),
    ]
    rf_input_mode = os.getenv("RF_SENTINEL_RF_INPUT_MODE", "live").strip().lower() or "live"
    if rf_input_mode in {"live", "capture", "playback"}:
        cmd.extend(["--rf-input-mode", rf_input_mode])
    iq_capture_path = os.getenv("RF_SENTINEL_IQ_CAPTURE_PATH", "").strip()
    if iq_capture_path:
        cmd.extend(["--iq-capture-path", iq_capture_path])
    iq_playback_path = os.getenv("RF_SENTINEL_IQ_PLAYBACK_PATH", "").strip()
    if iq_playback_path:
        cmd.extend(["--iq-playback-path", iq_playback_path])
    iq_capture_max_bytes = os.getenv("RF_SENTINEL_IQ_CAPTURE_MAX_BYTES", "").strip()
    if iq_capture_max_bytes:
        cmd.extend(["--iq-capture-max-bytes", iq_capture_max_bytes])
    if sweep_both_radios:
        cmd.extend(
            [
                "--sweep-both-radios",
                "--radio-a-device-id",
                btc_device_id,
                "--radio-b-device-id",
                hop_device_id,
                "--radio-a-btc-bandwidth-mhz",
                str(btc_bandwidth_mhz),
                "--radio-b-btc-bandwidth-mhz",
                "20",
            ]
        )
    protocols = enabled_protocols or set(RF_SENTINEL_PROTOCOLS)
    devices = enabled_devices or set()
    ui_config = _read_ui_config()
    bluetooth_classic = _clean_bluetooth_classic_config(ui_config.get("bluetooth_classic"))
    protocol_devices = _clean_protocol_devices(
        ui_config.get("protocol_devices"),
        devices,
        reserved_devices={btc_device_id},
    )
    wifi_channels = _clean_wifi_channels(ui_config.get("wifi_channels"))
    for device_id in sorted(devices):
        cmd.extend(["--allowed-device-id", device_id])
    if "wifi" in protocols:
        wifi_interface = _wifi_interface_from_devices(_available_devices(), enabled_devices)
        if wifi_interface:
            cmd.extend(["--wifi-interface", wifi_interface])
    if "fm" in protocols:
        selected_fm_device = fm_device_id or _fm_device_from_devices(_available_devices(), enabled_devices)
        if selected_fm_device:
            fm_is_bladerf = selected_fm_device.lower().startswith("bladerf:")
            cmd.extend(
                [
                    "--fm-device-id",
                    selected_fm_device,
                    "--fm-discovery-mode",
                    "wideband" if fm_is_bladerf else "sweep",
                    "--fm-sample-rate-sps",
                    "20000000" if fm_is_bladerf else "10000000",
                    "--fm-sweep-bin-width-hz",
                    "100000",
                    "--fm-discovery-dwell-s",
                    "3.0",
                    "--fm-decode-dwell-s",
                    "1.0",
                    "--fm-active-threshold-db",
                    "4.0",
                    "--fm-min-power-dbfs",
                    "-115",
                    "--fm-max-stations",
                    "24",
                    "--fm-lna-gain-db",
                    "32",
                    "--fm-vga-gain-db",
                    "32",
                ]
            )
    if "btc" not in protocols:
        cmd.append("--no-btc")
    if "ble" not in protocols:
        cmd.append("--no-ble")
    if "btc" in protocols and "ble" in protocols:
        # Combined mode (scanner.py's bluetooth_combined_job) captures BLE
        # from the same fixed BTC window instead of running a separate
        # hopper on the second radio - real BLE traffic was getting almost
        # entirely missed as a result: at btc_bandwidth_mhz=60 centered on
        # btc_center_mhz, the window only spans roughly 2412-2472 MHz, which
        # covers just 1 of BLE's 3 primary advertising channels (37=2402
        # and 39=2480 both fall outside it; only 38=2426 is covered).
        # --btc-band-hop is exactly the mechanism built for this - it
        # retunes the shared capture between two overlapping windows
        # (_band_hop_centers_mhz) that together span the full 2402-2482 MHz
        # ISM range - it just wasn't ever being turned on here.
        cmd.extend(["--btc-band-hop", "--btc-band-hop-dwell-s", "10"])
    if "zigbee" not in protocols:
        cmd.append("--no-zigbee")
    if "tpms" not in protocols:
        cmd.append("--no-tpms")
    if "walkie" not in protocols:
        cmd.append("--no-walkie")
    if "wifi" not in protocols:
        cmd.append("--no-wifi")
    if "fm" not in protocols:
        cmd.append("--no-fm")
    if "lfmf" not in protocols:
        cmd.append("--no-lfmf")
    if "cellular" not in protocols:
        cmd.append("--no-cellular")
    page_detection_enabled = str(os.getenv("RF_SENTINEL_ENABLE_PAGE_DETECTION", "")).strip().lower() in {"1", "true", "yes", "on"}
    if bluetooth_classic.get("periodic_page_scan"):
        page_detection_enabled = True
        cmd.append("--btc-periodic-page-scan")
    if bluetooth_classic.get("log_passive_fhs_bdaddr"):
        cmd.append("--btc-log-passive-fhs-bdaddr")
    if bluetooth_classic.get("band_hop"):
        cmd.append("--btc-band-hop")
    if not page_detection_enabled:
        cmd.append("--no-page-detection")
    # Start in discovery mode; only the explicit right-click Follow action locks Zigbee.
    zigbee_follow_channel = None
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=devices,
        protocol_devices=protocol_devices,
        wifi_channels=wifi_channels,
        bluetooth_classic=bluetooth_classic,
        zigbee_follow_channel=zigbee_follow_channel,
    )
    cmd.extend(["--control-file", str(RF_SENTINEL_CONTROL_PATH)])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    rf_sentinel_process = proc
    rf_sentinel_thread = threading.Thread(target=_rf_sentinel_loop, args=(proc,), daemon=True)
    rf_sentinel_thread.start()
    return {"engine": "rf_sentinel_scan", "command": cmd, "pid": proc.pid, "follow": control.get("follow", {})}


def _stop_rf_sentinel_engine(timeout_s: float = 4.0) -> None:
    global rf_sentinel_process, rf_sentinel_thread
    rf_sentinel_stop.set()
    proc = rf_sentinel_process
    rf_sentinel_process = None
    with state_lock:
        if proc is not None:
            _append_scanner_log("[ui] stopping rf_sentinel_scan")
    if proc is not None and proc.poll() is None:
        _terminate_process_group(proc, timeout_s=timeout_s)
    thread = rf_sentinel_thread
    rf_sentinel_thread = None
    if thread and thread.is_alive():
        thread.join(timeout=2.0)


@app.post("/api/scan/protocols")
def update_scan_protocols():
    payload = request.get_json(silent=True) or {}
    requested_protocols = payload.get("protocols")
    if not isinstance(requested_protocols, list):
        return _json_error(400, "update_scan_protocols", error="protocols must be a list")
    enabled_protocols = {str(item).strip().lower() for item in requested_protocols}
    enabled_protocols &= RF_SENTINEL_PROTOCOLS
    requested_devices = payload.get("devices")
    enabled_devices = None
    if isinstance(requested_devices, list):
        enabled_devices = {str(item).strip() for item in requested_devices if str(item).strip()}
    disabled_devices = set()
    devices_available = _available_devices()
    known_devices = {str(item.get("id") or "").strip() for item in devices_available if str(item.get("id") or "").strip()}
    if enabled_devices is not None:
        disabled_devices = known_devices - enabled_devices
    else:
        disabled_devices = set(_read_ui_config().get("disabled_devices", []))
        enabled_devices = known_devices - disabled_devices
    if "wifi" in enabled_protocols and not _has_wifi_device(devices_available, enabled_devices):
        enabled_protocols.discard("wifi")
    if "lfmf" in enabled_protocols and not _has_lfmf_device(devices_available, enabled_devices):
        enabled_protocols.discard("lfmf")
    existing_config = _read_ui_config()
    bluetooth_classic = _clean_bluetooth_classic_config(payload.get("bluetooth_classic", existing_config.get("bluetooth_classic")))
    protocol_devices = _clean_protocol_devices(
        payload.get("protocol_devices", existing_config.get("protocol_devices")),
        enabled_devices or known_devices,
        reserved_devices={str(state.device_ids.get("radio_a") or state.device_ids.get("classic") or "").strip()},
    )
    wifi_channels = _clean_wifi_channels(payload.get("wifi_channels", existing_config.get("wifi_channels")))
    _write_ui_config(
        enabled_protocols,
        disabled_devices,
        wifi_channels=wifi_channels,
        protocol_devices=protocol_devices,
        bluetooth_classic=bluetooth_classic,
    )
    control = _write_rf_sentinel_control(
        enabled_protocols,
        enabled_devices=enabled_devices,
        protocol_devices=protocol_devices,
        wifi_channels=wifi_channels,
        bluetooth_classic=bluetooth_classic,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE if "zigbee" in enabled_protocols else None,
    )
    follow_state = _follow_state_for_protocols(control, enabled_protocols)
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(enabled_protocols)
        state.decoder_stats["follow"] = follow_state
        _append_scanner_log(f"[ui] enabled protocols updated: {', '.join(sorted(enabled_protocols)) or 'none'}")
    return jsonify({"ok": True, "protocols": sorted(enabled_protocols), "wifi_channels": wifi_channels, "protocol_devices": protocol_devices, "bluetooth_classic": bluetooth_classic})


@app.post("/api/scan/follow")
def update_scan_follow():
    payload = request.get_json(silent=True) or {}
    protocol = str(payload.get("protocol") or "").strip().lower()
    if protocol != "zigbee":
        return _json_error(400, "update_scan_follow", error="only zigbee follow is supported right now")
    follow = bool(payload.get("follow", True))
    channel_value = payload.get("channel")
    channel: int | None
    if follow:
        try:
            channel = int(channel_value)
        except (TypeError, ValueError):
            return _json_error(400, "update_scan_follow", error="zigbee follow requires a numeric channel")
        if channel < 11 or channel > 26:
            return _json_error(400, "update_scan_follow", error="zigbee channel must be 11-26")
    else:
        channel = None
    follow_device_id = _preferred_lock_device(prefer_free=False) if channel is not None else ""
    if follow_device_id:
        with contextlib.suppress(Exception):
            _force_release_gateway_device(follow_device_id)
    control = _write_rf_sentinel_control(
        zigbee_follow_channel=channel,
        zigbee_follow_device_id=follow_device_id if channel is not None else "",
    )
    follow_state = control.get("follow") if isinstance(control.get("follow"), dict) else {}
    with state_lock:
        state.decoder_stats["follow"] = follow_state
        if channel is None:
            _append_scanner_log("[ui] zigbee follow cleared")
        else:
            suffix = f" on {follow_device_id}" if follow_device_id else ""
            _append_scanner_log(f"[ui] zigbee follow locked channel {channel}{suffix}")
    return jsonify({"ok": True, "follow": follow_state})


@app.post("/api/fm/play")
def fm_play():
    global fm_pending_thread, fm_request_serial
    payload = request.get_json(silent=True) or {}
    freq_mhz = float(payload.get("freq_mhz", 0.0) or 0.0)
    device_id = str(payload.get("device_id") or "").strip() or _preferred_lock_device(prefer_free=False)
    if not 87.5 <= freq_mhz <= 108.0:
        return _json_error(400, "fm_play", error="freq_mhz must be between 87.5 and 108.0")
    try:
        fm_request_serial += 1
        request_serial = fm_request_serial
        fm_playback.pending = True
        fm_playback.pending_freq_mhz = float(freq_mhz)
        fm_playback.pending_device_id = device_id
        fm_playback.worker_error = "FM queued; waiting for SDR availability"
        if device_id and not _device_available(device_id):
            _pause_fm_scanner_for_playback()
            _force_release_gateway_device(device_id)
            _start_fm_pending_thread(request_serial, float(freq_mhz), device_id)
        else:
            try:
                _start_fm_playback_now(freq_mhz, device_id)
            except Exception as exc:
                if not _fm_busy_error(exc):
                    fm_playback.pending = False
                    _restore_fm_scanner_after_playback()
                    return _json_error(409, "fm_play", error=str(exc))
                _start_fm_pending_thread(request_serial, float(freq_mhz), device_id)
    except requests.RequestException as exc:
        return _json_error(503, "fm_play", error="sdr-gateway is unavailable", detail=str(exc))
    return jsonify({"ok": True, "fm_playback": _fm_playback_status_payload()})


@app.post("/api/fm/stop")
def fm_stop():
    _stop_fm_playback()
    return jsonify({"ok": True, "fm_playback": _fm_playback_status_payload()})


@app.get("/api/fm/audio/batch")
def fm_audio_batch():
    if not fm_playback.running:
        return Response(b"", mimetype="application/octet-stream", status=204)
    count = max(1, min(int(request.args.get("count", 6)), 16))
    timeout = max(0.05, min(float(request.args.get("timeout", 0.4)), 2.0))
    chunks: list[bytes] = []
    for idx in range(count):
        try:
            pcm = fm_audio_q.get(timeout=timeout if idx == 0 else 0.02)
        except queue.Empty:
            break
        chunks.append(pcm)
        fm_playback.served_chunks += 1
    if not chunks:
        fm_playback.empty_audio_polls += 1
        return Response(b"", mimetype="application/octet-stream", status=204)
    fm_playback.empty_audio_polls = 0
    return Response(b"".join(chunks), mimetype="application/octet-stream")


@app.post("/api/subghz/walkie/play")
def walkie_play():
    global walkie_pending_thread, walkie_request_serial
    payload = request.get_json(silent=True) or {}
    freq_mhz = float(payload.get("freq_mhz", 462.5) or 462.5)
    device_id = str(payload.get("device_id") or "").strip() or _current_walkie_scanner_device_id()
    if not 300.0 <= freq_mhz <= 500.0:
        return _json_error(400, "walkie_play", error="freq_mhz must be between 300.0 and 500.0")
    try:
        walkie_request_serial += 1
        request_serial = walkie_request_serial
        walkie_playback.pending = True
        walkie_playback.pending_freq_mhz = float(freq_mhz)
        walkie_playback.pending_device_id = device_id
        walkie_playback.worker_error = "Walkie audio queued; waiting for SDR availability"
        if device_id and not _device_available(device_id):
            _pause_walkie_scanner_for_playback()
            _force_release_gateway_device(device_id)
            _start_walkie_pending_thread(request_serial, float(freq_mhz), device_id)
        else:
            try:
                _start_walkie_playback_now(freq_mhz, device_id)
            except Exception as exc:
                if not _fm_busy_error(exc):
                    walkie_playback.pending = False
                    _restore_walkie_scanner_after_playback()
                    return _json_error(409, "walkie_play", error=str(exc))
                _start_walkie_pending_thread(request_serial, float(freq_mhz), device_id)
    except requests.RequestException as exc:
        return _json_error(503, "walkie_play", error="sdr-gateway is unavailable", detail=str(exc))
    return jsonify({"ok": True, "walkie_playback": _walkie_playback_status_payload()})


@app.post("/api/subghz/walkie/stop")
def walkie_stop():
    _stop_walkie_playback()
    return jsonify({"ok": True, "walkie_playback": _walkie_playback_status_payload()})


@app.get("/api/subghz/walkie/audio/batch")
def walkie_audio_batch():
    if not walkie_playback.running:
        return Response(b"", mimetype="application/octet-stream", status=204)
    count = max(1, min(int(request.args.get("count", 6)), 16))
    timeout = max(0.05, min(float(request.args.get("timeout", 0.4)), 2.0))
    chunks: list[bytes] = []
    for idx in range(count):
        try:
            pcm = walkie_audio_q.get(timeout=timeout if idx == 0 else 0.02)
        except queue.Empty:
            break
        chunks.append(pcm)
        walkie_playback.served_chunks += 1
    if not chunks:
        return Response(b"", mimetype="application/octet-stream", status=204)
    return Response(b"".join(chunks), mimetype="application/octet-stream")


@app.get("/api/subghz/walkie/audio/recent")
def walkie_audio_recent():
    with walkie_recent_lock:
        chunks = list(walkie_recent_audio)
    if not chunks:
        return Response(b"", mimetype="application/octet-stream", status=204)
    return Response(b"".join(chunks), mimetype="application/octet-stream")


def _reset_stats() -> None:
    state.chunks_seen = 0
    state.bytes_seen = 0
    state.last_rssi_dbfs = -120.0
    state.rssi_by_mode = {}
    state.chunks_by_mode = {}
    state.bytes_by_mode = {}
    state.noise_floor_dbfs = -120.0
    state.bursts_seen = 0
    state.ble_packets_seen = 0
    state.classic_bursts_seen = 0
    state.detections = []
    state.classic_candidates = []
    state.classic_addresses = []
    state.discovery_table = []
    state.channel_activity = {}
    state.page_activity = {}
    state.decoder_stats = {}
    state.scanner_log = []


def _reset_live_stats_keep_discoveries() -> None:
    state.chunks_seen = 0
    state.bytes_seen = 0
    state.last_rssi_dbfs = -120.0
    state.rssi_by_mode = {}
    state.chunks_by_mode = {}
    state.bytes_by_mode = {}
    state.noise_floor_dbfs = -120.0


def _pick_device(devices: list[dict[str, Any]], preferred: str, fallback: str = "") -> str:
    preferred_l = preferred.lower()
    fallback_l = fallback.lower()
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        haystack = f"{dev_id} {label}".lower()
        if preferred_l and preferred_l in haystack:
            return dev_id
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        haystack = f"{dev_id} {label}".lower()
        if fallback_l and fallback_l in haystack:
            return dev_id
    return str(devices[0].get("id", "")) if devices else ""


def _device_matches(devices: list[dict[str, Any]], device_id: str, pattern: str) -> bool:
    pattern_l = pattern.lower()
    for dev in devices:
        dev_id = str(dev.get("id", ""))
        label = str(dev.get("label", ""))
        if dev_id != device_id:
            continue
        return pattern_l in f"{dev_id} {label}".lower()
    return pattern_l in device_id.lower()


def _fetch_gateway_devices() -> list[dict[str, Any]]:
    global devices_cache, devices_cache_updated_at
    resp = requests.get(
        f"{_gateway_base()}/devices",
        headers=_gateway_headers(),
        timeout=SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        resp.raise_for_status()
    body = resp.json()
    devices = body if isinstance(body, list) else []
    devices = [dict(item) for item in devices if isinstance(item, dict)]
    devices.extend(_fetch_gateway_wifi_devices())
    with devices_cache_lock:
        devices_cache = devices
        devices_cache_updated_at = time.time()
    return devices


def _fetch_gateway_wifi_devices() -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            f"{_gateway_base()}/wifi/interfaces",
            headers=_gateway_headers(),
            timeout=min(SDR_GATEWAY_DEVICES_TIMEOUT_SECONDS, 5.0),
        )
        if resp.status_code >= 400:
            return []
        body = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(body, list):
        return []
    devices: list[dict[str, Any]] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        frequency_mhz = item.get("frequency_mhz")
        channel = item.get("channel")
        details = []
        if channel:
            details.append(f"channel {channel}")
        if frequency_mhz:
            details.append(f"{frequency_mhz} MHz")
        if item.get("type"):
            details.append(str(item.get("type")))
        devices.append(
            {
                "id": name,
                "driver": "wifi",
                "label": f"WiFi interface {name}",
                "serial": item.get("mac"),
                "freq_min_hz": 2_400_000_000,
                "freq_max_hz": 5_900_000_000,
                "max_sample_rate_sps": 0,
                "notes": "802.11 monitor/capture source from sdr-gateway"
                + (f" ({', '.join(details)})" if details else ""),
                "occupied": False,
                "occupied_by": None,
                "occupied_id": None,
                "up": bool(item.get("up")),
                "channel": channel,
                "frequency_mhz": frequency_mhz,
            }
        )
    return devices


def _demo_available_devices() -> list[dict[str, Any]]:
    return [
        {
            "id": "demo-bladerf:0",
            "driver": "demo-bladerf",
            "label": "Demo wideband SDR",
            "serial": "public-demo-a",
            "freq_min_hz": 70_000_000,
            "freq_max_hz": 6_000_000_000,
            "max_sample_rate_sps": 60_000_000,
            "notes": "Synthetic BTC/BLE 2.4 GHz ISM replay source",
            "occupied": True,
            "occupied_by": "rf-sentinel-demo",
        },
        {
            "id": "demo-hackrf:0",
            "driver": "demo-hackrf",
            "label": "Demo agile SDR",
            "serial": "public-demo-b",
            "freq_min_hz": 1_000_000,
            "freq_max_hz": 6_000_000_000,
            "max_sample_rate_sps": 20_000_000,
            "notes": "Synthetic Zigbee, sub-GHz, and cellular sweep source",
            "occupied": True,
            "occupied_by": "rf-sentinel-demo",
        },
        {
            "id": "wlan-demo0",
            "driver": "wifi",
            "label": "Demo WiFi monitor interface",
            "serial": "02:10:7a:ff:00:01",
            "freq_min_hz": 2_400_000_000,
            "freq_max_hz": 5_900_000_000,
            "max_sample_rate_sps": 0,
            "notes": "Synthetic 802.11 AP/station frame source",
            "occupied": True,
            "occupied_by": "rf-sentinel-demo",
            "up": True,
            "channel": 6,
            "frequency_mhz": 2437,
        },
        {
            "id": "demo-rtlsdr:0",
            "driver": "demo-rtlsdr",
            "label": "Demo FM receiver",
            "serial": "public-demo-fm",
            "freq_min_hz": 24_000_000,
            "freq_max_hz": 1_700_000_000,
            "max_sample_rate_sps": 2_400_000,
            "notes": "Synthetic broadcast FM discovery source",
            "occupied": True,
            "occupied_by": "rf-sentinel-demo",
        },
        {
            "id": "demo-sdrplay:0",
            "driver": "demo-sdrplay-rsp2",
            "label": "Demo low-frequency receiver",
            "serial": "public-demo-lf",
            "freq_min_hz": 1_000,
            "freq_max_hz": 2_000_000_000,
            "max_sample_rate_sps": 10_000_000,
            "notes": "Synthetic VLF/LF/MF awareness source",
            "occupied": True,
            "occupied_by": "rf-sentinel-demo",
        },
    ]


def _cached_gateway_devices() -> tuple[list[dict[str, Any]], float]:
    with devices_cache_lock:
        return [dict(item) for item in devices_cache], float(devices_cache_updated_at)


def _available_devices() -> list[dict[str, Any]]:
    if RF_SENTINEL_DEMO_MODE:
        return _demo_available_devices()
    try:
        return _fetch_gateway_devices()
    except requests.RequestException:
        cached, _ = _cached_gateway_devices()
        return cached


def _stop_scan(stop_gateway: bool = True) -> None:
    global worker_thread, worker_threads, worker_stops
    _stop_fm_playback()
    _stop_walkie_playback()
    _stop_bredr_inquiry()
    _stop_rf_sentinel_engine()
    _stop_btcsniffer_engine()
    worker_stop.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=2.0)
    worker_thread = None
    for stop in worker_stops.values():
        stop.set()
    for thread in worker_threads.values():
        if thread.is_alive():
            thread.join(timeout=2.0)
    worker_threads = {}
    worker_stops = {}
    stream_ids = list(state.stream_ids.values())
    if state.stream_id:
        stream_ids.append(state.stream_id)
    if stop_gateway:
        for stream_id in set(stream_ids):
            _stop_gateway_stream(stream_id)
    with state_lock:
        state.running = False
        state.stream_id = None
        state.stream_ids = {}
        state.worker_alive = False
        state.worker_alive_by_mode = {}
        state.worker_error = ""
        state.btc_engine = ""
        state.btc_engine_command = []
        state.btc_engine_log = ""
        state.worker_errors = {}
        state.gateway_start_response = None
        state.decoder_stats["follow"] = {}
        state.scanner_assignments = {}
        state.test_target = None
        state.test_target_error = ""


def _demo_assignments(now: float | None = None) -> dict[str, dict[str, Any]]:
    ts = float(now or time.time())
    return {
        "demo-bladerf:0": {
            "device_id": "demo-bladerf:0",
            "job_name": "demo-btc-ble",
            "protocol": "BTC",
            "band": "BTC+BTLE shared 2.4 GHz ISM public demo",
            "command": "synthetic-event-replay --protocols btc,ble --center-mhz 2442 --bandwidth-mhz 60",
            "dwell_s": 0.0,
            "seen_at": ts,
            "mode": "demo",
            "last_center_freq_hz": 2_442_000_000,
        },
        "demo-hackrf:0": {
            "device_id": "demo-hackrf:0",
            "job_name": "demo-agile-sweep",
            "protocol": "ZIGBEE",
            "band": "Zigbee, sub-GHz, walkie, and cellular public demo sweep",
            "command": "synthetic-event-replay --protocols zigbee,tpms,walkie,cellular",
            "dwell_s": 1.5,
            "seen_at": ts,
            "mode": "demo",
            "last_center_freq_hz": 2_450_000_000,
        },
        "wlan-demo0": {
            "device_id": "wlan-demo0",
            "job_name": "demo-wifi-monitor",
            "protocol": "wifi",
            "band": "WiFi monitor channels 1, 6, and 11",
            "command": "synthetic-event-replay --protocols wifi --channels 1,6,11",
            "dwell_s": 0.0,
            "seen_at": ts,
            "mode": "demo",
            "last_center_freq_hz": 2_437_000_000,
        },
        "demo-rtlsdr:0": {
            "device_id": "demo-rtlsdr:0",
            "job_name": "demo-fm",
            "protocol": "fm",
            "band": "Broadcast FM public demo sweep",
            "command": "synthetic-event-replay --protocols fm --range 87.5-108.0MHz",
            "dwell_s": 2.0,
            "seen_at": ts,
            "mode": "demo",
            "last_center_freq_hz": 101_700_000,
        },
        "demo-sdrplay:0": {
            "device_id": "demo-sdrplay:0",
            "job_name": "demo-lfmf",
            "protocol": "lfmf",
            "band": "VLF/LF/MF public demo awareness",
            "command": "synthetic-event-replay --protocols lfmf --range 1kHz-3MHz",
            "dwell_s": 2.0,
            "seen_at": ts,
            "mode": "demo",
            "last_center_freq_hz": 530_000,
        },
    }


def _demo_prime_state(protocols: set[str] | None = None, preserve_detections: bool = True) -> None:
    enabled = sorted((protocols or set(RF_SENTINEL_PROTOCOLS)) & RF_SENTINEL_PROTOCOLS)
    now = time.time()
    if not preserve_detections:
        _reset_stats()
    state.running = not demo_replay_paused.is_set()
    state.mode = "sentinel"
    state.stream_id = None
    state.stream_ids = {"demo": "synthetic-event-replay"}
    state.device_id = "demo-bladerf:0"
    state.device_ids = {
        "classic": "demo-bladerf:0",
        "btle": "demo-bladerf:0",
        "hop": "demo-hackrf:0",
        "radio_a": "demo-bladerf:0",
        "radio_b": "demo-hackrf:0",
    }
    state.center_freq_hz = 2_442_000_000
    state.sample_rate_sps = 60_000_000
    state.lna_gain_db = 32
    state.vga_gain_db = 32
    state.channel = 40
    state.channels_by_mode = {"classic": 12, "ble": 37}
    state.worker_alive = not demo_replay_paused.is_set()
    state.worker_alive_by_mode = {"demo": not demo_replay_paused.is_set()}
    state.worker_error = ""
    state.worker_errors = {}
    state.gateway_start_response = {
        "demo": {
            "engine": "synthetic-event-replay",
            "event_file": str(RF_SENTINEL_DEMO_EVENT_FILE),
            "public_safe": True,
        }
    }
    state.btc_engine = "synthetic-event-replay"
    state.btc_engine_command = ["synthetic-event-replay", "--event-file", str(RF_SENTINEL_DEMO_EVENT_FILE)]
    state.btc_engine_log = ""
    state.decoder_stats["enabled_protocols"] = enabled
    state.decoder_stats["sweep_both_radios"] = True
    state.decoder_stats["follow"] = {}
    state.decoder_stats["demo_mode"] = True
    state.scanner_assignments = _demo_assignments(now)
    if not any("public demo" in line.lower() for line in state.scanner_log[-10:]):
        _append_scanner_log(f"[demo] public demo replay active from {RF_SENTINEL_DEMO_EVENT_FILE}")


def _demo_start_response(protocols: set[str], preserve_detections: bool = True):
    demo_replay_paused.clear()
    with state_lock:
        _demo_prime_state(protocols or set(RF_SENTINEL_PROTOCOLS), preserve_detections=preserve_detections)
    return jsonify(
        {
            "ok": True,
            "mode": "sentinel",
            "demo": True,
            "event_file": str(RF_SENTINEL_DEMO_EVENT_FILE),
            "devices": state.device_ids,
        }
    )


def _demo_pause_response():
    demo_replay_paused.set()
    with state_lock:
        state.running = False
        state.worker_alive = False
        state.worker_alive_by_mode = {"demo": False}
        _append_scanner_log("[demo] public demo replay paused")
    return jsonify({"ok": True, "demo": True, "running": False})


def _demo_clear_response():
    with state_lock:
        _reset_stats()
        _demo_prime_state(set(RF_SENTINEL_PROTOCOLS), preserve_detections=True)
        _append_scanner_log("[demo] cleared public demo detections")
    return jsonify({"ok": True, "demo": True})


def _ensure_demo_event_file() -> None:
    if RF_SENTINEL_DEMO_EVENT_FILE.exists() and RF_SENTINEL_DEMO_EVENT_FILE.stat().st_size > 0:
        return
    scripts_dir = PROJECT_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from generate_demo_events import write_events
    except Exception as exc:
        raise RuntimeError(f"demo event generator is unavailable: {exc}") from exc
    write_events(RF_SENTINEL_DEMO_EVENT_FILE)


def _load_demo_payloads() -> list[dict[str, Any]]:
    _ensure_demo_event_file()
    payloads: list[dict[str, Any]] = []
    with RF_SENTINEL_DEMO_EVENT_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if isinstance(payload, dict):
                payloads.append(payload)
    payloads.sort(key=lambda item: float(item.get("offset_s") or 0.0))
    return payloads


def _demo_source_for_payload(payload: dict[str, Any]) -> str:
    protocol = str(payload.get("protocol") or "").strip().lower()
    if protocol == "ieee802154":
        protocol = "zigbee"
    return f"demo:{protocol or 'rf'}"


def _demo_mode_key(payload: dict[str, Any]) -> str:
    protocol = str(payload.get("protocol") or "").strip().lower()
    if protocol in {"btc", "bluetooth_classic", "classic"}:
        return "classic"
    if protocol in {"ble", "btle"}:
        return "ble"
    if protocol == "ieee802154":
        return "zigbee"
    return protocol or "rf"


def _demo_assignment_device(protocol: str) -> str:
    if protocol in {"btc", "classic", "ble", "btle"}:
        return "demo-bladerf:0"
    if protocol == "wifi":
        return "wlan-demo0"
    if protocol == "fm":
        return "demo-rtlsdr:0"
    if protocol == "lfmf":
        return "demo-sdrplay:0"
    return "demo-hackrf:0"


def _demo_seen_dates(payload: dict[str, Any], now: float) -> list[str]:
    dates: list[str] = []
    for offset in payload.get("demo_seen_day_offsets") or []:
        try:
            days = max(0, int(offset))
        except (TypeError, ValueError):
            continue
        dates.append(_utc_date_key(now - days * 86400))
    return list(dict.fromkeys(dates))


def _demo_touch_assignment(payload: dict[str, Any]) -> None:
    protocol = _demo_mode_key(payload)
    device_id = _demo_assignment_device(protocol)
    assignment = state.scanner_assignments.get(device_id)
    if not assignment:
        return
    assignment["seen_at"] = time.time()
    assignment["last_protocol"] = protocol
    if payload.get("center_freq_hz") is not None:
        assignment["last_center_freq_hz"] = payload.get("center_freq_hz")
    if payload.get("frequency_hz") is not None:
        assignment["last_frequency_hz"] = payload.get("frequency_hz")


def _demo_replay_loop() -> None:
    try:
        payloads = _load_demo_payloads()
    except Exception as exc:
        with state_lock:
            state.worker_error = str(exc)
            _append_scanner_log(f"[demo] unable to load public demo events: {exc}")
        return
    if not payloads:
        with state_lock:
            state.worker_error = "demo event file is empty"
            _append_scanner_log("[demo] public demo event file is empty")
        return
    with state_lock:
        _demo_prime_state(set(RF_SENTINEL_PROTOCOLS), preserve_detections=False)
    while not shutdown_complete:
        cycle_start = time.monotonic()
        for payload_template in payloads:
            while demo_replay_paused.is_set() and not shutdown_complete:
                time.sleep(0.2)
            if shutdown_complete:
                break
            offset = max(0.0, float(payload_template.get("offset_s") or 0.0)) / RF_SENTINEL_DEMO_TIME_SCALE
            deadline = cycle_start + offset
            while time.monotonic() < deadline and not demo_replay_paused.is_set() and not shutdown_complete:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            if demo_replay_paused.is_set() or shutdown_complete:
                continue
            payload = copy.deepcopy(payload_template)
            now = time.time()
            payload["timestamp"] = now
            payload["seen_at"] = now
            seen_dates = _demo_seen_dates(payload, now)
            if seen_dates:
                payload["seen_dates"] = seen_dates
            source = _demo_source_for_payload(payload)
            events = _scanner_json_to_events(source, payload)
            if seen_dates:
                for event in events:
                    event["seen_dates"] = seen_dates
            with state_lock:
                _demo_prime_state(set(state.decoder_stats.get("enabled_protocols", RF_SENTINEL_PROTOCOLS)), preserve_detections=True)
                _demo_touch_assignment(payload)
                mode_key = _demo_mode_key(payload)
                state.chunks_seen += 1
                state.bytes_seen += 4096
                state.chunks_by_mode[mode_key] = int(state.chunks_by_mode.get(mode_key, 0)) + 1
                state.bytes_by_mode[mode_key] = int(state.bytes_by_mode.get(mode_key, 0)) + 4096
                rssi = _real_rssi(payload.get("rssi_dbfs", payload.get("rssi_dbm", payload.get("power_dbfs"))))
                if rssi is not None:
                    state.last_rssi_dbfs = round(rssi, 1)
                    state.noise_floor_dbfs = round((state.noise_floor_dbfs * 0.94) + ((rssi - 32.0) * 0.06), 1)
            if events:
                _append_detections(events, [])
        if not RF_SENTINEL_DEMO_LOOP:
            break
        time.sleep(max(0.25, 1.5 / RF_SENTINEL_DEMO_TIME_SCALE))


def _shutdown_gateway_device_ids() -> set[str]:
    device_ids = {str(item or "").strip() for item in state.device_ids.values()}
    for assignment in (state.scanner_assignments or {}).values():
        if isinstance(assignment, dict):
            device_ids.add(str(assignment.get("device_id") or "").strip())
    if fm_playback.device_id:
        device_ids.add(str(fm_playback.device_id).strip())
    if walkie_playback.device_id:
        device_ids.add(str(walkie_playback.device_id).strip())
    return {device_id for device_id in device_ids if device_id}


def shutdown() -> None:
    global shutdown_complete
    with shutdown_lock:
        if shutdown_complete:
            return
        shutdown_complete = True
    stop_console_dashboard()
    device_ids = _shutdown_gateway_device_ids()
    try:
        _append_scanner_log("[ui] shutting down; releasing gateway sessions")
        _stop_scan(stop_gateway=True)
        for device_id in device_ids:
            _force_release_gateway_device(device_id)
    except Exception as exc:
        app.logger.warning("UI shutdown cleanup failed: %s", exc)


def _channel_freq(mode: str, channel: int) -> int:
    if mode in {"classic", "both"}:
        start_hz = BT_CLASSIC_CHANNELS.get(channel, BT_CLASSIC_CHANNELS[0])
        return int(start_hz + ((BT_CLASSIC_BANK_SIZE - 1) * BT_CLASSIC_LANE_SPACING_HZ / 2.0))
    return BLE_ADV_CHANNELS.get(channel, BLE_ADV_CHANNELS[37])


def _btc_bank_start_from_center(center_freq_hz: int, bandwidth_mhz: int = BT_CLASSIC_BANK_SIZE) -> int:
    center_mhz = float(center_freq_hz) / 1_000_000.0
    start = int(round(center_mhz - 2402.0 - ((float(bandwidth_mhz) - 1.0) / 2.0)))
    return max(0, min(78 - (bandwidth_mhz - 1), start))


def _start_gateway_stream(
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    baseband_filter_hz: int | None = None,
) -> tuple[dict[str, Any], int, int, int]:
    resp = requests.post(
        f"{_gateway_base()}/streams/start",
        headers=_gateway_headers(),
        json={
            "device_id": device_id,
            "center_freq_hz": center_freq_hz,
            "sample_rate_sps": sample_rate_sps,
            "lna_gain_db": lna_gain_db,
            "vga_gain_db": vga_gain_db,
            "amp_enable": False,
            "baseband_filter_hz": int(baseband_filter_hz or sample_rate_sps),
            "duration_seconds": None,
            "num_samples": None,
        },
        timeout=12,
    )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text)
    body = resp.json()
    accepted_config = body.get("config", {}) or {}
    actual_rate = int(accepted_config.get("sample_rate_sps", sample_rate_sps))
    actual_lna = int(accepted_config.get("lna_gain_db", lna_gain_db))
    actual_vga = int(accepted_config.get("vga_gain_db", vga_gain_db))
    return body, actual_rate, actual_lna, actual_vga


def _retune_gateway_stream(
    stream_id: str,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    baseband_filter_hz: int | None = None,
) -> tuple[dict[str, Any], int, int, int]:
    if not stream_id:
        raise RuntimeError("No gateway stream is available to retune")
    resp = requests.post(
        f"{_gateway_base()}/streams/{stream_id}/retune",
        headers=_gateway_headers(),
        json={
            "device_id": device_id,
            "center_freq_hz": center_freq_hz,
            "sample_rate_sps": sample_rate_sps,
            "lna_gain_db": lna_gain_db,
            "vga_gain_db": vga_gain_db,
            "amp_enable": False,
            "baseband_filter_hz": int(baseband_filter_hz or sample_rate_sps),
            "duration_seconds": None,
            "num_samples": None,
        },
        timeout=12,
    )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text)
    body = resp.json()
    accepted_config = body.get("config", {}) or {}
    actual_rate = int(accepted_config.get("sample_rate_sps", sample_rate_sps))
    actual_lna = int(accepted_config.get("lna_gain_db", lna_gain_db))
    actual_vga = int(accepted_config.get("vga_gain_db", vga_gain_db))
    return body, actual_rate, actual_lna, actual_vga


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/resources/<path:filename>")
def resources(filename: str):
    return send_from_directory(PROJECT_ROOT / "ui" / "resources", filename)


@app.get("/api/devices")
def devices():
    if RF_SENTINEL_DEMO_MODE:
        return jsonify(_demo_available_devices())
    try:
        return jsonify(_fetch_gateway_devices())
    except requests.RequestException as exc:
        cached, updated_at = _cached_gateway_devices()
        if cached:
            response = jsonify(cached)
            response.headers["X-RF-Sentinel-Warning"] = "using cached SDR device list; sdr-gateway /devices request failed"
            response.headers["X-RF-Sentinel-Cache-Age"] = f"{max(0.0, time.time() - updated_at):.1f}"
            response.headers["X-RF-Sentinel-Gateway-Error"] = str(exc)[:300]
            return response
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503


@app.get("/api/config")
def get_config():
    return jsonify(_read_ui_config())


@app.post("/api/config")
def update_config():
    payload = request.get_json(silent=True) or {}
    requested_protocols = payload.get("protocols")
    if not isinstance(requested_protocols, list):
        return _json_error(400, "update_config", error="protocols must be a list")
    protocols = {str(item).strip().lower() for item in requested_protocols} & RF_SENTINEL_PROTOCOLS
    requested_disabled = payload.get("disabled_devices")
    if not isinstance(requested_disabled, list):
        requested_disabled = []
    disabled_devices = {str(item).strip() for item in requested_disabled if str(item).strip()}
    devices_available = _available_devices()
    enabled_devices = _enabled_devices_from_disabled(devices_available, disabled_devices)
    protocol_devices = _clean_protocol_devices(
        payload.get("protocol_devices"),
        enabled_devices,
        reserved_devices={str(state.device_ids.get("radio_a") or state.device_ids.get("classic") or "").strip()},
    )
    wifi_channels = _clean_wifi_channels(payload.get("wifi_channels"))
    bluetooth_classic = _clean_bluetooth_classic_config(payload.get("bluetooth_classic"))
    if "wifi" in protocols and not _has_wifi_device(devices_available, enabled_devices):
        protocols.discard("wifi")
    if "lfmf" in protocols and not _has_lfmf_device(devices_available, enabled_devices):
        protocols.discard("lfmf")
    config = _write_ui_config(
        protocols,
        disabled_devices,
        wifi_channels=wifi_channels,
        protocol_devices=protocol_devices,
        bluetooth_classic=bluetooth_classic,
    )
    control = _write_rf_sentinel_control(
        protocols,
        enabled_devices=enabled_devices,
        protocol_devices=protocol_devices,
        wifi_channels=wifi_channels,
        bluetooth_classic=bluetooth_classic,
        zigbee_follow_channel=RF_SENTINEL_NO_CHANGE if "zigbee" in protocols else None,
    )
    follow_state = _follow_state_for_protocols(control, protocols)
    with state_lock:
        state.decoder_stats["enabled_protocols"] = sorted(protocols)
        state.decoder_stats["follow"] = follow_state
        _append_scanner_log(f"[ui] config saved: {', '.join(sorted(protocols)) or 'none'}")
    return jsonify({"ok": True, **config})


@app.errorhandler(BadRequest)
def handle_bad_request(exc: BadRequest):
    payload = {"error": "bad request", "detail": str(exc)}
    _log_http_error(400, request.endpoint or "unknown", payload, exc)
    return jsonify(payload), 400


@app.post("/api/scan/start")
def start_scan():
    global worker_thread, worker_threads, worker_stops
    try:
        payload = request.get_json(force=True) or {}
    except BadRequest as exc:
        return _json_error(400, "start_scan", error="invalid JSON payload", detail=str(exc))
    device_id = _clean_device_id(payload.get("device_id", ""))
    btc_device_id = _clean_device_id(payload.get("btc_device_id", ""))
    raw_btle_device_id = payload.get("btle_device_id", "") or payload.get("hop_device_id", "")
    btle_device_id = _clean_device_id(raw_btle_device_id)
    explicit_btle_device = bool(_clean_device_id(raw_btle_device_id))
    mode = str(payload.get("mode", "classic")).strip().lower()
    channel = int(payload.get("channel", 37 if mode != "classic" else 0))
    btc_center_mhz = float(payload.get("btc_center_mhz", 2442.0))
    sample_rate_sps = int(payload.get("sample_rate_sps", 60_000_000 if mode in {"classic", "both"} else BLE_ADV_SAMPLE_RATE_SPS))
    lna_gain_db = int(payload.get("lna_gain_db", 24))
    vga_gain_db = int(payload.get("vga_gain_db", 28))
    btc_lna_gain_db = int(payload.get("btc_lna_gain_db", lna_gain_db))
    btc_vga_gain_db = int(payload.get("btc_vga_gain_db", vga_gain_db))
    btle_lna_gain_db = int(payload.get("btle_lna_gain_db", lna_gain_db))
    btle_vga_gain_db = int(payload.get("btle_vga_gain_db", vga_gain_db))
    btc_target_mac = str(payload.get("btc_target_mac", "")).strip()
    preserve_detections = bool(payload.get("preserve_detections", False))
    btc_engine = str(payload.get("btc_engine", BTC_ENGINE_DEFAULT) or BTC_ENGINE_DEFAULT).strip().lower()
    saved_config = _read_ui_config()
    requested_protocols = payload.get("protocols")
    if isinstance(requested_protocols, list):
        enabled_protocols = {str(item).strip().lower() for item in requested_protocols}
    else:
        enabled_protocols = {str(item).strip().lower() for item in saved_config.get("protocols", [])}
    enabled_protocols &= RF_SENTINEL_PROTOCOLS
    requested_devices = payload.get("devices")
    if isinstance(requested_devices, list):
        enabled_devices = {str(item).strip() for item in requested_devices if str(item).strip()}
    else:
        disabled_devices = {str(item).strip() for item in saved_config.get("disabled_devices", []) if str(item).strip()}
        enabled_devices = _enabled_devices_from_disabled(_available_devices(), disabled_devices)
    sweep_both_radios = bool(payload.get("sweep_both_radios", mode == "sentinel"))
    single_radio_bluetooth_requested = bool(payload.get("single_radio_bluetooth") or payload.get("bluetooth_single_radio"))
    sentinel_hop_device_id = btle_device_id

    if RF_SENTINEL_DEMO_MODE:
        return _demo_start_response(enabled_protocols or set(RF_SENTINEL_PROTOCOLS), preserve_detections=preserve_detections)

    if mode not in {"ble", "classic", "both", "sentinel"}:
        return _json_error(400, "start_scan", error="mode must be ble, classic, both, or sentinel")
    if mode == "sentinel" and not enabled_protocols:
        return _json_error(400, "start_scan", error="select at least one protocol")
    if btc_engine not in {"btcsniffer", "python"}:
        return _json_error(400, "start_scan", error="btc_engine must be btcsniffer or python")
    if mode == "ble" and channel not in BLE_ADV_CHANNELS:
        return _json_error(400, "start_scan", error="BLE channel must be 37, 38, or 39")
    if mode in {"classic", "both", "sentinel"}:
        sample_rate_sps = max(1_000_000, min(60_000_000, sample_rate_sps))
        btc_center_mhz = max(2402.0, min(2480.0, btc_center_mhz))

    devices_available = _available_devices()
    if "wifi" in enabled_protocols and not _has_wifi_device(devices_available, enabled_devices):
        enabled_protocols.discard("wifi")
    if "lfmf" in enabled_protocols and not _has_lfmf_device(devices_available, enabled_devices):
        enabled_protocols.discard("lfmf")
    if mode == "sentinel" and not enabled_protocols:
        return _json_error(400, "start_scan", error="select at least one available protocol")
    combined_bluetooth_protocols = "btc" in enabled_protocols and "ble" in enabled_protocols
    if mode in {"both", "sentinel"} and combined_bluetooth_protocols:
        combined_device_id = _pick_ism24_bluetooth_device(devices_available, enabled_devices)
        if combined_device_id:
            btc_device_id = combined_device_id
            other_sdr_protocols = enabled_protocols & {"zigbee", "tpms", "fm", "cellular"}
            alternate_hop_device_id = _pick_non_bluetooth_hop_device(devices_available, combined_device_id, enabled_devices)
            # BTC and BLE share the proven wideband bladeRF command:
            # bluetooth_scanner --device-id <bladeRF> --center-mhz 2442 --bandwidth-mhz 60.
            # Keep the hop SDR for non-Bluetooth protocols only; sweep-both
            # cycles separate BTC/BLE jobs and starves the UI of steady updates.
            btle_device_id = combined_device_id
            sentinel_hop_device_id = alternate_hop_device_id or combined_device_id
            sweep_both_radios = False
            combined_rate_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, _btc_max_bandwidth_mhz_for_device(combined_device_id)))
            device_meta = next((dev for dev in devices_available if str(dev.get("id") or "") == combined_device_id), None)
            if device_meta is not None:
                combined_rate_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, _device_max_rate_mhz(device_meta)))
            sample_rate_sps = combined_rate_mhz * 1_000_000
            single_radio_bluetooth_requested = True
            _append_scanner_log(
                f"[ui] 2.4GHz ISM Bluetooth uses {combined_device_id} at {combined_rate_mhz} MHz "
                f"({'wideband' if combined_rate_mhz >= 60 else 'best available'})"
            )
            if alternate_hop_device_id and mode == "sentinel" and other_sdr_protocols:
                _append_scanner_log(f"[ui] non-Bluetooth SDR hopping uses {alternate_hop_device_id}")
            elif mode == "sentinel" and other_sdr_protocols:
                disabled = sorted(other_sdr_protocols)
                enabled_protocols -= other_sdr_protocols
                _append_scanner_log(
                    f"[ui] disabled {', '.join(disabled)} because no second SDR is available while BTC+BLE owns {combined_device_id}"
                )
    if mode in {"classic", "both", "sentinel"} and not btc_device_id:
        btc_device_id = _pick_device(devices_available, "bladerf")
    if mode in {"ble", "both", "sentinel"} and not btle_device_id:
        btle_device_id = _pick_device(devices_available, "hackrf", device_id or "sidekiq")
    if mode == "sentinel" and not sentinel_hop_device_id:
        sentinel_hop_device_id = btle_device_id
    if mode == "both" and btc_engine == "python" and btc_device_id and (single_radio_bluetooth_requested or not explicit_btle_device):
        btle_device_id = btc_device_id
    if mode == "classic" and not btc_device_id:
        return _json_error(400, "start_scan", error="btc_device_id is required")
    if mode == "ble" and not btle_device_id:
        return _json_error(400, "start_scan", error="btle_device_id is required")
    if mode == "both" and (not btc_device_id or not btle_device_id):
        return _json_error(400, "start_scan", error="both btc_device_id and btle_device_id are required")
    if mode == "sentinel" and (not btc_device_id or not btle_device_id):
        return _json_error(400, "start_scan", error="both btc_device_id and btle_device_id are required")

    btc_bandwidth_mhz = max(1, min(BT_CLASSIC_BANK_SIZE, int(round(sample_rate_sps / 1_000_000.0))))
    if mode in {"classic", "both", "sentinel"} and btc_device_id:
        btc_bandwidth_mhz = min(btc_bandwidth_mhz, _btc_max_bandwidth_mhz_for_device(btc_device_id))
        sample_rate_sps = btc_bandwidth_mhz * 1_000_000

    btc_center_freq_hz = int(round(btc_center_mhz * 1_000_000.0))
    btc_bank_start_channel = _btc_bank_start_from_center(btc_center_freq_hz, btc_bandwidth_mhz)
    center_freq_hz = btc_center_freq_hz if mode in {"classic", "both"} else _channel_freq(mode, channel)
    single_radio_bluetooth = mode == "both" and btc_engine == "python" and bool(btc_device_id) and btc_device_id == btle_device_id
    if mode == "sentinel":
        disabled_devices = {
            str(item.get("id") or "").strip()
            for item in devices_available
            if str(item.get("id") or "").strip() and str(item.get("id") or "").strip() not in enabled_devices
        }
        protocol_devices = _clean_protocol_devices(
            payload.get("protocol_devices", saved_config.get("protocol_devices")),
            enabled_devices,
            reserved_devices={btc_device_id},
        )
        wifi_channels = _clean_wifi_channels(payload.get("wifi_channels", saved_config.get("wifi_channels")))
        _write_ui_config(
            enabled_protocols,
            disabled_devices,
            wifi_channels=wifi_channels,
            protocol_devices=protocol_devices,
        )
    if state.running:
        _stop_scan()
    _start_csv_run()
    if btc_device_id:
        _stop_duplicate_gateway_streams(btc_device_id)
    if sentinel_hop_device_id and sentinel_hop_device_id != btc_device_id:
        _stop_duplicate_gateway_streams(sentinel_hop_device_id)

    btc_test_target: dict[str, Any] | None = None
    btc_test_error = ""
    if mode in {"classic", "both"}:
        btc_test_target = _configured_btc_target(btc_target_mac)
        if btc_target_mac and btc_test_target is None:
            btc_test_error = "BTC target MAC is invalid; set BTC_TARGET_MAC if needed"
        _stop_bredr_inquiry()
    else:
        _stop_bredr_inquiry()

    if mode == "sentinel":
        try:
            scanner_body = _start_rf_sentinel_engine(
                btc_device_id=btc_device_id,
                hop_device_id=sentinel_hop_device_id,
                btc_center_mhz=btc_center_mhz,
                btc_bandwidth_mhz=btc_bandwidth_mhz,
                btc_lna_gain_db=btc_lna_gain_db,
                btc_vga_gain_db=btc_vga_gain_db,
                hop_lna_gain_db=btle_lna_gain_db,
                hop_vga_gain_db=btle_vga_gain_db,
                enabled_protocols=enabled_protocols,
                enabled_devices=enabled_devices,
                sweep_both_radios=sweep_both_radios,
                fm_device_id=_fm_device_for_sentinel(devices_available, enabled_devices, sentinel_hop_device_id),
            )
        except RuntimeError as exc:
            return _json_error(400, "start_scan", error="scan start failed", detail=str(exc))
        with state_lock:
            if preserve_detections:
                _reset_live_stats_keep_discoveries()
            else:
                _reset_stats()
            state.running = True
            state.mode = "sentinel"
            state.stream_id = None
            state.stream_ids = {}
            state.device_id = btc_device_id
            state.device_ids = {"classic": btc_device_id, "btle": btle_device_id, "hop": sentinel_hop_device_id, "radio_a": btc_device_id, "radio_b": sentinel_hop_device_id}
            state.scanner_assignments = {}
            state.center_freq_hz = btc_center_freq_hz
            state.sample_rate_sps = btc_bandwidth_mhz * 1_000_000
            state.lna_gain_db = btc_lna_gain_db
            state.vga_gain_db = btc_vga_gain_db
            state.channel = btc_bank_start_channel
            state.channels_by_mode = {"classic": btc_bank_start_channel}
            state.gateway_start_response = {"scanner": scanner_body}
            state.btc_engine = "rf_sentinel_scan"
            state.btc_engine_command = list(scanner_body.get("command", []))
            state.btc_engine_log = ""
            _append_scanner_log(f"[ui] started {' '.join(state.btc_engine_command)}")
            _append_scanner_log("[ui] RF Sentinel scanner mode active")
            state.worker_error = ""
            state.worker_errors = {}
            state.worker_alive = True
            state.worker_alive_by_mode = {"scanner": True}
            state.test_target = btc_test_target
            state.test_target_error = btc_test_error
            state.decoder_stats["enabled_protocols"] = sorted(enabled_protocols)
            state.decoder_stats["sweep_both_radios"] = bool(sweep_both_radios)
            control = _read_rf_sentinel_control()
            state.decoder_stats["follow"] = _follow_state_for_protocols(control, enabled_protocols)
        return jsonify(
            {
                "ok": True,
                "mode": "sentinel",
                "scanner": scanner_body,
                "devices": {"classic": btc_device_id, "btle": btle_device_id, "hop": sentinel_hop_device_id, "radio_a": btc_device_id, "radio_b": sentinel_hop_device_id},
                "test_target": btc_test_target,
                "test_target_error": btc_test_error,
            }
        )

    try:
        started: dict[str, dict[str, Any]] = {}
        if single_radio_bluetooth:
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btc_device_id,
                center_freq_hz,
                sample_rate_sps,
                btc_lna_gain_db,
                btc_vga_gain_db,
            )
            started["both"] = {
                "engine": "python-combined",
                "body": body,
                "stream_id": body["stream_id"],
                "device_id": btc_device_id,
                "center_freq_hz": center_freq_hz,
                "sample_rate_sps": actual_rate,
                "lna_gain_db": actual_lna,
                "vga_gain_db": actual_vga,
                "channel": btc_bank_start_channel,
            }
        elif mode in {"classic", "both"}:
            if btc_engine == "btcsniffer":
                started["classic"] = _start_btcsniffer_engine(
                    btc_device_id,
                    center_freq_hz,
                    btc_bandwidth_mhz,
                    btc_bank_start_channel,
                )
            else:
                body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                    btc_device_id,
                    center_freq_hz,
                    sample_rate_sps,
                    btc_lna_gain_db,
                    btc_vga_gain_db,
                )
                started["classic"] = {
                    "engine": "python",
                    "body": body,
                    "stream_id": body["stream_id"],
                    "device_id": btc_device_id,
                    "center_freq_hz": center_freq_hz,
                    "sample_rate_sps": actual_rate,
                    "lna_gain_db": actual_lna,
                    "vga_gain_db": actual_vga,
                    "channel": btc_bank_start_channel,
                }
        if mode in {"ble", "both"} and not single_radio_bluetooth:
            ble_channel = int(payload.get("ble_channel", 37))
            ble_center = BLE_ADV_CHANNELS.get(ble_channel, BLE_ADV_CHANNELS[37])
            body, actual_rate, actual_lna, actual_vga = _start_gateway_stream(
                btle_device_id,
                ble_center,
                BLE_ADV_SAMPLE_RATE_SPS,
                btle_lna_gain_db,
                btle_vga_gain_db,
            )
            started["ble"] = {
                "body": body,
                "stream_id": body["stream_id"],
                "device_id": btle_device_id,
                "center_freq_hz": ble_center,
                "sample_rate_sps": actual_rate,
                "lna_gain_db": actual_lna,
                "vga_gain_db": actual_vga,
                "channel": ble_channel,
            }
    except requests.RequestException as exc:
        _stop_btcsniffer_engine()
        return jsonify({"error": "sdr-gateway is unavailable", "detail": str(exc), "gateway_base": _gateway_base()}), 503
    except RuntimeError as exc:
        _stop_btcsniffer_engine()
        return _json_error(400, "start_scan", error="scan start failed", detail=str(exc))

    worker_stop.clear()
    with state_lock:
        if preserve_detections:
            _reset_live_stats_keep_discoveries()
        else:
            _reset_stats()
        state.running = True
        state.mode = mode
        primary = started.get("classic") or started.get("ble") or started.get("both")
        state.stream_id = primary["stream_id"] if primary else None
        if "both" in started:
            state.stream_ids = {
                "both": started["both"]["stream_id"],
                "classic": started["both"]["stream_id"],
                "ble": started["both"]["stream_id"],
            }
        else:
            state.stream_ids = {key: value["stream_id"] for key, value in started.items()}
        state.device_id = primary["device_id"] if primary else None
        if "both" in started:
            state.device_ids = {
                "both": started["both"]["device_id"],
                "classic": started["both"]["device_id"],
                "ble": started["both"]["device_id"],
            }
        else:
            state.device_ids = {key: value["device_id"] for key, value in started.items()}
        state.center_freq_hz = int(primary["center_freq_hz"]) if primary else center_freq_hz
        state.sample_rate_sps = int(primary["sample_rate_sps"]) if primary else sample_rate_sps
        state.lna_gain_db = int(primary["lna_gain_db"]) if primary else lna_gain_db
        state.vga_gain_db = int(primary["vga_gain_db"]) if primary else vga_gain_db
        state.channel = btc_bank_start_channel if mode in {"classic", "both"} else channel
        if "both" in started:
            state.channels_by_mode = {
                "both": btc_bank_start_channel,
                "classic": btc_bank_start_channel,
                "ble": 0,
            }
        else:
            state.channels_by_mode = {key: int(value["channel"]) for key, value in started.items()}
        state.gateway_start_response = {key: value["body"] for key, value in started.items()}
        state.btc_engine = str((started.get("classic") or started.get("both") or {}).get("engine", "")) if mode in {"classic", "both"} else ""
        state.btc_engine_command = list(started.get("classic", {}).get("body", {}).get("command", []))
        state.btc_engine_log = str(started.get("classic", {}).get("body", {}).get("log", ""))
        state.worker_error = ""
        if mode in {"classic", "both"}:
            state.test_target = btc_test_target
            state.test_target_error = btc_test_error
        else:
            state.test_target = None
            state.test_target_error = ""

    worker_threads = {}
    worker_stops = {}
    for protocol, cfg in started.items():
        if cfg.get("engine") == "btcsniffer":
            continue
        stop = threading.Event()
        worker_stops[protocol] = stop
        worker_mode = "both" if protocol == "both" else ("classic" if protocol == "classic" else "ble")
        thread = threading.Thread(
            target=_worker_loop,
            args=(
                cfg["stream_id"],
                cfg["sample_rate_sps"],
                worker_mode,
                cfg["center_freq_hz"],
                cfg["channel"],
                stop,
            ),
            daemon=True,
        )
        worker_threads[protocol] = thread
        thread.start()
    worker_thread = next(iter(worker_threads.values()), None)
    return jsonify(
        {
            "ok": True,
            "mode": mode,
            "streams": {
                key: {
                    "stream_id": value["stream_id"],
                    "device_id": value["device_id"],
                    "center_freq_hz": value["center_freq_hz"],
                    "sample_rate_sps": value["sample_rate_sps"],
                }
                for key, value in started.items()
            },
            "test_target": btc_test_target,
            "test_target_error": btc_test_error,
        }
    )


@app.post("/api/scan/stop")
def stop_scan():
    if RF_SENTINEL_DEMO_MODE:
        return _demo_pause_response()
    _stop_scan()
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    gateway_live_centers = {} if RF_SENTINEL_DEMO_MODE else _sync_scanner_assignment_centers_from_gateway()
    devices = _available_devices()
    ui_config = _read_ui_config()
    with state_lock:
        enabled_protocols = {str(item).lower() for item in state.decoder_stats.get("enabled_protocols", ui_config.get("protocols", []))}
        follow_target = _follow_state_for_protocols({"follow": state.decoder_stats.get("follow", {})}, enabled_protocols)
        return jsonify(
            {
                "running": state.running,
                "mode": state.mode,
                "stream_id": state.stream_id,
                "stream_ids": state.stream_ids,
                "device_id": state.device_id,
                "device_ids": state.device_ids,
                "center_freq_hz": state.center_freq_hz,
                "sample_rate_sps": state.sample_rate_sps,
                "lna_gain_db": state.lna_gain_db,
                "vga_gain_db": state.vga_gain_db,
                "channel": state.channel,
                "channels_by_mode": state.channels_by_mode,
                "worker_alive": state.worker_alive,
                "worker_alive_by_mode": state.worker_alive_by_mode,
                "worker_error": state.worker_error,
                "worker_errors": state.worker_errors,
                "chunks_seen": state.chunks_seen,
                "bytes_seen": state.bytes_seen,
                "last_rssi_dbfs": state.last_rssi_dbfs,
                "rssi_by_mode": state.rssi_by_mode,
                "chunks_by_mode": state.chunks_by_mode,
                "bytes_by_mode": state.bytes_by_mode,
                "noise_floor_dbfs": state.noise_floor_dbfs,
                "bursts_seen": state.bursts_seen,
                "ble_packets_seen": state.ble_packets_seen,
                "classic_bursts_seen": state.classic_bursts_seen,
                "detections": state.detections[:120],
                "discovery_table": state.discovery_table,
                "classic_candidates": state.classic_candidates[:32],
                "classic_addresses": state.classic_addresses[:64],
                "decoder_stats": {**state.decoder_stats, "follow": follow_target},
                "follow_target": follow_target,
                "test_target": state.test_target,
                "test_target_error": state.test_target_error,
                "btc_engine": state.btc_engine,
                "btc_engine_command": state.btc_engine_command,
                "btc_engine_log": state.btc_engine_log,
                "scanner_log": state.scanner_log[-160:],
                "scanner_assignments": state.scanner_assignments,
                "gateway_live_centers": gateway_live_centers,
                "csv_run_id": state.csv_run_id,
                "csv_log_dir": state.csv_log_dir,
                "ui_config": ui_config,
                "fm_playback": _fm_playback_status_payload(),
                "walkie_playback": _walkie_playback_status_payload(),
                "available_devices": devices,
                "channel_activity": [
                    state.channel_activity.get(idx, {"channel": idx, "hits": 0, "rssi_dbfs": -120.0})
                    for idx in range(79)
                ],
                "gateway_start_response": state.gateway_start_response,
            }
        )


@app.post("/api/test/discoverable-dongle")
def enable_discoverable_dongle():
    try:
        target, output = _enable_discoverable_controller()
    except FileNotFoundError:
        return jsonify({"error": "bluetoothctl is not installed or not on PATH"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "bluetoothctl timed out while enabling discoverable mode"}), 504
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500

    with state_lock:
        state.test_target = target
    return jsonify(
        {
            "ok": True,
            "target": target,
            "message": f"Discoverable BTC test target armed: LAP {target['lap']} / UAP {target['uap']}",
            "bluetoothctl_output": output,
        }
    )


@app.post("/api/clear")
def clear():
    if RF_SENTINEL_DEMO_MODE:
        return _demo_clear_response()
    with state_lock:
        _reset_stats()
    return jsonify({"ok": True})


# Module-level, not inside `if __name__ == "__main__":` below - this file
# is loaded via importlib.exec_module() by rf_platform/ui.py (the
# container's actual entrypoint), under a different module name, so that
# guard never runs here. Placed at true end-of-file (not right after the
# function definition earlier) so every module-level name it touches
# (state, state_lock, _scanner_json_to_events, _append_detections) is
# guaranteed to already exist before the thread's first iteration can
# possibly run, instead of racing module exec.
if RF_SENTINEL_DEMO_MODE:
    threading.Thread(target=_demo_replay_loop, daemon=True).start()
else:
    threading.Thread(target=_shared_bt_detector_poll_loop, daemon=True).start()


if __name__ == "__main__":
    host = os.getenv("BT_EXPLORER_HOST", "0.0.0.0")
    port = int(os.getenv("BT_EXPLORER_PORT", "5050"))
    try:
        if os.getenv("RF_SENTINEL_TEXTUAL_CONSOLE", "1").strip().lower() not in {"0", "false", "no", "off"} and TextualApp is not None:
            flask_thread = threading.Thread(
                target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
                daemon=True,
            )
            flask_thread.start()
            run_textual_console_dashboard(host, port)
        else:
            start_console_dashboard(host, port)
            app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[ui] Ctrl+C received, disconnecting from sdr-gateway...", file=sys.stderr)
    finally:
        shutdown()
