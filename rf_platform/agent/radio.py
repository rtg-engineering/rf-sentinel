"""IQ service client used by the agent: choose a receiver, measure levels, choose gain."""
from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any

import numpy as np
import requests

TARGET_PEAK_DBFS = -6.0      # 99.9th-percentile magnitude target
WINDOW_DB = 3.0              # accept target +/- this
MAX_CLIP_FRACTION = 0.001    # at most 0.1% of samples at full scale


@dataclass
class Levels:
    gain_db: float
    rms_dbfs: float
    p999_dbfs: float
    clip_fraction: float

    @property
    def saturated(self) -> bool:
        return self.clip_fraction > MAX_CLIP_FRACTION or self.p999_dbfs > TARGET_PEAK_DBFS + WINDOW_DB

    @property
    def weak(self) -> bool:
        return self.p999_dbfs < TARGET_PEAK_DBFS - WINDOW_DB

    def describe(self) -> str:
        return (f"gain {self.gain_db:.0f} dB: peak(99.9%) {self.p999_dbfs:.1f} dBFS, rms {self.rms_dbfs:.1f} dBFS, "
                f"clipped {self.clip_fraction * 100:.2f}%")


def iq_levels(raw: bytes, gain_db: float) -> Levels:
    iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
    if iq.size < 2:
        return Levels(gain_db, -120.0, -120.0, 0.0)
    mag = np.hypot(iq[0::2], iq[1::2]) / 128.0
    db = lambda x: float(20 * np.log10(max(float(x), 1e-6)))  # noqa: E731
    return Levels(gain_db, db(np.sqrt(np.mean(mag ** 2))), db(np.percentile(mag, 99.9)),
                  float(np.mean(np.abs(iq) >= 127)))


