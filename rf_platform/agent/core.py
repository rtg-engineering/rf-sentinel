"""RF Sentinel operator agent: plain-language request -> receive-only scan -> detections.

    Agent().run("find bluetooth devices", on_step=print, on_detection=handle)

1. Parse the request into a bounded, receive-only task (profiles.parse).
2. Choose a free receiver that covers the band, through the IQ service (SoapySDR by default).
3. Level-based gain: measure IQ on every center and set the highest gain that keeps peaks near
   -6 dBFS without clipping (radio.auto_gain).
4. Run the protocol's receiver plugin. Every 8 s, use the decoder's own burst peaks to adjust:
   lower the gain 6 dB when more than 20% of bursts hit full scale, raise it 6 dB when bursts are weak.
   Each adjustment restarts the receiver and is reported as a step.
5. Aggregate detections per device and return a summary. Nothing is transmitted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .profiles import Profile, Task, parse
from .radio import Radio, auto_gain, split_gain

WINDOW_S = 8.0
SATURATED_PEAK_DBFS = -1.0
SATURATED_FRACTION = 0.2
WEAK_MEDIAN_PEAK_DBFS = -18.0
MAX_ADJUSTMENTS = 4


def plugin_path(name: str) -> str | None:
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else shutil.which(name)


def plugin_command(task: Task, device: dict[str, Any], gain_db: float, base_url: str, token: str) -> list[str]:
    p, exe = task.profile, plugin_path(task.profile.plugin)
    lna, vga = split_gain(device["driver"], gain_db)
    common_gain = ["--lna-gain-db", str(lna), "--vga-gain-db", str(vga)]
    auth = ["--token", token] if token else []
    if p.name == "ble":
        return [exe, "listen", "--base-url", base_url, *auth, "--device-id", device["id"], "--hop", "--dwell-s", "2",
                "--sample-rate-sps", str(p.sample_rate_sps), "--baseband-filter-hz", "2000000", *common_gain,
                "--json", "--debug-bursts"]
    if p.name == "zigbee":
        channel = task.channel or p.extra["channel"]
        return [exe, "listen", "--base-url", base_url, *auth, "--device-id", device["id"], "--channel", str(channel),
                "--sample-rate-sps", str(p.sample_rate_sps), *common_gain, "--json", "--debug-bursts"]
    if p.name == "tpms":
        return [exe, "--gateway", base_url, *auth, "listen", "--device-id", device["id"], "--protocol", "tpms",
                "--auto-hop-known", "--sample-rate-sps", str(p.sample_rate_sps), *common_gain, "--json"]
    raise ValueError(f"No runner for {p.name}")


class Agent:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.radio = Radio(base_url, token)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self, command: str, on_step: Callable[[str], None] = print,
            on_detection: Callable[[dict[str, Any]], None] | None = None,
            gain_db: float | None = None) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        started = time.time()

        def step(message: str) -> None:
            steps.append({"t": round(time.time() - started, 1), "message": message})
            on_step(message)

        task = parse(command)
        profile = task.profile
        exe = plugin_path(profile.plugin)
        if not exe:
            raise RuntimeError(f"The {profile.title} receiver ({profile.plugin}) is not installed. "
                               f"Install it with: pip install -e rf_platform/plugins/<plugin>")
        step(f"Task: {profile.title} for {task.duration_s} s (receive-only). {profile.notes}")
        device = self.radio.choose(profile.freqs_hz, profile.sample_rate_sps, task.device_id)
        step(f"Receiver: {device['id']} ({device['label']})")
        if gain_db is None:
            gain = auto_gain(self.radio, device, profile.freqs_hz, profile.sample_rate_sps, log=step)
        else:
            gain = float(gain_db)
            step(f"gain fixed at {gain:.0f} dB by request")

        detections: dict[str, dict[str, Any]] = {}
        adjustments = 0
        gains_used = [gain]
        deadline = time.monotonic() + task.duration_s
        while time.monotonic() < deadline and not self._stop.is_set():
            proc = subprocess.Popen(plugin_command(task, device, gain, self.radio.base_url, self.radio.token),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            peaks: list[float] = []
            window_end = time.monotonic() + WINDOW_S
            decided = None
            try:
                for line in self._lines(proc, deadline):
                    event = self._event(line)
                    kind = event.get("kind") if event else None
                    if kind in profile.burst_kinds and isinstance(event.get("peak_dbfs"), (int, float)):
                        peaks.append(float(event["peak_dbfs"]))
                    elif kind in profile.detection_kinds:
                        self._record(detections, profile, event, gain, on_detection)
                    if time.monotonic() >= window_end:  # evaluated even when the plugin is quiet
                        decided = self._decide(peaks, gain, adjustments)
                        if decided is not None:
                            break
                        peaks, window_end = [], time.monotonic() + WINDOW_S
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if decided is None:
                break
            new_gain, reason = decided
            step(f"{reason}: adjusting gain {gain:.0f} -> {new_gain:.0f} dB and restarting the receiver")
            gain, adjustments = new_gain, adjustments + 1
            gains_used.append(gain)

        devices = sorted(detections.values(), key=lambda d: -d["count"])
        step(f"Done: {len(devices)} {profile.name} device(s), {sum(d['count'] for d in devices)} detections; "
             f"gain {', '.join(f'{g:.0f}' for g in gains_used)} dB")
        from .report import finalize
        return finalize({"command": command, "profile": profile.name, "title": profile.title, "receiver": device["id"],
                         "receiver_label": device.get("label"), "duration_s": task.duration_s, "gains_db": gains_used,
                         "adjustments": adjustments, "devices": devices, "steps": steps,
                         "elapsed_s": round(time.time() - started, 1), "receive_only": True}, started)

    def _lines(self, proc: subprocess.Popen, deadline: float):
        """Plugin stdout lines until the deadline, without blocking past it."""
        queue: list[str] = []
        lock = threading.Lock()

        def pump():
            for line in proc.stdout:
                with lock:
                    queue.append(line)
        threading.Thread(target=pump, daemon=True).start()
        while time.monotonic() < deadline and not self._stop.is_set() and (proc.poll() is None or queue):
            with lock:
                batch, queue[:] = list(queue), []
            if not batch:
                time.sleep(0.05)
                yield ""
            for line in batch:
                yield line

    @staticmethod
    def _event(line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            return json.loads(line)
        except ValueError:
            return None

    @staticmethod
    def _decide(peaks: list[float], gain: float, adjustments: int) -> tuple[float, str] | None:
        if adjustments >= MAX_ADJUSTMENTS or len(peaks) < 10:
            return None
        saturated = sum(p >= SATURATED_PEAK_DBFS for p in peaks) / len(peaks)
        median = statistics.median(peaks)
        if saturated > SATURATED_FRACTION and gain > 0:
            return max(0.0, gain - 6), f"{saturated:.0%} of {len(peaks)} bursts hit full scale"
        if median < WEAK_MEDIAN_PEAK_DBFS and gain < 60:
            return min(60.0, gain + 6), f"bursts are weak (median peak {median:.1f} dBFS)"
        return None

    @staticmethod
    def _record(detections, profile: Profile, event, gain, on_detection) -> None:
        key = profile.key(event)
        if not key:
            return
        item = detections.setdefault(key, {"key": key, "count": 0, "first_seen": event.get("seen_at"),
                                           "protocol": profile.name})
        item["count"] += 1
        item["last_seen"] = event.get("seen_at")
        item["gain_db"] = gain
        for field in ("identity", "name", "address_type", "pdu_type", "device_type_detail", "channel"):
            if event.get(field) not in (None, "", []):
                item[field] = event[field]
        rssi = event.get("rssi_dbfs")
        if isinstance(rssi, (int, float)):
            item["best_rssi_dbfs"] = max(float(rssi), item.get("best_rssi_dbfs", -200.0))
        if isinstance(event.get("manufacturer"), dict):
            item["manufacturer"] = event["manufacturer"].get("company_name")
        if on_detection:
            on_detection(event)
