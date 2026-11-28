from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import requests
import websocket

from .dsp import AMMetrics, channel_grid, cs16_to_complex, i8_to_complex, measure_am_channel


DEFAULT_START_KHZ = 530
DEFAULT_STOP_KHZ = 1700
DEFAULT_STEP_KHZ = 10
DEFAULT_SAMPLE_RATE_SPS = 250_000
DEFAULT_BANDWIDTH_HZ = 80_000
DEFAULT_BAND = "am"
DEFAULT_GATEWAY_BASE_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class BandPreset:
    label: str
    start_khz: int
    stop_khz: int
    step_khz: int
    description: str


BAND_PRESETS: dict[str, BandPreset] = {
    "vlf": BandPreset("VLF", 3, 30, 1, "Very Low Frequency, 3-30 kHz"),
    "lf": BandPreset("LF", 30, 300, 5, "Low Frequency, 30-300 kHz"),
    "mf": BandPreset("MF", 300, 3000, 10, "Medium Frequency, 300 kHz-3 MHz"),
    "am": BandPreset("AM broadcast", 530, 1700, 10, "Medium-wave AM broadcast band"),
    "1khz-1mhz": BandPreset("1 kHz-1 MHz", 1, 1000, 5, "VLF/LF/lower-MF survey range"),
    "vlf-lf-mf": BandPreset("VLF/LF/MF", 3, 3000, 10, "VLF through MF survey range"),
}


@contextlib.contextmanager
def _suppress_native_output(enabled: bool) -> Any:
    if not enabled:
        yield
        return
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


@contextlib.contextmanager
def _soapy_log_level(SoapySDR: Any, level: int | None) -> Any:
    if level is None or not hasattr(SoapySDR, "setLogLevel"):
        yield
        return
    previous = None
    try:
        if hasattr(SoapySDR, "getLogLevel"):
            previous = SoapySDR.getLogLevel()
        SoapySDR.setLogLevel(level)
        yield
    finally:
        if previous is not None:
            SoapySDR.setLogLevel(previous)


@dataclass
class ScanResult:
    freq_hz: int
    freq_khz: float
    protocol: str
    protocol_detail: str
    priority: int
    power_dbfs: float
    carrier_dbfs: float
    carrier_snr_db: float
    audio_dbfs: float
    modulation_pct: float
    excess_db: float
    samples: int
    active: bool
    confirmation: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReceiverConfig:
    sample_rate_sps: int
    bandwidth_hz: int
    tune_offset_hz: int
    format: str = "CS16"
    backend: str = "direct"