def split_gain(driver: str, total_db: float) -> tuple[int, int]:
    """Total gain -> (lna, vga) as the IQ service expects. Staged radios (HackRF) get both stages."""
    total = max(0, int(round(total_db)))
    if driver == "hackrf":
        lna = min(40, (max(0, total - 20) // 8) * 8)
        return lna, min(62, total - lna)
    return 0, min(62, total)


class Radio:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.getenv("SDR_GATEWAY_BASE_URL") or "http://127.0.0.1:8790").rstrip("/")
        self.token = token if token is not None else os.getenv("SDR_GATEWAY_API_TOKEN", "")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def devices(self) -> list[dict[str, Any]]:
        try:
            response = requests.get(f"{self.base_url}/devices", headers=self.headers, timeout=30)
        except requests.ConnectionError:
            raise RuntimeError(f"The IQ service at {self.base_url} is not running, so no receiver is available. "
                               "Start the dashboard outside demo mode (or rf-sentinel-soapy-gateway).") from None
        response.raise_for_status()
        return response.json()

    def choose(self, freqs_hz: tuple[int, ...], rate_sps: int, wanted: str | None = None,
               exclude: tuple[str, ...] = ()) -> dict[str, Any]:
        devices = [d for d in self.devices() if d["id"] not in exclude]
        if wanted:
            match = [d for d in devices if d["id"] == wanted]
            if not match:
                raise RuntimeError(f"{wanted} is not available. Receivers: {', '.join(d['id'] for d in devices) or 'none'}")
            devices = match
        fits = [d for d in devices if d["freq_min_hz"] <= min(freqs_hz) and max(freqs_hz) <= d["freq_max_hz"]
                and d["max_sample_rate_sps"] >= rate_sps]
        free = [d for d in fits if not d.get("occupied")]
        if not fits:
            raise RuntimeError("No attached receiver covers " + ", ".join(f"{f / 1e6:.1f} MHz" for f in freqs_hz)
                               + f". Receivers: {', '.join(d['id'] for d in devices) or 'none'}")
        if not free:
            raise RuntimeError("Every suitable receiver is busy: " + ", ".join(f"{d['id']} ({d.get('occupied_by')})" for d in fits))
        return free[0]

    def measure(self, device: dict[str, Any], freq_hz: int, rate_sps: int, gain_db: float,
                seconds: float = 0.4) -> Levels:
        import websocket
        lna, vga = split_gain(device["driver"], gain_db)
        body = {"device_id": device["id"], "center_freq_hz": int(freq_hz), "sample_rate_sps": int(rate_sps),
                "lna_gain_db": lna, "vga_gain_db": vga, "baseband_filter_hz": int(rate_sps // 2)}
        response = requests.post(f"{self.base_url}/streams/start", headers=self.headers, json=body, timeout=30)
        response.raise_for_status()
        stream_id = response.json()["stream_id"]
        ws_base = "ws://" + self.base_url.split("://", 1)[1]
        query = f"?keep=1{'&token=' + self.token if self.token else ''}"
        data = bytearray()
        failure = None
        try:
            ws = websocket.create_connection(f"{ws_base}/ws/iq/{stream_id}{query}", timeout=15)
            wanted = int(rate_sps * seconds) * 2
            deadline = time.monotonic() + 15
            try:
                while len(data) < wanted and time.monotonic() < deadline:
                    data.extend(ws.recv())
            except (websocket.WebSocketTimeoutException, websocket.WebSocketConnectionClosedException) as exc:
                failure = exc
            ws.close()
        finally:
            states = requests.get(f"{self.base_url}/streams", headers=self.headers, timeout=10).json()
            error = next((s.get("error") for s in states if s["stream_id"] == stream_id), None)
            requests.post(f"{self.base_url}/streams/{stream_id}/stop", headers=self.headers, timeout=10)
        if len(data) < 2048:
            raise RuntimeError(f"Receiver {device['id']} is not delivering samples"
                               + (f": {error}" if error else f" ({type(failure).__name__})" if failure else "")
                               + ". Check its USB connection, or choose another receiver.")
        return iq_levels(bytes(data), gain_db)


def capture_iq(radio: "Radio", device: dict[str, Any], freq_hz: int, rate_sps: int, gain_db: float,
               seconds: float, attempts: int = 2) -> bytes:
    """Receive `seconds` of CS8 IQ from one receiver (receive-only), then release the stream.

    A transport error (e.g. a malformed websocket frame) retries once on a fresh stream; a partial capture
    is never returned as if it were complete.
    """
    import websocket
    for attempt in range(attempts):
        try:
            data = _capture_once(radio, device, freq_hz, rate_sps, gain_db, seconds)
        except (websocket.WebSocketException, OSError):
            if attempt + 1 == attempts:
                raise
            continue
        if len(data) >= int(rate_sps * seconds) * 2 or attempt + 1 == attempts:
            if len(data) < int(rate_sps * seconds) * 2:
                raise RuntimeError(f"capture incomplete: {len(data) // 2} of {int(rate_sps * seconds)} samples")
            return data
    raise RuntimeError("capture failed")


def _capture_once(radio: "Radio", device: dict[str, Any], freq_hz: int, rate_sps: int, gain_db: float,
                  seconds: float) -> bytes:
    import websocket
    lna, vga = split_gain(device["driver"], gain_db)
    body = {"device_id": device["id"], "center_freq_hz": int(freq_hz), "sample_rate_sps": int(rate_sps),
            "lna_gain_db": lna, "vga_gain_db": vga, "baseband_filter_hz": int(rate_sps // 2)}
    response = requests.post(f"{radio.base_url}/streams/start", headers=radio.headers, json=body, timeout=30)
    response.raise_for_status()
    stream_id = response.json()["stream_id"]
    query = f"?keep=1{'&token=' + radio.token if radio.token else ''}"
    wanted = int(rate_sps * seconds) * 2
    data = bytearray()
    try:
        ws = websocket.create_connection(f"ws://{radio.base_url.split('://', 1)[1]}/ws/iq/{stream_id}{query}", timeout=15)
        deadline = time.monotonic() + seconds + 15
        while len(data) < wanted and time.monotonic() < deadline:
            data.extend(ws.recv())
        ws.close()
    finally:
        requests.post(f"{radio.base_url}/streams/{stream_id}/stop", headers=radio.headers, timeout=10)
    return bytes(data[:wanted])


def auto_gain(radio: Radio, device: dict[str, Any], freqs_hz: tuple[int, ...], rate_sps: int,
              start_db: float = 40.0, max_db: float = 60.0, min_db: float = 0.0, steps: int = 6, log=print,
              should_stop=lambda: False) -> float:
    """Find the highest gain that keeps peaks near the target without clipping on every listed center.

    Uses the loudest center: a gain that is safe there is safe everywhere.
    """
    gain = start_db
    for _ in range(steps):
        if should_stop():
            log(f"gain optimization stopped at {gain:.0f} dB")
            return gain
        measured = [radio.measure(device, f, rate_sps, gain) for f in freqs_hz]
        loudest = max(measured, key=lambda m: (m.clip_fraction, m.p999_dbfs))
        log(f"measured {', '.join(f'{f / 1e6:.0f} MHz' for f in freqs_hz)} at {loudest.describe()}")
        if loudest.saturated:
            step = -max(6.0, loudest.p999_dbfs - TARGET_PEAK_DBFS) if loudest.clip_fraction > MAX_CLIP_FRACTION \
                else TARGET_PEAK_DBFS - loudest.p999_dbfs
        elif loudest.weak:
            step = min(12.0, TARGET_PEAK_DBFS - loudest.p999_dbfs)
        else:
            log(f"gain set to {gain:.0f} dB (peaks within {WINDOW_DB:.0f} dB of {TARGET_PEAK_DBFS:.0f} dBFS)")
            return gain
        new_gain = float(min(max_db, max(min_db, round(gain + step))))
        if new_gain == gain:
            reason = "at maximum gain; signals are weak" if step > 0 else "at minimum gain; strong signals still clip"
            log(f"gain left at {gain:.0f} dB ({reason})")
            return gain
        log(f"{'saturated' if step < 0 else 'too weak'}: adjusting gain {gain:.0f} -> {new_gain:.0f} dB")
        gain = new_gain
    log(f"gain settled at {gain:.0f} dB after {steps} measurements")
    return gain
