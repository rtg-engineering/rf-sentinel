"""SoapySDR receive backend for RF Sentinel's built-in IQ service.

Any SoapySDR-supported receiver works without driver-specific code. Samples are read as CF32 and
delivered as interleaved signed 8-bit I/Q (CS8), the framing every RF Sentinel receiver plugin decodes.
All SoapySDR calls for a device happen on that device's own worker thread.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import glob
import math
import os
import sys
import threading
import time
from typing import Any
import uuid

import numpy as np

from . import bladerf_native

# Fallback ranges when a device cannot be opened to query them (e.g. it is in use elsewhere).
KNOWN_DRIVERS = {
    "hackrf": (1_000_000, 6_000_000_000, 20_000_000),
    "bladerf": (47_000_000, 6_000_000_000, 61_440_000),
    "rtlsdr": (24_000_000, 1_766_000_000, 3_200_000),
    "airspy": (24_000_000, 1_800_000_000, 10_000_000),
    "sdrplay": (1_000, 2_000_000_000, 10_000_000),
    "lime": (100_000, 3_800_000_000, 61_440_000),
    "uhd": (70_000_000, 6_000_000_000, 61_440_000),
    "plutosdr": (70_000_000, 6_000_000_000, 61_440_000),
}
NON_RF_DRIVERS = {"audio"}  # sound cards exposed by SoapyAudio are not receivers
CHUNK_SAMPLES = 131_072  # samples per published chunk (filled from several driver reads)
RING_CHUNKS = 64
MAX_CAPTURE_SAMPLES = 4_194_304  # one capture block: 4 Mi samples (8 MB CS8), ~0.2 s at 20 Msps
SOAPY_SDR_OVERFLOW = -4
TRANSIENT_READ_ERRORS = {-1, SOAPY_SDR_OVERFLOW}  # SOAPY_SDR_TIMEOUT, SOAPY_SDR_OVERFLOW
NO_DATA_TIMEOUT_S = 5.0           # a running stream that delivers nothing this long has failed


class DeviceBusy(RuntimeError):
    pass


def load_soapysdr() -> Any:
    """Import SoapySDR, falling back to system site-packages when running inside a virtualenv."""
    try:
        import SoapySDR  # type: ignore[import-not-found]
        return SoapySDR
    except ImportError:
        pass
    candidates = ["/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages",
                  *glob.glob("/usr/local/lib/python3*/site-packages"), *glob.glob("/usr/lib/python3*/site-packages")]
    for path in candidates:
        if path not in sys.path:
            sys.path.append(path)
        try:
            import SoapySDR  # type: ignore[import-not-found]
            return SoapySDR
        except ImportError:
            continue
    raise RuntimeError("SoapySDR Python bindings are not importable. Install python3-soapysdr "
                       "(or build SoapySDR with Python support).")


# Driver full scale maps to +/-127 in int8 for maximum resolution. AGC is disabled, so the gain is set
# explicitly; rf_platform.agent's automatic gain control keeps peaks just below clipping.
CS8_FULL_SCALE = float(os.getenv("RF_SENTINEL_SOAPY_CS8_FULL_SCALE", "127"))


def cf32_to_cs8(samples: np.ndarray, full_scale: float = CS8_FULL_SCALE) -> bytes:
    """CF32 (driver full scale = 1.0) -> interleaved int8 I/Q."""
    interleaved = np.empty(samples.size * 2, dtype=np.float32)
    interleaved[0::2] = samples.real
    interleaved[1::2] = samples.imag
    return np.clip(np.round(interleaved * full_scale), -128, 127).astype(np.int8).tobytes()


@dataclass
class Device:
    id: str
    driver: str
    label: str
    serial: str | None
    kwargs: dict[str, str]
    freq_min_hz: int
    freq_max_hz: int
    max_sample_rate_sps: int
    notes: str

    def info(self, owner: tuple[str, str] | None) -> dict[str, Any]:
        return {"id": self.id, "driver": self.driver, "label": self.label, "serial": self.serial,
                "freq_min_hz": self.freq_min_hz, "freq_max_hz": self.freq_max_hz,
                "max_sample_rate_sps": self.max_sample_rate_sps, "notes": self.notes,
                "occupied": owner is not None, "occupied_by": owner[0] if owner else None,
                "occupied_id": owner[1] if owner else None}


def _clip(dev: Any, rx: int, name: str | None, value: float) -> float:
    try:
        r = dev.getGainRange(rx, 0, name) if name else dev.getGainRange(rx, 0)
        return float(min(max(value, r.minimum()), r.maximum()))
    except Exception:
        return float(value)


def _apply_gains(soapy: Any, dev: Any, driver: str, lna_db: float, vga_db: float, amp: bool) -> None:
    """Gateway-compatible mapping of (lna, vga, amp) onto each driver's gain controls, with AGC off."""
    rx = soapy.SOAPY_SDR_RX
    try:
        if dev.hasGainMode(rx, 0):
            dev.setGainMode(rx, 0, False)  # manual gain: AGC would override the requested levels
    except Exception:
        pass
    names = {n.lower(): n for n in dev.listGains(rx, 0)}

    def set_named(key: str, value: float) -> bool:
        if key in names:
            dev.setGain(rx, 0, names[key], _clip(dev, rx, names[key], value))
            return True
        return False

    total = float(lna_db + vga_db)
    if {"lna", "vga"} <= set(names):  # HackRF-style staged gains
        set_named("lna", lna_db)
        set_named("vga", vga_db)
        set_named("amp", 14.0 if amp else 0.0)
        return
    if driver == "rtlsdr" and (set_named("tuner", total) or set_named("lna", total)):
        return
    if driver == "airspy":
        mix = min(max(0.0, float(vga_db)), 15.0)
        if any([set_named("lna", lna_db), set_named("mix", mix), set_named("vga", max(0.0, vga_db - mix))]):
            return
    if driver == "sdrplay" and any([set_named("rfgr", lna_db), set_named("ifgr", vga_db)]):
        return
    try:
        dev.setGain(rx, 0, _clip(dev, rx, None, total))  # aggregate gain (bladeRF 2.0 exposes only "full")
    except Exception:
        pass