@dataclass(frozen=True)
class ScanPlan:
    band: str
    label: str
    start_khz: int
    stop_khz: int
    step_khz: int

    @property
    def channel_count(self) -> int:
        if self.stop_khz < self.start_khz or self.step_khz <= 0:
            return 0
        return ((self.stop_khz - self.start_khz) // self.step_khz) + 1


class GatewayReceiver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = str(args.gateway_base_url or os.environ.get("SDR_GATEWAY_BASE_URL") or DEFAULT_GATEWAY_BASE_URL).rstrip("/")
        self.token = str(args.gateway_token or os.environ.get("SDR_GATEWAY_API_TOKEN") or "").strip()
        self.session = requests.Session()
        self.stream_id = ""
        self.ws: websocket.WebSocket | None = None
        self.config = ReceiverConfig(
            sample_rate_sps=int(args.sample_rate_sps),
            bandwidth_hz=int(args.bandwidth_hz),
            tune_offset_hz=int(args.tune_offset_hz),
            format="I8",
            backend="gateway",
        )

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def start(self, first_freq_hz: int) -> ReceiverConfig:
        payload = self._stream_payload(first_freq_hz)
        resp = self.session.post(f"{self.base_url}/streams/start", json=payload, headers=self.headers(), timeout=15)
        if resp.status_code == 409 and bool(getattr(self.args, "replace_existing", False)):
            self._stop_existing_for_device(str(self.args.device_id))
            resp = self.session.post(f"{self.base_url}/streams/start", json=payload, headers=self.headers(), timeout=15)
        resp.raise_for_status()
        body = resp.json()
        self.stream_id = str(body["stream_id"])
        self.ws = websocket.WebSocket()
        ws_url = self._ws_url(self.stream_id)
        headers = [f"Authorization: Bearer {self.token}"] if self.token else None
        self.ws.connect(ws_url, timeout=8, header=headers)
        return self.config

    def retune(self, freq_hz: int) -> None:
        if not self.stream_id:
            return
        resp = self.session.post(
            f"{self.base_url}/streams/{self.stream_id}/retune",
            json=self._stream_payload(freq_hz),
            headers=self.headers(),
            timeout=15,
        )
        resp.raise_for_status()
        if float(getattr(self.args, "settle_s", 0.0)) > 0:
            time.sleep(float(self.args.settle_s))

    def capture(self, freq_hz: int) -> np.ndarray:
        self.retune(freq_hz)
        target_bytes = max(2048, int(float(self.args.sample_rate_sps) * float(self.args.dwell_s)) * 2)
        deadline = time.monotonic() + max(0.5, float(self.args.dwell_s) + 1.5)
        chunks: list[bytes] = []
        captured = 0
        ws = self.ws
        if ws is None:
            return np.empty(0, dtype=np.complex64)
        while captured < target_bytes and time.monotonic() < deadline:
            try:
                message = ws.recv()
            except Exception as exc:
                if self.args.debug:
                    print(f"lowfreq_gateway_read_warning freq_khz={freq_hz/1000:.1f} error={exc}", file=sys.stderr, flush=True)
                break
            if isinstance(message, str):
                continue
            if not message:
                continue
            chunks.append(bytes(message))
            captured += len(message)
        if not chunks:
            return np.empty(0, dtype=np.complex64)
        return i8_to_complex(b"".join(chunks))

    def recv_raw(self, target_bytes: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        captured = 0
        ws = self.ws
        if ws is None:
            return b""
        while captured < target_bytes and time.monotonic() < deadline:
            try:
                message = ws.recv()
            except Exception as exc:
                if self.args.debug:
                    print(f"lowfreq_gateway_record_warning error={exc}", file=sys.stderr, flush=True)
                break
            if isinstance(message, str) or not message:
                continue
            chunk = bytes(message)
            chunks.append(chunk)
            captured += len(chunk)
        if not chunks:
            return b""
        payload = b"".join(chunks)
        return payload[:target_bytes]

    def close(self) -> None:
        ws = self.ws
        self.ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self.stream_id:
            try:
                self.session.post(f"{self.base_url}/streams/{self.stream_id}/stop", headers=self.headers(), timeout=5)
            except Exception:
                pass
            self.stream_id = ""

    def _stream_payload(self, freq_hz: int) -> dict[str, Any]:
        return {
            "device_id": str(self.args.device_id or "sdrplay:0"),
            "center_freq_hz": int(freq_hz + int(self.args.tune_offset_hz) + int(getattr(self.args, "upconverter_offset_hz", 0) or 0)),
            "sample_rate_sps": int(self.args.sample_rate_sps),
            "baseband_filter_hz": int(self.args.bandwidth_hz),
            "lna_gain_db": int(float(self.args.rfgr)),
            "vga_gain_db": int(float(self.args.ifgr)),
            "amp_enable": False,
            "replace_existing": bool(getattr(self.args, "replace_existing", False)),
        }

    def _ws_url(self, stream_id: str) -> str:
        if self.base_url.startswith("https://"):
            root = "wss://" + self.base_url[len("https://") :]
        elif self.base_url.startswith("http://"):
            root = "ws://" + self.base_url[len("http://") :]
        else:
            root = "ws://" + self.base_url
        token = requests.utils.quote(self.token, safe="") if self.token else ""
        suffix = f"?keep=1&start=oldest&token={token}" if token else "?keep=1&start=oldest"
        return f"{root}/ws/iq/{stream_id}{suffix}"

    def _stop_existing_for_device(self, device_id: str) -> None:
        try:
            resp = self.session.get(f"{self.base_url}/streams", headers=self.headers(), timeout=5)
            resp.raise_for_status()
        except Exception:
            return
        for item in resp.json():
            config = item.get("config") or {}
            if str(config.get("device_id") or "") != device_id:
                continue
            stream_id = str(item.get("stream_id") or "")
            if not stream_id:
                continue
            with contextlib.suppress(Exception):
                self.session.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self.headers(), timeout=3)


def _load_soapysdr() -> Any:
    try:
        import SoapySDR  # type: ignore[import-not-found]

        return SoapySDR
    except ImportError:
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        candidates = (
            Path("/usr/local/lib") / version / "site-packages",
            Path("/usr/lib") / version / "site-packages",
            Path("/usr/lib") / version / "dist-packages",
        )
        for path in candidates:
            if path.exists() and str(path) not in sys.path:
                sys.path.append(str(path))
        try:
            import SoapySDR  # type: ignore[import-not-found]

            return SoapySDR
        except ImportError as exc:
            raise RuntimeError(
                "SoapySDR Python bindings are not importable. Install python3-soapysdr or run from the SDR environment."
            ) from exc


def _open_sdrplay(SoapySDR: Any, args: argparse.Namespace) -> Any:
    serial = str(args.serial or os.environ.get("SDRPLAY_SERIAL", "")).strip()
    kwargs: dict[str, str] = {"driver": "sdrplay"}
    if serial:
        kwargs["serial"] = serial
    return SoapySDR.Device(kwargs)


def _try_call(label: str, func: Any, *args: Any, debug: bool = False) -> None:
    try:
        func(*args)
    except Exception as exc:
        if debug:
            print(f"am_broadcast_config_warning setting={label} error={exc}", file=sys.stderr, flush=True)


def _set_named_gain(SoapySDR: Any, dev: Any, name: str, value: float, *, debug: bool = False) -> None:
    try:
        names = set(dev.listGains(SoapySDR.SOAPY_SDR_RX, 0))
    except Exception:
        names = set()
    if name in names:
        _try_call(f"gain:{name}", dev.setGain, SoapySDR.SOAPY_SDR_RX, 0, name, float(value), debug=debug)


def _read_int_setting(label: str, fallback: int, func: Any, *args: Any, debug: bool = False) -> int:
    try:
        value = func(*args)
    except Exception as exc:
        if debug:
            print(f"am_broadcast_readback_warning setting={label} error={exc}", file=sys.stderr, flush=True)
        return int(fallback)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(fallback)


def _configure_receiver(SoapySDR: Any, dev: Any, args: argparse.Namespace) -> tuple[Any, ReceiverConfig]:
    direction = SoapySDR.SOAPY_SDR_RX
    channel = 0
    _try_call("sample_rate", dev.setSampleRate, direction, channel, int(args.sample_rate_sps), debug=args.debug)
    _try_call("bandwidth", dev.setBandwidth, direction, channel, int(args.bandwidth_hz), debug=args.debug)
    _try_call("gain_mode", dev.setGainMode, direction, channel, False, debug=args.debug)
    _set_named_gain(SoapySDR, dev, "RFGR", float(args.rfgr), debug=args.debug)
    _set_named_gain(SoapySDR, dev, "IFGR", float(args.ifgr), debug=args.debug)
    if args.antenna:
        _try_call("antenna", dev.setAntenna, direction, channel, str(args.antenna), debug=args.debug)
    stream = dev.setupStream(direction, SoapySDR.SOAPY_SDR_CS16, [channel])
    dev.activateStream(stream)
    config = ReceiverConfig(
        sample_rate_sps=_read_int_setting(
            "sample_rate",
            int(args.sample_rate_sps),
            dev.getSampleRate,
            direction,
            channel,
            debug=args.debug,
        ),
        bandwidth_hz=_read_int_setting(
            "bandwidth",
            int(args.bandwidth_hz),
            dev.getBandwidth,
            direction,
            channel,
            debug=args.debug,
        ),
        tune_offset_hz=int(args.tune_offset_hz),
    )
    return stream, config


def _resolve_scan_plan(args: argparse.Namespace) -> ScanPlan:
    band = str(args.band)
    preset = BAND_PRESETS.get(band)
    if preset is None:
        preset = BAND_PRESETS[DEFAULT_BAND]
    start_khz = preset.start_khz if args.start_khz is None else int(args.start_khz)
    stop_khz = preset.stop_khz if args.stop_khz is None else int(args.stop_khz)
    step_khz = preset.step_khz if args.step_khz is None else int(args.step_khz)
    label = "Custom" if args.start_khz is not None or args.stop_khz is not None or args.step_khz is not None else preset.label
    return ScanPlan(band=band, label=label, start_khz=start_khz, stop_khz=stop_khz, step_khz=step_khz)


def _capture_channel(SoapySDR: Any, dev: Any, stream: Any, freq_hz: int, args: argparse.Namespace) -> np.ndarray:
    direction = SoapySDR.SOAPY_SDR_RX
    channel = 0
    dev.setFrequency(direction, channel, int(freq_hz + int(args.tune_offset_hz)))
    if args.settle_s > 0:
        time.sleep(float(args.settle_s))

    target_samples = max(1024, int(float(args.sample_rate_sps) * float(args.dwell_s)))
    chunk_samples = min(16384, max(1024, target_samples))
    buf = np.empty(chunk_samples * 2, dtype=np.int16)
    chunks: list[np.ndarray] = []
    captured = 0
    deadline = time.monotonic() + max(0.1, float(args.dwell_s) + 0.5)
    while captured < target_samples and time.monotonic() < deadline:
        result = dev.readStream(stream, [buf], chunk_samples, timeoutUs=int(args.timeout_ms * 1000))
        count = int(getattr(result, "ret", result[0] if isinstance(result, tuple) else -1))
        if count > 0:
            chunks.append(buf[: count * 2].copy())
            captured += count
            continue
        if args.debug:
            print(f"am_broadcast_read_warning freq_khz={freq_hz/1000:.1f} ret={count}", file=sys.stderr, flush=True)
    if not chunks:
        return np.empty(0, dtype=np.complex64)
    return cs16_to_complex(np.concatenate(chunks))


def _scan(args: argparse.Namespace) -> int:
    scan_plan = _resolve_scan_plan(args)
    freqs = channel_grid(scan_plan.start_khz, scan_plan.stop_khz, scan_plan.step_khz)
    if not freqs:
        print("No channels in requested range.", file=sys.stderr)
        return 2
    if scan_plan.channel_count > 400 and not args.yes:
        estimated_s = scan_plan.channel_count * max(0.02, float(args.dwell_s))
        print(
            f"Refusing long scan: {scan_plan.channel_count} channels, about {estimated_s:.0f}s of dwell time. "
            "Use --yes to run it, or increase --step-khz.",
            file=sys.stderr,
        )
        return 2

    if bool(getattr(args, "wideband", False)):
        return _scan_wideband(args, scan_plan, freqs)

    raw_results: list[tuple[int, AMMetrics]] = []
    if str(args.backend) == "gateway":
        receiver = GatewayReceiver(args)
        receiver_config = receiver.start(freqs[0])
        try:
            for index, freq_hz in enumerate(freqs, start=1):
                if args.debug:
                    print(f"lowfreq_gateway_scan freq_khz={freq_hz/1000:.1f} channel={index}/{len(freqs)}", file=sys.stderr, flush=True)
                iq = receiver.capture(freq_hz)
                raw_results.append(
                    (
                        freq_hz,
                        measure_am_channel(
                            iq,
                            int(args.sample_rate_sps),
                            carrier_offset_hz=-float(args.tune_offset_hz),
                            carrier_width_hz=float(args.carrier_width_hz),
                        ),
                    )
                )
        finally:
            receiver.close()
    else:
        SoapySDR = _load_soapysdr()
        quiet_log_level = None if args.show_driver_log else int(getattr(SoapySDR, "SOAPY_SDR_ERROR", 3))
        with _soapy_log_level(SoapySDR, quiet_log_level), _suppress_native_output(not bool(args.show_driver_log)):
            dev = _open_sdrplay(SoapySDR, args)
            stream, receiver_config = _configure_receiver(SoapySDR, dev, args)
            if not args.show_driver_log:
                time.sleep(0.05)
        try:
            for index, freq_hz in enumerate(freqs, start=1):
                if args.debug:
                    print(f"am_broadcast_scan freq_khz={freq_hz/1000:.1f} channel={index}/{len(freqs)}", file=sys.stderr, flush=True)
                iq = _capture_channel(SoapySDR, dev, stream, freq_hz, args)
                raw_results.append(
                    (
                        freq_hz,
                        measure_am_channel(
                            iq,
                            int(args.sample_rate_sps),
                            carrier_offset_hz=-float(args.tune_offset_hz),
                            carrier_width_hz=float(args.carrier_width_hz),
                        ),
                    )
                )
        finally:
            try:
                dev.deactivateStream(stream)
            finally:
                dev.closeStream(stream)

    noise_floor = float(np.median([metrics.carrier_dbfs for _, metrics in raw_results])) if raw_results else -160.0
    rows = [_to_result(freq_hz, metrics, noise_floor, float(args.active_threshold_db)) for freq_hz, metrics in raw_results]
    if str(args.sort) == "score":
        rows.sort(key=lambda row: (row.active, row.excess_db, row.power_dbfs), reverse=True)
    else:
        rows.sort(key=lambda row: row.freq_hz)
    if args.top > 0:
        rows = rows[: int(args.top)]
    _print_results(rows, args, noise_floor, receiver_config, scan_plan)
    return 0


def _scan_wideband(args: argparse.Namespace, scan_plan: ScanPlan, freqs: list[int]) -> int:
    if str(args.backend) != "gateway":
        raise RuntimeError("--wideband currently uses --backend gateway")
    start_hz = int(scan_plan.start_khz) * 1000
    stop_hz = int(scan_plan.stop_khz) * 1000
    center_hz = int(args.wideband_center_khz) * 1000 if int(args.wideband_center_khz) > 0 else int((start_hz + stop_hz) // 2)
    old_tune_offset = int(args.tune_offset_hz)
    args.tune_offset_hz = 0
    args.sample_rate_sps = int(args.wideband_sample_rate_sps or args.sample_rate_sps)
    args.bandwidth_hz = int(args.wideband_bandwidth_hz or args.bandwidth_hz or args.sample_rate_sps)

    receiver = GatewayReceiver(args)
    receiver_config = receiver.start(center_hz)
    target_bytes = max(2048, int(float(args.sample_rate_sps) * float(args.dwell_s)) * 2)
    deadline = time.monotonic() + max(1.0, float(args.dwell_s) + 2.0)
    try:
        raw = receiver.recv_raw(target_bytes, deadline)
    finally:
        receiver.close()
        args.tune_offset_hz = old_tune_offset
    iq = i8_to_complex(raw)
    if args.debug:
        print(
            f"lowfreq_wideband center_khz={center_hz/1000:.1f} sr={args.sample_rate_sps} "
            f"bandwidth={args.bandwidth_hz} upconverter_offset_hz={int(getattr(args, 'upconverter_offset_hz', 0) or 0)} "
            f"bytes={len(raw)} samples={iq.size}",
            file=sys.stderr,
            flush=True,
        )

    raw_results: list[tuple[int, AMMetrics]] = []
    half_span = float(args.sample_rate_sps) * 0.48
    for freq_hz in freqs:
        offset = float(freq_hz - center_hz)
        if abs(offset) > half_span:
            continue
        raw_results.append(
            (
                freq_hz,
                measure_am_channel(
                    iq,
                    int(args.sample_rate_sps),
                    carrier_offset_hz=offset,
                    carrier_width_hz=float(args.carrier_width_hz),
                ),
            )
        )

    noise_floor = float(np.median([metrics.carrier_dbfs for _, metrics in raw_results])) if raw_results else -160.0
    rows = [_to_result(freq_hz, metrics, noise_floor, float(args.active_threshold_db)) for freq_hz, metrics in raw_results]
    rows = _cluster_rows(rows, int(args.cluster_hz))
    if bool(getattr(args, "confirm", False)):
        if bool(getattr(args, "confirm_retune", True)):
            rows = _confirm_rows_with_retune(args, rows)
        elif iq.size > 0:
            rows = [_confirm_result(row, iq, int(args.sample_rate_sps), center_hz) for row in rows]
    if str(args.sort) == "score":
        rows.sort(key=lambda row: (row.active, row.excess_db, row.power_dbfs), reverse=True)
    else:
        rows.sort(key=lambda row: row.freq_hz)
    if args.top > 0:
        rows = rows[: int(args.top)]
    _print_results(rows, args, noise_floor, receiver_config, scan_plan)
    return 0


def _confirm_rows_with_retune(args: argparse.Namespace, rows: list[ScanResult]) -> list[ScanResult]:
    active = [row for row in rows if row.active]
    if not active:
        return rows
    limit = max(1, int(getattr(args, "confirm_max_candidates", 8)))
    active = sorted(active, key=lambda row: (row.excess_db, row.carrier_snr_db, -row.priority), reverse=True)[:limit]

    old_sample_rate = int(args.sample_rate_sps)
    old_bandwidth = int(args.bandwidth_hz)
    old_tune_offset = int(args.tune_offset_hz)
    old_dwell = float(args.dwell_s)
    args.sample_rate_sps = int(args.confirm_sample_rate_sps)
    args.bandwidth_hz = int(args.confirm_bandwidth_hz)
    args.tune_offset_hz = int(args.confirm_tune_offset_hz)
    args.dwell_s = float(args.confirm_dwell_s)
    receiver = GatewayReceiver(args)
    by_freq = {row.freq_hz: row for row in rows}
    try:
        first_freq = active[0].freq_hz
        receiver.start(first_freq)
        for row in active:
            if args.debug:
                print(
                    f"lowfreq_confirm_retune freq_khz={row.freq_khz:.1f} "
                    f"sr={args.sample_rate_sps} bandwidth={args.bandwidth_hz}",
                    file=sys.stderr,
                    flush=True,
                )
            iq = receiver.capture(row.freq_hz)
            confirmed = _confirm_result(row, iq, int(args.sample_rate_sps), row.freq_hz + int(args.confirm_tune_offset_hz))
            if confirmed.confirmation is not None:
                confirmed.confirmation.update(
                    {
                        "confirm_sample_rate_sps": int(args.sample_rate_sps),
                        "confirm_bandwidth_hz": int(args.bandwidth_hz),
                        "confirm_tune_offset_hz": int(args.tune_offset_hz),
                    }
                )
            by_freq[row.freq_hz] = confirmed
    finally:
        receiver.close()
        args.sample_rate_sps = old_sample_rate
        args.bandwidth_hz = old_bandwidth
        args.tune_offset_hz = old_tune_offset
        args.dwell_s = old_dwell
    return [by_freq.get(row.freq_hz, row) for row in rows]


def _cluster_rows(rows: list[ScanResult], cluster_hz: int) -> list[ScanResult]:
    if cluster_hz <= 0:
        return rows
    inactive = [row for row in rows if not row.active]
    active = sorted((row for row in rows if row.active), key=lambda row: row.freq_hz)
    clusters: list[list[ScanResult]] = []
    for row in active:
        if not clusters or row.freq_hz - clusters[-1][-1].freq_hz > cluster_hz:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    kept = [
        max(cluster, key=lambda row: (row.excess_db, row.carrier_snr_db, row.power_dbfs))
        for cluster in clusters
    ]
    if inactive and len(rows) <= 100:
        kept.extend(inactive)
    return kept


def _record(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    receiver = GatewayReceiver(args)
    center_hz = int(args.center_khz) * 1000
    receiver_config = receiver.start(center_hz)
    target_bytes = max(2, int(float(args.sample_rate_sps) * float(args.seconds)) * 2)
    deadline = time.monotonic() + max(float(args.seconds) + 5.0, 5.0)
    started_at = time.time()
    written = 0
    try:
        with output.open("wb") as fh:
            while written < target_bytes and time.monotonic() < deadline:
                payload = receiver.recv_raw(min(512 * 1024, target_bytes - written), deadline)
                if not payload:
                    time.sleep(0.02)
                    continue
                fh.write(payload)
                written += len(payload)
                if args.debug:
                    print(f"lowfreq_record bytes={written}/{target_bytes}", file=sys.stderr, flush=True)
    finally:
        receiver.close()

    metadata = {
        "kind": "lowfreq_i8_recording",
        "path": str(output),
        "bytes": written,
        "complex_samples": written // 2,
        "center_freq_hz": center_hz + int(args.tune_offset_hz),
        "target_freq_hz": center_hz,
        "sample_rate_sps": int(receiver_config.sample_rate_sps),
        "bandwidth_hz": int(receiver_config.bandwidth_hz),
        "tune_offset_hz": int(receiver_config.tune_offset_hz),
        "format": "i8_interleaved_iq",
        "device_id": str(args.device_id),
        "started_at": started_at,
        "duration_s": round(float(written // 2) / float(args.sample_rate_sps), 3) if args.sample_rate_sps else 0.0,
    }
    metadata_path = output.with_suffix(output.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(
        f"lowfreq_record complete file={output} bytes={written} "
        f"samples={written // 2} metadata={metadata_path}"
    )
    return 0


def _to_result(freq_hz: int, metrics: AMMetrics, noise_floor: float, active_threshold_db: float) -> ScanResult:
    excess = round(float(metrics.carrier_dbfs - noise_floor), 1)
    protocol, detail, priority = _classify_lfmf_signal(freq_hz, metrics, excess)
    return ScanResult(
        freq_hz=int(freq_hz),
        freq_khz=round(float(freq_hz) / 1000.0, 1),
        protocol=protocol,
        protocol_detail=detail,
        priority=priority,
        power_dbfs=float(metrics.power_dbfs),
        carrier_dbfs=float(metrics.carrier_dbfs),
        carrier_snr_db=float(metrics.carrier_snr_db),
        audio_dbfs=float(metrics.audio_dbfs),
        modulation_pct=float(metrics.modulation_pct),
        excess_db=excess,
        samples=int(metrics.samples),
        active=bool(excess >= active_threshold_db and metrics.carrier_snr_db >= active_threshold_db and metrics.samples > 0),
    )


def _confirm_result(row: ScanResult, iq: np.ndarray, sample_rate_sps: int, center_hz: int) -> ScanResult:
    offset_hz = float(row.freq_hz - center_hz)
    confirmation = _protocol_confirmation(row.protocol, iq, sample_rate_sps, offset_hz)
    row.confirmation = confirmation
    if confirmation:
        confidence = float(confirmation.get("confidence", 0.0))
        verdict = str(confirmation.get("verdict", "candidate"))
        row.protocol_detail = f"{row.protocol_detail}; confirmation={verdict} confidence={confidence:.2f}"
    return row


def _protocol_confirmation(protocol: str, iq: np.ndarray, sample_rate_sps: int, offset_hz: float) -> dict[str, Any]:
    baseband = _frequency_shift(iq, sample_rate_sps, offset_hz)
    if protocol == "NAVTEX":
        return _confirm_navtex(baseband, sample_rate_sps)
    if protocol == "Non-Directional Beacon":
        return _confirm_non_directional_beacon(baseband, sample_rate_sps)
    if protocol == "Amplitude Modulation":
        return _confirm_amplitude_modulation(baseband, sample_rate_sps)
    if protocol == "Continuous Wave":
        return _confirm_continuous_wave(baseband, sample_rate_sps)
    if protocol == "Single-sideband":
        return _confirm_single_sideband(baseband, sample_rate_sps)
    return {}


def _frequency_shift(iq: np.ndarray, sample_rate_sps: int, offset_hz: float) -> np.ndarray:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size == 0 or abs(offset_hz) < 1e-6:
        return samples
    n = np.arange(samples.size, dtype=np.float32)
    rotator = np.exp(-2j * np.pi * float(offset_hz) * n / float(sample_rate_sps)).astype(np.complex64)
    return (samples * rotator).astype(np.complex64, copy=False)


def _confirm_navtex(baseband: np.ndarray, sample_rate_sps: int) -> dict[str, Any]:
    freqs, power_db = _fm_audio_spectrum(baseband, sample_rate_sps, decim=20, max_hz=500.0)
    if freqs.size == 0:
        return {"verdict": "candidate", "confidence": 0.0}
    mark = _band_peak_db(freqs, power_db, 70.0, 110.0)
    space = _band_peak_db(freqs, power_db, 150.0, 190.0)
    local = _median_db(freqs, power_db, 20.0, 260.0)
    tone_excess = max(0.0, min(mark, space) - local)
    confidence = float(np.clip(tone_excess / 18.0, 0.0, 1.0))
    verdict = "confirmed" if confidence >= 0.65 else "candidate"
    summary = (
        f"NAVTEX FSK tone pair present, excess {tone_excess:.1f} dB"
        if verdict == "confirmed"
        else f"NAVTEX channel candidate, FSK tone-pair evidence weak ({tone_excess:.1f} dB)"
    )
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "method": "SITOR-B/NAVTEX FSK tone energy",
        "evidence_summary": summary,
        "status_reason": "fsk_tone_pair_detected" if verdict == "confirmed" else "weak_fsk_tone_pair",
        "mark_tone_db": round(mark, 1),
        "space_tone_db": round(space, 1),
        "tone_excess_db": round(tone_excess, 1),
    }


def _confirm_non_directional_beacon(baseband: np.ndarray, sample_rate_sps: int) -> dict[str, Any]:
    freqs, power_db = _envelope_spectrum(baseband, sample_rate_sps, decim=20, max_hz=1600.0)
    if freqs.size == 0:
        return {"verdict": "candidate", "confidence": 0.0}
    tone = _band_peak_db(freqs, power_db, 350.0, 1100.0)
    local = _median_db(freqs, power_db, 150.0, 1400.0)
    tone_excess = max(0.0, tone - local)
    keying = _keying_score(baseband, sample_rate_sps)
    cadence_plausible = 0.02 <= keying <= 0.85
    confidence = float(np.clip((tone_excess / 22.0) * 0.65 + keying * 0.35, 0.0, 1.0))
    if not cadence_plausible:
        confidence = min(confidence, 0.55)
    verdict = "confirmed" if confidence >= 0.65 else "candidate"
    if verdict == "confirmed":
        summary = f"NDB Morse-tone energy present with plausible keying cadence, tone excess {tone_excess:.1f} dB"
        reason = "morse_tone_and_plausible_keying"
    elif not cadence_plausible:
        summary = "NDB-band tone energy present, but Morse keying cadence is not plausible"
        reason = "morse_cadence_not_plausible"
    else:
        summary = f"NDB-band candidate, Morse-tone evidence weak ({tone_excess:.1f} dB)"
        reason = "weak_morse_tone"
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "method": "NDB Morse audio tone and on/off keying",
        "evidence_summary": summary,
        "status_reason": reason,
        "morse_tone_excess_db": round(tone_excess, 1),
        "keying_score": round(keying, 3),
        "cadence_plausible": cadence_plausible,
    }


def _confirm_amplitude_modulation(baseband: np.ndarray, sample_rate_sps: int) -> dict[str, Any]:
    envelope = np.abs(np.asarray(baseband, dtype=np.complex64)).astype(np.float32)
    if envelope.size < 256:
        return {"verdict": "candidate", "confidence": 0.0}
    envelope_mean = float(np.mean(envelope))
    envelope_ac = envelope - envelope_mean
    modulation_pct = 0.0
    if envelope_mean > 1e-9:
        modulation_pct = float(np.clip((float(np.sqrt(np.mean(envelope_ac * envelope_ac))) / envelope_mean) * np.sqrt(2.0) * 100.0, 0.0, 250.0))
    freqs, power_db = _envelope_spectrum(baseband, sample_rate_sps, decim=20, max_hz=5000.0)
    if freqs.size == 0:
        confidence = float(np.clip(modulation_pct / 20.0, 0.0, 1.0))
        verdict = "confirmed" if confidence >= 0.55 else "candidate"
        return {
            "verdict": verdict,
            "confidence": round(confidence, 3),
            "method": "AM envelope modulation depth",
            "evidence_summary": (
                f"AM envelope modulation present, modulation depth {modulation_pct:.1f}%"
                if verdict == "confirmed"
                else f"AM candidate, modulation depth weak ({modulation_pct:.1f}%)"
            ),
            "status_reason": "envelope_modulation_detected" if verdict == "confirmed" else "weak_envelope_modulation",
            "modulation_pct": round(modulation_pct, 1),
        }
    voice = _median_db(freqs, power_db, 100.0, 4500.0)
    low = _median_db(freqs, power_db, 5.0, 80.0)
    audio_excess = max(0.0, voice - low)
    confidence = float(max(np.clip(audio_excess / 12.0, 0.0, 1.0), np.clip(modulation_pct / 20.0, 0.0, 1.0)))
    verdict = "confirmed" if confidence >= 0.55 else "candidate"
    summary = (
        f"AM envelope modulation present, modulation depth {modulation_pct:.1f}%"
        if verdict == "confirmed"
        else f"AM candidate, envelope/audio evidence weak ({modulation_pct:.1f}% modulation)"
    )
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "method": "AM envelope modulation and audio energy",
        "evidence_summary": summary,
        "status_reason": "envelope_modulation_detected" if verdict == "confirmed" else "weak_envelope_or_audio",
        "modulation_pct": round(modulation_pct, 1),
        "audio_excess_db": round(audio_excess, 1),
    }


def _confirm_continuous_wave(baseband: np.ndarray, sample_rate_sps: int) -> dict[str, Any]:
    freqs, power_db = _iq_spectrum(baseband, sample_rate_sps, span_hz=2500.0)
    if freqs.size == 0:
        return {"verdict": "candidate", "confidence": 0.0}
    peak = float(np.max(power_db))
    median = float(np.median(power_db))
    occupied_hz = _occupied_width_hz(freqs, power_db, peak - 12.0)
    narrow_score = float(np.clip((400.0 - occupied_hz) / 400.0, 0.0, 1.0))
    confidence = float(np.clip(((peak - median) / 28.0) * 0.65 + narrow_score * 0.35, 0.0, 1.0))
    verdict = "confirmed" if confidence >= 0.65 else "candidate"
    summary = (
        f"Continuous-wave narrow carrier present, occupied width {occupied_hz:.1f} Hz"
        if verdict == "confirmed"
        else f"Continuous-wave candidate, carrier not narrow/strong enough ({occupied_hz:.1f} Hz)"
    )
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "method": "narrow carrier spectral shape",
        "evidence_summary": summary,
        "status_reason": "narrow_carrier_detected" if verdict == "confirmed" else "weak_or_broad_carrier",
        "occupied_width_hz": round(occupied_hz, 1),
        "peak_excess_db": round(peak - median, 1),
    }


def _confirm_single_sideband(baseband: np.ndarray, sample_rate_sps: int) -> dict[str, Any]:
    freqs, power_db = _iq_spectrum(baseband, sample_rate_sps, span_hz=5000.0)
    if freqs.size == 0:
        return {"verdict": "candidate", "confidence": 0.0}
    upper = _band_sum_db(freqs, power_db, 300.0, 3000.0)
    lower = _band_sum_db(freqs, power_db, -3000.0, -300.0)
    carrier = _band_peak_db(freqs, power_db, -75.0, 75.0)
    sideband_delta = abs(upper - lower)
    sideband_power = max(upper, lower)
    local = _median_db(freqs, power_db, -5000.0, 5000.0)
    confidence = float(np.clip((sideband_power - local) / 20.0, 0.0, 1.0) * np.clip(sideband_delta / 10.0, 0.0, 1.0))
    if carrier > sideband_power - 3.0:
        confidence *= 0.4
    verdict = "confirmed" if confidence >= 0.65 else "candidate"
    summary = (
        "Single-sideband asymmetric voice-band energy present with suppressed carrier"
        if verdict == "confirmed"
        else "Single-sideband candidate, asymmetric voice-band/suppressed-carrier evidence weak"
    )
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "method": "single-sideband asymmetric voice-band energy",
        "evidence_summary": summary,
        "status_reason": "asymmetric_sideband_detected" if verdict == "confirmed" else "weak_sideband_evidence",
        "upper_sideband_db": round(upper, 1),
        "lower_sideband_db": round(lower, 1),
        "carrier_peak_db": round(carrier, 1),
    }


def _envelope_spectrum(iq: np.ndarray, sample_rate_sps: int, *, decim: int, max_hz: float) -> tuple[np.ndarray, np.ndarray]:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size < 1024:
        return np.empty(0), np.empty(0)
    envelope = np.abs(samples).astype(np.float32)
    envelope = envelope - float(np.mean(envelope))
    return _real_spectrum(envelope[::max(1, decim)], sample_rate_sps / float(max(1, decim)), max_hz)


def _fm_audio_spectrum(iq: np.ndarray, sample_rate_sps: int, *, decim: int, max_hz: float) -> tuple[np.ndarray, np.ndarray]:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size < 1024:
        return np.empty(0), np.empty(0)
    phase = np.angle(samples[1:] * np.conj(samples[:-1])).astype(np.float32)
    phase = phase - float(np.mean(phase))
    return _real_spectrum(phase[::max(1, decim)], sample_rate_sps / float(max(1, decim)), max_hz)


def _real_spectrum(values: np.ndarray, sample_rate_sps: float, max_hz: float) -> tuple[np.ndarray, np.ndarray]:
    if values.size < 256:
        return np.empty(0), np.empty(0)
    nfft = min(65536, 1 << int(np.floor(np.log2(values.size))))
    if nfft < 256:
        return np.empty(0), np.empty(0)
    work = values[-nfft:].astype(np.float32, copy=False)
    work = work - float(np.mean(work))
    window = np.hanning(nfft).astype(np.float32)
    spectrum = np.fft.rfft(work * window)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate_sps))
    power = 20.0 * np.log10(np.abs(spectrum) / max(1.0, float(nfft)) + 1e-12)
    mask = freqs <= float(max_hz)
    return freqs[mask], power[mask]


