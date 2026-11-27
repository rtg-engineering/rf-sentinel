"""A detector-side receiver that reads through RF Sentinel's IQ service instead of opening the radio.

Same interface as the detector's own SoapySDR source: constructed with the radio settings, then
set_frequency(hz) / read_iq(samples) -> float32 array of shape (samples, 2) / close().
Each read_iq is one POST /streams/<id>/capture: the service tunes (when the frequency changed), drops the settle
samples still in flight from the old frequency, and returns one contiguous block, so a hop is one round trip.

Device args select it: "driver=rfsentinel,device_id=bladerf:1[,settle_us=8000]". The service URL and token come
from DRONE_RF_BASE_URL / DRONE_RF_TOKEN (falling back to SDR_GATEWAY_BASE_URL / SDR_GATEWAY_API_TOKEN).
Samples arrive as CS8 (8-bit I/Q, about 48 dB of dynamic range), scaled to full scale 1.0 like CF32.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np

DRIVER = "rfsentinel"
DEFAULT_SETTLE_US = 8_000.0


def parse_device_args(value: str) -> dict[str, str]:
    out = {}
    for part in str(value or "").split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def is_rfsentinel(device_args: str) -> bool:
    return parse_device_args(device_args).get("driver", "").lower() == DRIVER


def split_gain(total_db: float) -> tuple[float, float]:
    """The IQ service sets bladeRF's single aggregate gain from lna + vga: keep it all in vga."""
    return 0.0, float(total_db)


class RfSentinelIqSource:
    def __init__(self, *, device_args: str, channel: int = 0, sample_rate: float, freq: float, gain: float | None = None,
                 agc: bool = False, antenna: str | None = None, bandwidth: float | None = None,
                 stream_args: str | None = None, session: Any = None, base_url: str | None = None,
                 token: str | None = None) -> None:
        args = parse_device_args(device_args)
        self.device_id = args.get("device_id") or args.get("device") or ""
        if not self.device_id:
            raise ValueError("rfsentinel device args need device_id=<receiver id>, e.g. device_id=bladerf:1")
        self.base_url = (base_url or os.getenv("DRONE_RF_BASE_URL") or os.getenv("SDR_GATEWAY_BASE_URL")
                         or "http://127.0.0.1:8790").rstrip("/")
        token = token if token is not None else (os.getenv("DRONE_RF_TOKEN") or os.getenv("SDR_GATEWAY_API_TOKEN") or "")
        if session is None:
            import requests
            session = requests.Session()
        self.http = session
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.settle_us = float(args.get("settle_us") or DEFAULT_SETTLE_US)
        self.sample_rate = float(sample_rate)
        self.center_hz = int(freq)
        lna, vga = split_gain(gain if gain is not None else 30.0)
        body = {"device_id": self.device_id, "center_freq_hz": self.center_hz, "sample_rate_sps": int(sample_rate),
                "lna_gain_db": lna, "vga_gain_db": vga}
        if bandwidth:
            body["baseband_filter_hz"] = int(bandwidth)
        state = self._post("/streams/start", body).json()
        self.stream_id = state["stream_id"]
        self.closed = False

    def _post(self, path: str, body: dict[str, Any]):
        response = self.http.post(f"{self.base_url}{path}", json=body, headers=self.headers, timeout=10)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(f"IQ service {path}: HTTP {response.status_code}: {detail}")
        return response

    def set_frequency(self, freq: float) -> None:
        self.center_hz = int(freq)  # applied by the next read, so tune + settle + read is one request

    def read_iq(self, samples: int) -> np.ndarray:
        samples = int(samples)
        response = self._post(f"/streams/{self.stream_id}/capture",
                              {"samples": samples, "center_freq_hz": self.center_hz, "settle_us": self.settle_us})
        raw = np.frombuffer(response.content, dtype=np.int8)
        iq = np.zeros((samples, 2), dtype=np.float32)
        n = min(samples, raw.size // 2)
        iq[:n] = raw[: n * 2].reshape(n, 2).astype(np.float32) / 127.0
        return iq

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._post(f"/streams/{self.stream_id}/stop", {})
        except Exception:
            pass  # the service's idle reaper releases the receiver anyway