class RxWorker:
    """One receive stream on one device, running on its own thread; CS8 chunks go into a ring buffer."""

    def __init__(self, soapy: Any, device: Device, center_hz: int, rate_sps: int, lna_db: float = 16,
                 vga_db: float = 20, amp: bool = False, bandwidth_hz: int | None = None,
                 max_samples: int | None = None, opener=None, on_failure=None, on_success=None):
        self.soapy, self.device = soapy, device
        # Opening some devices (e.g. bladeRF) takes seconds; the backend reuses an open handle.
        self._open = opener or (lambda: soapy.Device(device.kwargs))
        self._on_failure = on_failure
        self._on_success = on_success
        self.center_hz, self.rate_sps = int(center_hz), int(rate_sps)
        self.gains = (lna_db, vga_db, amp)
        self.bandwidth_hz = bandwidth_hz
        self.max_samples = max_samples
        self.status, self.error = "starting", None
        self.samples_received = 0
        self.overflows = 0  # driver-reported overruns (samples lost)
        self.driver_path: str | None = None  # "libbladerf" or "soapysdr"
        # (sequence, CS8 payload, center frequency, tuning generation); the generation counts applied retunes, so a
        # reader can tell samples taken after a retune from ones still in flight.
        self._ring: deque[tuple[int, bytes, float, int]] = deque(maxlen=RING_CHUNKS)
        self._seq = 0
        self._gen = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._pending_freq: int | None = None
        self.retuned_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"soapy-rx-{device.id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        soapy, rx = self.soapy, self.soapy.SOAPY_SDR_RX
        dev = stream = None
        try:
            dev = self._open()
            self.driver_path = "libbladerf" if isinstance(dev, bladerf_native.NativeBladeRF) else "soapysdr"
            dev.setSampleRate(rx, 0, float(self.rate_sps))
            dev.setFrequency(rx, 0, float(self.center_hz))
            if self.bandwidth_hz:
                try:
                    dev.setBandwidth(rx, 0, float(self.bandwidth_hz))
                except Exception:
                    pass
            _apply_gains(soapy, dev, self.device.driver, *self.gains)
            stream = dev.setupStream(rx, soapy.SOAPY_SDR_CF32, [0])
            dev.activateStream(stream)
            self.status = "running"
            buf = np.zeros(CHUNK_SAMPLES, dtype=np.complex64)
            received = 0
            last_data = time.monotonic()
            last_error = None
            while not self._stop.is_set():
                if self._pending_freq is not None:
                    freq, self._pending_freq = self._pending_freq, None
                    dev.setFrequency(rx, 0, float(freq))
                    with self._cond:
                        self._gen += 1
                        self.center_hz, self.retuned_at, self.status = freq, time.time(), "running"
                filled = 0
                while filled < CHUNK_SAMPLES and not self._stop.is_set() and self._pending_freq is None:
                    result = dev.readStream(stream, [buf[filled:]], CHUNK_SAMPLES - filled, timeoutUs=500_000)
                    if result.ret > 0:
                        filled += result.ret
                        last_data = time.monotonic()
                    elif result.ret == SOAPY_SDR_OVERFLOW:
                        self.overflows += 1
                    elif result.ret not in TRANSIENT_READ_ERRORS:
                        last_error = result.ret
                    if time.monotonic() - last_data > NO_DATA_TIMEOUT_S:
                        raise RuntimeError(f"no samples for {NO_DATA_TIMEOUT_S:.0f} s"
                                           + (f" (driver read error {last_error})" if last_error is not None else ""))
                if filled == 0:
                    continue
                payload = cf32_to_cs8(buf[:filled])
                with self._cond:
                    self._seq += 1
                    self._ring.append((self._seq, payload, self.center_hz, self._gen))
                    self._cond.notify_all()
                if not received and self._on_success:
                    self._on_success()  # the device delivers again: clear its failure backoff
                received += filled
                self.samples_received = received
                if self.max_samples and received >= self.max_samples:
                    break
            self.status = "stopped"
        except Exception as exc:
            self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
            if self._on_failure:
                self._on_failure()  # drop the cached handle so the next stream reopens the device
        finally:
            try:
                if dev is not None and stream is not None:
                    dev.deactivateStream(stream)
                    dev.closeStream(stream)
            except Exception:
                pass
            with self._cond:
                self._cond.notify_all()

    def retune(self, center_hz: int) -> None:
        self.status = "retuning"
        self._pending_freq = int(center_hz)

    def capture(self, samples: int, center_hz: int | None = None, settle_samples: int = 0,
                timeout_s: float = 3.0) -> tuple[bytes, int]:
        """`samples` contiguous CS8 samples taken after now (and after the retune, when `center_hz` differs from the
        current frequency), dropping `settle_samples` first. One call per dwell is how a hopping client (tune, read a
        block, move on) works without guessing which streamed frames already belong to the new frequency.
        Returns (payload, center_hz)."""
        need, skip = int(samples) * 2, 0
        with self._cond:
            gen = self._gen
            if center_hz is not None and (int(center_hz) != self.center_hz or self._pending_freq is not None):
                gen += 1
                self._pending_freq, self.status = int(center_hz), "retuning"
                skip = max(0, int(settle_samples)) * 2
            cursor = self._seq
            out = bytearray()
            deadline = time.monotonic() + timeout_s
            while len(out) < need:
                progressed = False
                for seq, payload, freq, g in self._ring:
                    if seq <= cursor:
                        continue
                    if out and seq != cursor + 1:
                        out.clear()  # fell behind the ring: start the block again so it stays contiguous
                    cursor, progressed = seq, True
                    if g < gen:
                        continue
                    if g > gen:
                        raise RuntimeError("the stream was retuned by another client during the capture")
                    if skip:
                        drop = min(skip, len(payload))
                        skip, payload = skip - drop, payload[drop:]
                    out.extend(payload)
                    if len(out) >= need:
                        break
                if len(out) >= need:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive:
                    raise TimeoutError(f"captured {len(out) // 2} of {samples} samples in {timeout_s:.1f} s"
                                       + (f" ({self.error})" if self.error else ""))
                if not progressed:
                    self._cond.wait(remaining)
            return bytes(out[:need]), int(self.center_hz)

    def cursor(self, start: str = "latest") -> int:
        with self._cond:
            if start == "oldest" and self._ring:
                return self._ring[0][0] - 1
            return self._seq

    def read(self, cursor: int, timeout_s: float = 1.0) -> tuple[int, bytes]:
        """Next chunk after `cursor` (skipping ahead if the reader fell behind the ring)."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                for seq, payload, _, _ in self._ring:
                    if seq > cursor:
                        return seq, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive:
                    return cursor, b""
                self._cond.wait(remaining)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, join_s: float = 3.0) -> None:
        self._stop.set()
        self._thread.join(join_s)

    def release(self) -> None:
        """Drop buffered samples once the stream is stopped: a stopped stream must not keep its ring in memory."""
        with self._cond:
            self._ring.clear()
            self._cond.notify_all()


@dataclass
class Session:
    id: str
    kind: str
    config: dict[str, Any]
    worker: RxWorker | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.extra.get("stopped"):
            return "stopped"
        return self.worker.status if self.worker else self.extra.get("status", "running")


class SoapyBackend:
    """Devices, streams, power sweeps, and IQ sweeps over SoapySDR, with one owner per device."""

    def __init__(self, soapy: Any = None, include_non_rf: bool = False, native_bladerf: bool | None = None):
        # Direct libbladeRF for bladeRF devices: on by default with the real SoapySDR, off when one is injected.
        self.native_bladerf = (soapy is None and bladerf_native.enabled()) if native_bladerf is None else native_bladerf
        self.soapy = soapy or load_soapysdr()
        self.include_non_rf = include_non_rf
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._handles: dict[str, Any] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self.sessions: dict[str, Session] = {}

    def _failure_handler(self, device: Device):
        def drop_handle():
            self._handles.pop(device.id, None)
            self._record_failure(device.id)
        return drop_handle

    def _success_handler(self, device: Device):
        def delivered():
            self._failures.pop(device.id, None)
        return delivered

    def _record_failure(self, device_id: str) -> None:
        count, _ = self._failures.get(device_id, (0, 0.0))
        self._failures[device_id] = (count + 1, time.monotonic())

    def backoff_remaining(self, device_id: str) -> float:
        """Seconds until a failed device may be reopened. Reopening a wedged radio in a tight loop can drive
        the USB host into a reset storm (seen with bladeRF), so consecutive failures back off 2, 4, 8 ... 60 s."""
        count, when = self._failures.get(device_id, (0, 0.0))
        if not count:
            return 0.0
        return max(0.0, min(60.0, 2.0 ** count) - (time.monotonic() - when))

    def _open_handle(self, driver: str, kwargs: dict[str, str]) -> Any:
        """bladeRF goes through libbladeRF directly (continuous stream; see bladerf_native); others via SoapySDR."""
        if driver == "bladerf" and self.native_bladerf and bladerf_native.load() is not None:
            return bladerf_native.NativeBladeRF(kwargs)
        return self.soapy.Device(kwargs)

    def _opener(self, device: Device):
        """Open once and reuse. Safe because one session owns a device at a time and is joined before the next."""
        def open_device():
            handle = self._handles.get(device.id)
            if handle is None:
                wait = self.backoff_remaining(device.id)
                if wait > 0:
                    raise RuntimeError(f"{device.id} failed recently; not reopening for {wait:.0f} s "
                                       "(protects the USB host from reset storms)")
                try:
                    handle = self._open_handle(device.driver, device.kwargs)
                except Exception:
                    self._record_failure(device.id)
                    raise
                self._handles[device.id] = handle
            return handle
        return open_device

    # Devices ------------------------------------------------------------------------------------
    def refresh_devices(self) -> list[Device]:
        found: dict[str, Device] = {}
        counters: dict[str, int] = {}
        for raw in self.soapy.Device.enumerate():
            kwargs = {str(k): str(v) for k, v in dict(raw).items()}
            driver = kwargs.get("driver", "unknown").lower()
            if driver in NON_RF_DRIVERS and not self.include_non_rf:
                continue
            index = counters.get(driver, 0)
            counters[driver] = index + 1
            device_id = f"{driver}:{index}"
            known = self._devices.get(device_id)
            if known and known.kwargs == kwargs:
                found[device_id] = known
                continue
            found[device_id] = self._describe(device_id, driver, kwargs)
        with self._lock:
            self._devices = found
        return list(found.values())

    def _describe(self, device_id: str, driver: str, kwargs: dict[str, str]) -> Device:
        label = kwargs.get("label") or f"{driver} {kwargs.get('serial', '')}".strip()
        lo, hi, rate = KNOWN_DRIVERS.get(driver, (1_000_000, 6_000_000_000, 20_000_000))
        notes = "ranges from driver defaults"
        if device_id not in self._owners:
            try:
                rx = self.soapy.SOAPY_SDR_RX
                dev = self._handles.get(device_id) or self._open_handle(driver, kwargs)
                self._handles[device_id] = dev
                freqs = list(dev.getFrequencyRange(rx, 0))
                rates = list(dev.getSampleRateRange(rx, 0))
                if freqs:
                    lo, hi = int(min(r.minimum() for r in freqs)), int(max(r.maximum() for r in freqs))
                if rates:
                    rate = int(max(r.maximum() for r in rates))
                notes = "ranges reported by the driver"
            except Exception as exc:
                notes = f"ranges from driver defaults (device not queried: {type(exc).__name__})"
        return Device(device_id, driver, label, kwargs.get("serial"), kwargs, lo, hi, rate, notes)

    def devices(self) -> list[dict[str, Any]]:
        if not self._devices:
            self.refresh_devices()
        with self._lock:
            return [d.info(self._owners.get(d.id)) for d in self._devices.values()]

    def device(self, device_id: str) -> Device:
        if device_id not in self._devices:
            self.refresh_devices()
        if device_id not in self._devices:
            raise KeyError(f"Unknown device_id '{device_id}'")
        return self._devices[device_id]

    def _claim(self, device_id: str, kind: str, session_id: str, replace: bool) -> None:
        with self._lock:
            owner = self._owners.get(device_id)
            previous = self.sessions.get(owner[1]) if owner else None
            if previous is not None and previous.worker is not None and not previous.worker.alive \
                    and not previous.extra.get("stopped"):
                # The previous session's receiver failed (e.g. USB error or unplug): it no longer holds the device.
                previous.extra["stopped"] = True
                owner = None
            if owner and owner[1] in self.sessions:
                if not replace:
                    raise DeviceBusy(f"{device_id} is in use by {owner[0]} {owner[1]}")
                self.stop(owner[1])
            self._owners[device_id] = (kind, session_id)

    def _check(self, device: Device, center_hz: int, rate_sps: int) -> None:
        if not device.freq_min_hz <= center_hz <= device.freq_max_hz:
            raise ValueError(f"{center_hz} Hz is outside {device.id}'s range {device.freq_min_hz}-{device.freq_max_hz} Hz")
        if rate_sps > device.max_sample_rate_sps:
            raise ValueError(f"{rate_sps} S/s exceeds {device.id}'s maximum {device.max_sample_rate_sps} S/s")

    # Streams ------------------------------------------------------------------------------------
    def start_stream(self, config: dict[str, Any]) -> Session:
        device = self.device(config["device_id"])
        self._check(device, int(config["center_freq_hz"]), int(config["sample_rate_sps"]))
        sid = f"stream-{uuid.uuid4().hex[:12]}"
        self._claim(device.id, "stream", sid, bool(config.get("replace_existing")))
        max_samples = config.get("num_samples") or (
            int(config["duration_seconds"]) * int(config["sample_rate_sps"]) if config.get("duration_seconds") else None)
        worker = RxWorker(self.soapy, device, config["center_freq_hz"], config["sample_rate_sps"],
                          config.get("lna_gain_db", 16), config.get("vga_gain_db", 20), bool(config.get("amp_enable")),
                          config.get("baseband_filter_hz"), max_samples, opener=self._opener(device), on_failure=self._failure_handler(device),
                          on_success=self._success_handler(device))
        session = Session(sid, "stream", dict(config), worker)
        with self._lock:
            self.sessions[sid] = session
        return session

    def retune_stream(self, session_id: str, config: dict[str, Any]) -> Session:
        session = self.get(session_id, "stream")
        self._check(self.device(session.config["device_id"]), int(config["center_freq_hz"]),
                    int(config.get("sample_rate_sps", session.config["sample_rate_sps"])))
        if int(config.get("sample_rate_sps", session.config["sample_rate_sps"])) != int(session.config["sample_rate_sps"]):
            raise ValueError("Changing sample rate requires a new stream")
        session.worker.retune(int(config["center_freq_hz"]))
        session.config.update({k: v for k, v in config.items() if k in session.config or k == "center_freq_hz"})
        return session

    def capture_stream(self, session_id: str, config: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        """Tune (optionally) and return one contiguous block from a running stream: see RxWorker.capture."""
        session = self.get(session_id, "stream")
        samples = int(config.get("samples") or 0)
        if not 0 < samples <= MAX_CAPTURE_SAMPLES:
            raise ValueError(f"samples must be 1..{MAX_CAPTURE_SAMPLES}")
        center = config.get("center_freq_hz")
        if center is not None:
            self._check(self.device(session.config["device_id"]), int(center), int(session.config["sample_rate_sps"]))
        rate = int(session.config["sample_rate_sps"])
        settle = int(float(config.get("settle_us") or 0) * rate / 1e6)
        payload, freq = session.worker.capture(samples, None if center is None else int(center), settle,
                                               float(config.get("timeout_s") or 3.0))
        session.config["center_freq_hz"] = freq
        return payload, {"center_freq_hz": freq, "sample_rate_sps": rate, "samples": len(payload) // 2}

    # Power sweeps -------------------------------------------------------------------------------
    def start_sweep(self, config: dict[str, Any], engine: str = "soapy_sweep") -> Session:
        device = self.device(config["device_id"])
        start, stop = int(config["start_freq_hz"]), int(config["stop_freq_hz"])
        if start >= stop:
            raise ValueError("start_freq_hz must be lower than stop_freq_hz")
        rate = min(device.max_sample_rate_sps, 20_000_000)
        usable = int(rate * 0.75)
        centers = [start + usable // 2 + i * usable for i in range(max(1, math.ceil((stop - start) / usable)))]
        sid = f"sweep-{uuid.uuid4().hex[:12]}"
        self._claim(device.id, "sweep", sid, False)
        worker = RxWorker(self.soapy, device, centers[0], rate, config.get("lna_gain_db", 16),
                          config.get("vga_gain_db", 20), bool(config.get("amp_enable")), opener=self._opener(device), on_failure=self._failure_handler(device),
                          on_success=self._success_handler(device))
        session = Session(sid, "sweep", dict(config), worker,
                          {"samples": deque(maxlen=4096), "engine": engine, "centers": centers, "usable": usable})
        threading.Thread(target=self._sweep_loop, args=(session, rate), daemon=True).start()
        with self._lock:
            self.sessions[sid] = session
        return session

    def _sweep_loop(self, session: Session, rate: int) -> None:
        bin_width = int(session.config.get("bin_width_hz", 100_000))
        fft_size = int(2 ** max(8, min(16, round(math.log2(max(256, rate / bin_width))))))
        worker, usable = session.worker, session.extra["usable"]
        while not session.extra.get("stopped") and worker.alive:
            for center in session.extra["centers"]:
                if session.extra.get("stopped") or not worker.alive:
                    return
                worker.retune(center)
                cursor = worker.cursor()
                time.sleep(0.02)
                cursor, payload = worker.read(cursor, timeout_s=2.0)
                if worker.center_hz != center or len(payload) < fft_size * 2:
                    cursor, payload = worker.read(cursor, timeout_s=2.0)
                if len(payload) < fft_size * 2:
                    continue
                iq = np.frombuffer(payload[: (len(payload) // (fft_size * 2)) * fft_size * 2], dtype=np.int8)
                iq = (iq[0::2].astype(np.float32) + 1j * iq[1::2].astype(np.float32)) / 128.0
                frames = iq.reshape(-1, fft_size) * np.hanning(fft_size)
                power = np.mean(np.abs(np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)) ** 2, axis=0)
                db = 10 * np.log10(power / fft_size + 1e-12)
                freqs = center + (np.arange(fft_size) - fft_size / 2) * rate / fft_size
                keep = np.abs(freqs - center) <= usable / 2
                low, high = int(freqs[keep][0]), int(freqs[keep][-1])
                session.extra["samples"].append({"timestamp": datetime.now(timezone.utc).isoformat(), "hz_low": low,
                                                 "hz_high": high, "db_values": [round(float(v), 2) for v in db[keep]]})

    # IQ sweeps ----------------------------------------------------------------------------------
    def start_iq_sweep(self, config: dict[str, Any]) -> Session:
        device = self.device(config["device_id"])
        centers = [int(f) for f in config.get("center_freqs_hz") or []]
        if not centers:
            start, stop, hop = config.get("start_freq_hz"), config.get("stop_freq_hz"), config.get("hop_hz")
            if not (start and stop and hop):
                raise ValueError("provide center_freqs_hz, or start_freq_hz, stop_freq_hz and hop_hz")
            centers = list(range(int(start), int(stop) + 1, int(hop)))
        rate = int(config.get("sample_rate_sps", 2_000_000))
        for center in centers:
            self._check(device, center, rate)
        sid = f"iqsweep-{uuid.uuid4().hex[:12]}"
        self._claim(device.id, "iq_sweep", sid, False)
        worker = RxWorker(self.soapy, device, centers[0], rate, config.get("lna_gain_db", 16),
                          config.get("vga_gain_db", 20), bool(config.get("amp_enable")), config.get("baseband_filter_hz"),
                          opener=self._opener(device), on_failure=self._failure_handler(device),
                          on_success=self._success_handler(device))
        session = Session(sid, "iq_sweep", dict(config), worker,
                          {"centers": centers, "point_index": 0, "stream_id": f"{sid}-stream", "cursor": 0})
        session.extra["cursor"] = worker.cursor()
        threading.Thread(target=self._hop_loop, args=(session,), daemon=True).start()
        with self._lock:
            self.sessions[sid] = session
        return session

    def _hop_loop(self, session: Session) -> None:
        dwell = float(session.config.get("dwell_s", 1.5))
        while not session.extra.get("stopped") and session.worker.alive:
            time.sleep(dwell)
            if session.extra.get("stopped"):
                return
            index = (session.extra["point_index"] + 1) % len(session.extra["centers"])
            session.extra["point_index"] = index
            session.worker.retune(session.extra["centers"][index])

    def iq_sweep_chunk(self, session_id: str, nbytes: int) -> dict[str, Any]:
        session = self.get(session_id, "iq_sweep")
        worker = session.worker
        data = bytearray()
        deadline = time.monotonic() + 2.0
        while len(data) < nbytes and time.monotonic() < deadline:
            session.extra["cursor"], chunk = worker.read(session.extra["cursor"], timeout_s=0.5)
            data.extend(chunk)
        data = bytes(data[:nbytes])
        return {"iq_sweep_id": session.id, "stream_id": session.extra["stream_id"], "device_id": session.config["device_id"],
                "center_freq_hz": worker.center_hz, "sample_rate_sps": worker.rate_sps,
                "point_index": session.extra["point_index"], "retuned_at": worker.retuned_at,
                "byte_count": len(data), "iq_i8_b64": base64.b64encode(data).decode()}

    # Common -------------------------------------------------------------------------------------
    def get(self, session_id: str, kind: str | None = None) -> Session:
        session = self.sessions.get(session_id)
        if session is None or (kind and session.kind != kind) or session.extra.get("stopped"):
            raise KeyError(f"Unknown {kind or 'session'} {session_id}")
        return session

    def list(self, kind: str) -> list[Session]:
        return [s for s in self.sessions.values() if s.kind == kind and not s.extra.get("stopped")]

    def stop(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.extra.get("stopped"):
            raise KeyError(f"Unknown session {session_id}")
        session.extra["stopped"] = True
        session.extra["stopped_at"] = time.time()
        if session.worker:
            session.worker.stop()
            session.worker.release()
        with self._lock:
            for device_id, owner in list(self._owners.items()):
                if owner[1] == session_id:
                    del self._owners[device_id]
            self._prune()

    STOPPED_KEPT_S = 60.0

    def _prune(self) -> None:
        """Forget sessions stopped more than a minute ago. Every stopped session used to stay in this table with its
        worker for the life of the service: an agent run starts a stream every few seconds, so a standing watch grew
        the process by gigabytes until it died (found with scripts/soak_iq_service.py, about 12 MB per stream)."""
        cutoff = time.time() - self.STOPPED_KEPT_S
        for sid in [sid for sid, s in self.sessions.items() if s.extra.get("stopped") and s.extra.get("stopped_at", 0) < cutoff]:
            del self.sessions[sid]

    def release_handles(self) -> None:
        """Close idle device handles (none may be streaming)."""
        with self._lock:
            for device_id in [d for d in self._handles if d not in self._owners]:
                self._handles.pop(device_id, None)

    def stop_all(self) -> None:
        for session_id in [s.id for s in self.sessions.values() if not s.extra.get("stopped")]:
            try:
                self.stop(session_id)
            except KeyError:
                pass