def _iq_spectrum(iq: np.ndarray, sample_rate_sps: int, *, span_hz: float) -> tuple[np.ndarray, np.ndarray]:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size < 1024:
        return np.empty(0), np.empty(0)
    nfft = min(131072, 1 << int(np.floor(np.log2(samples.size))))
    if nfft < 1024:
        return np.empty(0), np.empty(0)
    work = samples[-nfft:].astype(np.complex64, copy=False)
    work = work - np.mean(work)
    window = np.hanning(nfft).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(work * window))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / float(sample_rate_sps)))
    power = 20.0 * np.log10(np.abs(spectrum) / max(1.0, float(nfft)) + 1e-12)
    mask = np.abs(freqs) <= float(span_hz)
    return freqs[mask], power[mask]


def _band_peak_db(freqs: np.ndarray, power_db: np.ndarray, low_hz: float, high_hz: float) -> float:
    mask = (freqs >= float(low_hz)) & (freqs <= float(high_hz))
    return float(np.max(power_db[mask])) if np.any(mask) else -240.0


def _band_sum_db(freqs: np.ndarray, power_db: np.ndarray, low_hz: float, high_hz: float) -> float:
    mask = (freqs >= float(low_hz)) & (freqs <= float(high_hz))
    if not np.any(mask):
        return -240.0
    linear = np.sum(10.0 ** (power_db[mask] / 10.0))
    return float(10.0 * np.log10(max(linear, 1e-24)))


