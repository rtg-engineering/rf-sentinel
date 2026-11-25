"""Receive-only bladeRF access through libbladeRF directly, presenting the SoapySDR Device methods RxWorker uses.

Why: SoapyBladeRF always streams in timestamp-metadata mode. With bladeRF 2.0 FPGA/firmware newer than the
installed libbladeRF knows (seen with FPGA v0.16.0 / libbladeRF 2.5.0), every 508-sample metadata message is
followed by an overrun, so the stream silently loses samples every ~127 us at 4 MS/s. That corrupts most
packets longer than ~100 us (measured: ~95% of BLE advertising packets failed CRC). The plain sync interface
without metadata (SC16_Q11) delivers a continuous stream on the same hardware.

Disable with RF_SENTINEL_BLADERF_NATIVE=0 to go back to SoapyBladeRF. Only receive functions are bound.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Any

import numpy as np

_RX0 = 0                    # BLADERF_CHANNEL_RX(0)
_RX_X1 = 0                  # BLADERF_RX_X1
_FORMAT_SC16_Q11 = 0        # BLADERF_FORMAT_SC16_Q11 (no metadata)
_GAIN_MGC = 1               # BLADERF_GAIN_MGC (manual)
_ERR_TIMEOUT = -6           # BLADERF_ERR_TIMEOUT
_SOAPY_TIMEOUT = -1         # SOAPY_SDR_TIMEOUT
_READ_SAMPLES = 16_384
_SC16_FULL_SCALE = 2048.0

_lib: Any = None


class _Range(ctypes.Structure):
    _fields_ = [("min", ctypes.c_int64), ("max", ctypes.c_int64), ("step", ctypes.c_int64), ("scale", ctypes.c_float)]


class Range:
    """SoapySDR.Range look-alike."""

    def __init__(self, lo: float, hi: float):
        self._lo, self._hi = lo, hi

    def minimum(self) -> float:
        return self._lo

    def maximum(self) -> float:
        return self._hi


class ReadResult:
    def __init__(self, ret: int):
        self.ret = ret


def enabled() -> bool:
    return os.getenv("RF_SENTINEL_BLADERF_NATIVE", "1").strip().lower() not in ("0", "false", "no", "off")


def load() -> Any:
    """The libbladeRF shared library, or None when it is not installed."""
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("bladeRF") or "libbladeRF.so.2"
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            _lib = False
            return None
        lib.bladerf_strerror.restype = ctypes.c_char_p
        lib.bladerf_set_frequency.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64]
        lib.bladerf_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p]
        lib.bladerf_set_bandwidth.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p]
        lib.bladerf_set_gain.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.bladerf_sync_rx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        _lib = lib
    return _lib or None


class NativeBladeRF:
    """One open bladeRF. Construct only for driver == 'bladerf'; raises RuntimeError if it cannot be opened."""

    def __init__(self, kwargs: dict[str, str]):
        self._lib = load()
        if self._lib is None:
            raise RuntimeError("libbladeRF is not installed")
        self._dev = ctypes.c_void_p()
        serial = kwargs.get("serial")
        ident = f"*:serial={serial}" if serial else ""
        self._check(self._lib.bladerf_open(ctypes.byref(self._dev), ident.encode()), "open")
        self._scratch = np.zeros(2 * _READ_SAMPLES, dtype=np.int16)
        self._streaming = False

    def _check(self, code: int, what: str) -> None:
        if code != 0:
            raise RuntimeError(f"bladerf {what} failed: {code} {self._lib.bladerf_strerror(code).decode()}")

    def _range(self, fn: str) -> list[Range]:
        ptr = ctypes.POINTER(_Range)()
        self._check(getattr(self._lib, fn)(self._dev, _RX0, ctypes.byref(ptr)), fn)
        r = ptr.contents
        return [Range(r.min * r.scale, r.max * r.scale)]

    # Configuration (direction/channel arguments are accepted for API compatibility; RX0 is used)
    def getFrequencyRange(self, *_: Any) -> list[Range]:
        return self._range("bladerf_get_frequency_range")

    def getSampleRateRange(self, *_: Any) -> list[Range]:
        return self._range("bladerf_get_sample_rate_range")

    def getGainRange(self, *_: Any) -> Range:
        return self._range("bladerf_get_gain_range")[0]

    def setSampleRate(self, _direction: int, _channel: int, rate: float) -> None:
        actual = ctypes.c_uint32()
        self._check(self._lib.bladerf_set_sample_rate(self._dev, _RX0, int(rate), ctypes.byref(actual)), "set_sample_rate")

    def setFrequency(self, _direction: int, _channel: int, freq: float) -> None:
        self._check(self._lib.bladerf_set_frequency(self._dev, _RX0, int(freq)), "set_frequency")

    def setBandwidth(self, _direction: int, _channel: int, bandwidth: float) -> None:
        actual = ctypes.c_uint32()
        self._check(self._lib.bladerf_set_bandwidth(self._dev, _RX0, int(bandwidth), ctypes.byref(actual)), "set_bandwidth")

    def hasGainMode(self, *_: Any) -> bool:
        return True

    def setGainMode(self, _direction: int, _channel: int, automatic: bool) -> None:
        if not automatic:
            self._check(self._lib.bladerf_set_gain_mode(self._dev, _RX0, _GAIN_MGC), "set_gain_mode")

    def listGains(self, *_: Any) -> list[str]:
        return []  # one aggregate gain, as SoapyBladeRF exposes for bladeRF 2.0

    def setGain(self, _direction: int, _channel: int, *args: Any) -> None:
        value = args[-1]  # setGain(dir, ch, value) or setGain(dir, ch, name, value)
        self._check(self._lib.bladerf_set_gain(self._dev, _RX0, int(round(float(value)))), "set_gain")

    # Streaming
    def setupStream(self, *_: Any) -> str:
        self._check(self._lib.bladerf_sync_config(self._dev, _RX_X1, _FORMAT_SC16_Q11, 32, _READ_SAMPLES, 16, 3500),
                    "sync_config")
        return "rx0"

    def activateStream(self, _stream: Any) -> None:
        self._check(self._lib.bladerf_enable_module(self._dev, _RX0, True), "enable_module")
        self._streaming = True

    def readStream(self, _stream: Any, buffers: list[np.ndarray], num: int, timeoutUs: int = 500_000) -> ReadResult:
        out = buffers[0]
        n = int(min(num, _READ_SAMPLES, out.size))
        if n <= 0:
            return ReadResult(0)
        code = self._lib.bladerf_sync_rx(self._dev, self._scratch.ctypes.data_as(ctypes.c_void_p), n, None,
                                         max(1, int(timeoutUs) // 1000))
        if code != 0:
            return ReadResult(_SOAPY_TIMEOUT if code == _ERR_TIMEOUT else int(code))
        iq = self._scratch[: 2 * n].astype(np.float32) / _SC16_FULL_SCALE
        out[:n] = iq[0::2] + 1j * iq[1::2]
        return ReadResult(n)

    def deactivateStream(self, _stream: Any) -> None:
        if self._streaming:
            self._lib.bladerf_enable_module(self._dev, _RX0, False)
            self._streaming = False

    def closeStream(self, _stream: Any) -> None:
        pass

    def close(self) -> None:
        if self._dev:
            self.deactivateStream(None)
            self._lib.bladerf_close(self._dev)
            self._dev = ctypes.c_void_p()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