def _median_db(freqs: np.ndarray, power_db: np.ndarray, low_hz: float, high_hz: float) -> float:
    mask = (freqs >= float(low_hz)) & (freqs <= float(high_hz))
    return float(np.median(power_db[mask])) if np.any(mask) else float(np.median(power_db)) if power_db.size else -240.0


def _occupied_width_hz(freqs: np.ndarray, power_db: np.ndarray, threshold_db: float) -> float:
    mask = power_db >= float(threshold_db)
    if not np.any(mask):
        return 0.0
    selected = freqs[mask]
    return float(np.max(selected) - np.min(selected))


def _keying_score(iq: np.ndarray, sample_rate_sps: int) -> float:
    samples = np.asarray(iq, dtype=np.complex64)
    if samples.size < 1024:
        return 0.0
    envelope = np.abs(samples).astype(np.float32)
    decim = max(1, int(sample_rate_sps // 200))
    slow = envelope[::decim]
    if slow.size < 20:
        return 0.0
    lo = float(np.percentile(slow, 20))
    hi = float(np.percentile(slow, 80))
    if hi <= lo * 1.05:
        return 0.0
    threshold = (lo + hi) * 0.5
    keyed = slow > threshold
    transitions = int(np.count_nonzero(keyed[1:] != keyed[:-1]))
    transition_rate = transitions / max(1.0, slow.size / 200.0)
    return float(np.clip(transition_rate / 6.0, 0.0, 1.0))


def _classify_lfmf_signal(freq_hz: int, metrics: AMMetrics, excess_db: float) -> tuple[str, str, int]:
    freq = int(freq_hz)
    modulation = float(metrics.modulation_pct)
    carrier_snr = float(metrics.carrier_snr_db)
    audio_dbfs = float(metrics.audio_dbfs)
    has_carrier = carrier_snr >= 6.0 and excess_db >= 4.0
    has_audio = audio_dbfs > -75.0 and modulation >= 2.0

    if abs(freq - 518_000) <= 1_250 or abs(freq - 490_000) <= 1_250:
        return "NAVTEX", "Maritime NAVTEX channel candidate at 490/518 kHz", 2
    if 190_000 <= freq <= 535_000:
        if has_audio:
            return "Non-Directional Beacon", "Aviation non-directional beacon candidate: LF/MF beacon range with keyed/audio energy", 1
        return "Non-Directional Beacon", "Aviation non-directional beacon range carrier candidate", 1
    if 530_000 <= freq <= 1_700_000:
        if modulation >= 6.0:
            return "Amplitude Modulation", "Medium-wave AM broadcast candidate with envelope modulation", 4
        if has_carrier:
            return "Continuous Wave", "Narrow carrier in medium-wave range; possible continuous-wave signal or carrier spur", 6
        return "Amplitude Modulation", "Medium-wave AM broadcast range candidate", 4
    if 10_000 <= freq <= 30_000:
        return "Continuous Wave", "VLF/LF narrow carrier candidate; classify presence/cadence rather than content", 3
    if has_carrier and modulation < 2.5:
        return "Continuous Wave", "Narrow carrier / continuous-wave candidate", 6
    if has_audio and not has_carrier:
        return "Single-sideband", "Voice-band energy without a strong carrier; possible single-sideband signal", 6
    if has_audio:
        return "Amplitude Modulation", "Amplitude/envelope-modulated LF/MF candidate", 5
    if freq < 1_000_000:
        return "Continuous Wave", "Low-frequency narrow emitter candidate", 6
    return "Single-sideband", "MF/HF voice/narrowband candidate", 6


def _print_results(
    rows: list[ScanResult],
    args: argparse.Namespace,
    noise_floor: float,
    receiver_config: ReceiverConfig,
    scan_plan: ScanPlan,
) -> None:
    if args.jsonl:
        for row in rows:
            if args.active_only and not row.active:
                continue
            payload = asdict(row)
            payload.update(
                {
                    "rf_protocol": row.protocol,
                    "kind": "lfmf_signal",
                    "band": scan_plan.band,
                    "band_label": scan_plan.label,
                    "frequency_hz": row.freq_hz,
                    "frequency_khz": row.freq_khz,
                    "sample_rate_sps": receiver_config.sample_rate_sps,
                    "bandwidth_hz": receiver_config.bandwidth_hz,
                    "tune_offset_hz": receiver_config.tune_offset_hz,
                    "timestamp": time.time(),
                }
            )
            print(json.dumps(payload, separators=(",", ":")), flush=True)
        return
    if args.json:
        print(
            json.dumps(
                {
                    "noise_floor_dbfs": round(noise_floor, 1),
                    "receiver": asdict(receiver_config),
                    "scan_plan": asdict(scan_plan),
                    "signals": [asdict(row) for row in rows],
                },
                indent=2,
            )
        )
        return
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(asdict(rows[0]).keys()) if rows else list(ScanResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
        return
    print(
        f"{scan_plan.label} scan  range={scan_plan.start_khz}-{scan_plan.stop_khz} kHz  step={scan_plan.step_khz} kHz  "
        f"sample_rate={receiver_config.sample_rate_sps} sps  bandwidth={receiver_config.bandwidth_hz} Hz  "
        f"carrier_floor={noise_floor:.1f} dBFS  threshold=+{float(args.active_threshold_db):.1f} dB  "
        f"tune_offset={receiver_config.tune_offset_hz} Hz"
    )
    print("freq_khz  status  excess  protocol      car_dbfs  car_snr  audio_dbfs  mod_pct  samples")
    for row in rows:
        status = "ACTIVE" if row.active else "-"
        print(
            f"{row.freq_khz:8.1f}  {status:6s}  {row.excess_db:6.1f}  {row.protocol:12s}  "
            f"{row.carrier_dbfs:8.1f}  {row.carrier_snr_db:7.1f}  "
            f"{row.audio_dbfs:9.1f}  {row.modulation_pct:7.1f}  {row.samples}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan VLF, LF, MF, and AM broadcast signals with an SDRplay receiver.")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="Scan VLF/LF/MF signal bands")
    scan.add_argument("--backend", choices=["gateway", "direct"], default="gateway", help="Receiver backend. Gateway keeps SDR ownership centralized.")
    scan.add_argument("--gateway-base-url", default="", help="sdr-gateway base URL. Defaults to SDR_GATEWAY_BASE_URL or http://127.0.0.1:8080.")
    scan.add_argument("--gateway-token", default="", help="sdr-gateway bearer token. Defaults to SDR_GATEWAY_API_TOKEN.")
    scan.add_argument("--replace-existing", action="store_true", help="Stop an existing gateway stream for this device if needed.")
    scan.add_argument("--device-id", default="sdrplay:0", help="Gateway device id. Defaults to sdrplay:0.")
    scan.add_argument("--serial", default="", help="SDRplay serial number. Defaults to SDRPLAY_SERIAL when set.")
    scan.add_argument("--antenna", default="", help="Optional SDRplay antenna name, for example A, B, or Hi-Z.")
    scan.add_argument("--band", choices=sorted(BAND_PRESETS), default=DEFAULT_BAND, help="Band preset to scan.")
    scan.add_argument("--start-khz", type=int, default=None, help=f"Override preset start frequency. Default for AM is {DEFAULT_START_KHZ}.")
    scan.add_argument("--stop-khz", type=int, default=None, help=f"Override preset stop frequency. Default for AM is {DEFAULT_STOP_KHZ}.")
    scan.add_argument("--step-khz", type=int, default=None, help=f"Override preset channel step. Default for AM is {DEFAULT_STEP_KHZ}.")
    scan.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    scan.add_argument("--bandwidth-hz", type=int, default=DEFAULT_BANDWIDTH_HZ)
    scan.add_argument("--tune-offset-hz", type=int, default=25_000, help="Tune this far above each channel so the AM carrier avoids DC.")
    scan.add_argument(
        "--upconverter-offset-hz",
        type=int,
        default=0,
        help="Add this fixed frequency offset to SDR tuning while keeping reported signal frequencies unchanged.",
    )
    scan.add_argument("--carrier-width-hz", type=float, default=350.0, help="Narrow FFT window used to score the AM carrier.")
    scan.add_argument("--wideband", dest="wideband", action="store_true", help="Capture one wide IQ buffer and score all requested LF/MF channels from it.")
    scan.add_argument(
        "--wideband-center-khz",
        dest="wideband_center_khz",
        type=int,
        default=0,
        help="Center frequency for --wideband. 0 uses the middle of the scan range.",
    )
    scan.add_argument(
        "--wideband-sample-rate-sps",
        dest="wideband_sample_rate_sps",
        type=int,
        default=1_000_000,
        help="Sample rate for --wideband capture.",
    )
    scan.add_argument(
        "--wideband-bandwidth-hz",
        dest="wideband_bandwidth_hz",
        type=int,
        default=1_000_000,
        help="Baseband filter for --wideband capture.",
    )
    scan.add_argument("--cluster-hz", type=int, default=20_000, help="Collapse active detections closer than this into the strongest local peak. 0 disables.")
    scan.add_argument("--confirm", action="store_true", help="Run protocol-specific confirmation features for wideband candidates.")
    scan.add_argument("--no-confirm-retune", dest="confirm_retune", action="store_false", help="With --confirm, use the wideband capture instead of retuning each candidate.")
    scan.add_argument("--confirm-sample-rate-sps", type=int, default=250_000, help="Sample rate for candidate confirmation retunes.")
    scan.add_argument("--confirm-bandwidth-hz", type=int, default=80_000, help="Baseband filter for candidate confirmation retunes.")
    scan.add_argument("--confirm-tune-offset-hz", type=int, default=25_000, help="Tune this far above candidates during confirmation to avoid DC.")
    scan.add_argument("--confirm-dwell-s", type=float, default=0.35, help="Capture dwell per candidate during confirmation.")
    scan.add_argument("--confirm-max-candidates", type=int, default=8, help="Maximum active candidates to retune and confirm per scan.")
    scan.add_argument("--dwell-s", type=float, default=0.20)
    scan.add_argument("--settle-s", type=float, default=0.04)
    scan.add_argument("--timeout-ms", type=int, default=250)
    scan.add_argument("--rfgr", type=float, default=0.0, help="SDRplay RF gain reduction. Lower usually means more RF gain.")
    scan.add_argument("--ifgr", type=float, default=35.0, help="SDRplay IF gain reduction. Lower usually means more IF gain.")
    scan.add_argument("--active-threshold-db", type=float, default=6.0)
    scan.add_argument("--top", type=int, default=0, help="Only show the top N rows after sorting. 0 shows all.")
    scan.add_argument("--sort", choices=["score", "freq"], default="score")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--jsonl", action="store_true", help="Emit one compact JSON event per scanned signal.")
    scan.add_argument("--active-only", action="store_true", help="With --jsonl, only emit rows above the active threshold.")
    scan.add_argument("--csv", action="store_true")
    scan.add_argument("--debug", action="store_true")
    scan.add_argument("--show-driver-log", action="store_true", help="Show native SDRplay/SoapySDR startup messages.")
    scan.add_argument("--yes", action="store_true", help="Allow long scans with more than 400 channels.")
    scan.set_defaults(func=_scan)
    record = sub.add_parser("record", help="Record low-frequency IQ through sdr-gateway")
    record.add_argument("--backend", choices=["gateway"], default="gateway")
    record.add_argument("--gateway-base-url", default="", help="sdr-gateway base URL. Defaults to SDR_GATEWAY_BASE_URL or http://127.0.0.1:8080.")
    record.add_argument("--gateway-token", default="", help="sdr-gateway bearer token. Defaults to SDR_GATEWAY_API_TOKEN.")
    record.add_argument("--replace-existing", action="store_true", help="Stop an existing gateway stream for this device if needed.")
    record.add_argument("--device-id", default="sdrplay:0")
    record.add_argument("--center-khz", type=int, default=500, help="Target center frequency before tune offset.")
    record.add_argument("--sample-rate-sps", type=int, default=1_000_000)
    record.add_argument("--bandwidth-hz", type=int, default=1_000_000)
    record.add_argument("--tune-offset-hz", type=int, default=0)
    record.add_argument("--seconds", type=float, default=60.0)
    record.add_argument("--rfgr", type=float, default=0.0)
    record.add_argument("--ifgr", type=float, default=35.0)
    record.add_argument("--output", default="recordings/lf_vlf_mf_sdrplay_500khz_1msps.i8")
    record.add_argument("--debug", action="store_true")
    record.set_defaults(func=_record)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        print(f"am-broadcast: {exc}", file=sys.stderr)
        return 1
