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

from .capabilities import classic_capability
from .gateway import AgentGateway, PermissionDenied, effective_mode
from .profiles import PROFILES, Profile, Task, parse
from .radio import Radio, auto_gain, split_gain

WINDOW_S = 8.0
SATURATED_PEAK_DBFS = -1.0
SATURATED_FRACTION = 0.2
WEAK_MEDIAN_PEAK_DBFS = -18.0
MAX_ADJUSTMENTS = 4


def drone_profile_for(profile: Profile, command: str) -> Profile:
    """Gain is measured where the detector will listen: the 2.4 GHz controller band when the request asks for it."""
    from dataclasses import replace
    from .profiles import drone_band
    if drone_band(command) != "2g4":
        return profile
    return replace(profile, freqs_hz=(2_405_500_000, 2_440_000_000, 2_475_500_000),
                   notes="Runs the station's drone detector through this receiver on the 2.4 GHz band, where drone "
                         "controllers and some links transmit. Needs the detector installed on this host.")


def plugin_path(name: str) -> str | None:
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else shutil.which(name)


def simulate_command(task: Task, device: dict[str, Any], clock: tuple[float, float] | None = None) -> list[str]:
    """The scripted-scenario stand-in for a receiver plugin (rf_platform.agent.simulate).

    clock = (scenario start, scenario length): a playbook runs one scenario across all its steps."""
    import zlib
    seed = zlib.crc32(f"{task.profile.name}|{device['id']}".encode()) % 1000  # reproducible, unlike hash()
    duration, offset = (task.duration_s, 0.0) if clock is None else (clock[1], max(0.0, time.time() - clock[0]))
    tag = int(clock[0]) if clock is not None else int(time.time())
    return [sys.executable, "-m", "rf_platform.agent.simulate", "--profile", task.profile.name,
            "--duration", str(duration), "--seed", str(seed), "--offset", f"{offset:.1f}", "--run-tag", str(tag)]


def plugin_command(task: Task, device: dict[str, Any], gain_db: float, base_url: str, token: str) -> list[str]:
    p, exe = task.profile, plugin_path(task.profile.plugin)
    if p.name == "cellular":  # modem survey adapter, not an SDR plugin: no gain
        return [sys.executable, "-m", "rf_platform.cellular", "--url", device.get("url") or ""]
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
    if p.name == "drone":  # the external detector, reading through the IQ service (see rf_platform/plugins/drone-rf)
        from .profiles import drone_band
        return [exe, "listen", "--base-url", base_url, *auth, "--device-id", device["id"],
                "--band", drone_band(task.command), *common_gain, "--json"]
    if p.name == "classic":
        # Receive-only "listen" from the IQ service; the plugin's transmitting page-stimulus mode is never built here.
        # Absolute, and created here: the native decoder runs from the plugin's own directory, so a relative path
        # pointed into a folder that did not exist and the decoder exited (255) before decoding anything.
        log = (Path(os.getenv("RF_SENTINEL_LOG_DIR") or ".demo/livelog") / "agent-classic-sniffer.log").resolve()
        log.parent.mkdir(parents=True, exist_ok=True)
        return [exe, "listen", "--source", "gateway", "--gateway-base-url", base_url,
                *(["--gateway-token", token] if token else []), "--device-id", device["id"],
                "--center-mhz", str(p.extra["center_mhz"]), "--bandwidth-mhz", str(p.extra["bandwidth_mhz"]),
                *common_gain, "--log", str(log), "--json", "--metrics"]
    raise ValueError(f"No runner for {p.name}")


class Agent:
    def __init__(self, base_url: str | None = None, token: str | None = None, gateway: AgentGateway | None = None,
                 mode: str | None = None, run_id: str | None = None, simulate: bool | None = None):
        if simulate is None:
            simulate = os.getenv("RF_SENTINEL_AGENT_SIMULATE", "").strip().lower() in ("1", "true", "yes", "on")
        self.simulate = bool(simulate)
        if self.simulate:
            from .simulate import SimRadio
            self.radio = SimRadio()
        else:
            self.radio = Radio(base_url, token)
        self._stop = threading.Event()
        self.gateway = gateway or AgentGateway(effective_mode(mode), run_id or "local", stop=self._stop)
        self.gateway.stop = self._stop
        self._deadline = float("inf")
        self.stop_reason: str | None = None

    def _classic_capability(self, devices, exclude=()):
        if self.simulate:
            free = [d for d in devices if d["id"] not in exclude]
            return ({"supported": True, "device": free[0], "decoder": "simulated", "reason": f"{free[0]['id']} (simulated)"}
                    if free else {"supported": False, "reason": "no free simulated receiver"})
        return classic_capability(devices, plugin_path(PROFILES["classic"].plugin), exclude=exclude)

    def child(self) -> "Agent":
        """Another agent on the same radio service, gateway (policy, approvals, audit), and stop control."""
        child = Agent.__new__(Agent)
        child.radio, child._stop, child.gateway = self.radio, self._stop, self.gateway
        child._deadline, child.stop_reason = float("inf"), None
        child.simulate = self.simulate
        child.sim_clock = getattr(self, "sim_clock", None)
        return child

    def stop(self, reason: str = "operator") -> None:
        self.stop_reason = self.stop_reason or reason
        self._stop.set()
        self.gateway.broker.cancel_run(self.gateway.run_id)

    def finish_early(self, reason: str) -> None:
        """End collection now and report normally (e.g. a workflow trigger said 'stop')."""
        self.stop_reason = self.stop_reason or reason
        self._deadline = time.monotonic()

    def extend(self, seconds: float, limit_s: float) -> float:
        """Push the end of collection out by `seconds`, never past `limit_s` from the start. Returns the added time."""
        if self._deadline == float("inf"):
            return 0.0
        end = min(self._deadline + seconds, self._started_mono + limit_s)
        added = max(0.0, end - self._deadline)
        self._deadline = end
        return added

    def run(self, command: str, on_step: Callable[[str], None] = print,
            on_detection: Callable[[dict[str, Any]], None] | None = None,
            gain_db: float | None = None) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        started = time.time()

        def step(message: str) -> None:
            steps.append({"t": round(time.time() - started, 1), "message": message})
            on_step(message)

        task = parse(command)
        if task.profile.name == "drone":
            task.profile = drone_profile_for(task.profile, command)
        capabilities: dict[str, Any] = {}
        companion: dict[str, Any] | None = None
        if task.profile.name == "classic" or "classic" in task.companions:
            self.gateway.authorize("list_receivers", {})
            devices = self.radio.devices()
            if task.profile.name == "classic":
                cap = self._classic_capability(devices)
                capabilities["classic"] = {k: v for k, v in cap.items() if k != "device"}
                if not cap["supported"]:
                    step(f"Bluetooth Classic is not available here: {cap['reason']}. Continuing with BLE.")
                    task = Task(PROFILES["ble"], task.duration_s, task.device_id, None, command)
            else:
                ble_device = self.radio.choose(PROFILES["ble"].freqs_hz, PROFILES["ble"].sample_rate_sps, task.device_id)
                cap = self._classic_capability(devices, exclude=(ble_device["id"],))
                capabilities["classic"] = {k: v for k, v in cap.items() if k != "device"}
                if cap["supported"]:
                    companion = {"profile": "classic", "device": cap["device"]}
                    task = Task(task.profile, task.duration_s, ble_device["id"], task.channel, command)
                else:
                    step(f"Bluetooth Classic not run: {cap['reason']}. Running BLE only.")
        if task.profile.name == "classic" and "ble" in task.companions:
            classic_device = self.radio.choose(task.profile.freqs_hz, task.profile.sample_rate_sps, task.device_id)
            try:
                ble_device = self.radio.choose(PROFILES["ble"].freqs_hz, PROFILES["ble"].sample_rate_sps,
                                               exclude=(classic_device["id"],))
                companion = {"profile": "ble", "device": ble_device}
            except RuntimeError as exc:
                step(f"BLE not run alongside Classic: {exc}")
            task = Task(task.profile, task.duration_s, classic_device["id"], task.channel, command)
        profile = task.profile
        modem = profile.extra.get("source") == "modem"
        exe = sys.executable if (modem or self.simulate) else plugin_path(profile.plugin)
        if not exe:
            raise RuntimeError(f"The {profile.title} receiver ({profile.plugin}) is not installed. "
                               f"Install it with: pip install -e rf_platform/plugins/<plugin>")
        child_result: dict[str, Any] = {}
        child_thread = None
        if companion is not None:
            child_thread = self._start_companion(companion, task, step, on_detection, gain_db, child_result)
        gw = self.gateway
        if self.simulate:
            step("SIMULATION: no radio is used. Receivers and detections are synthetic (scripted scenario).")
        step(f"Task: {profile.title} for {task.duration_s} s (receive-only, {gw.mode} mode). {profile.notes}")
        self.gateway.authorize("list_receivers", {})
        if modem and self.simulate:
            cap = {"supported": True, "url": "sim://", "reason": "simulated cellular modem"}
            capabilities["cellular"] = cap
            device = {"id": "sim:cellmodem", "label": "Simulated cellular modem", "driver": "sim"}
        elif modem:
            from rf_platform.cellular.survey import cellular_capability
            cap = cellular_capability()
            capabilities["cellular"] = cap
            if not cap["supported"]:
                raise RuntimeError(f"Cellular survey is not available: {cap['reason']}")
            device = {"id": "cellmodem:0", "label": f"cellular modem ({cap.get('port') or 'auto'})", "driver": "modem",
                      "url": cap["url"]}
        else:
            device = self.radio.choose(profile.freqs_hz, profile.sample_rate_sps, task.device_id,
                                       exclude=(companion["device"]["id"],) if companion else ())
        step(f"Receiver: {device['id']} ({device['label']})")
        if gw.mode == "approval-required":
            step("Waiting for operator approval before touching the receiver")
        if self.simulate:
            gain = None
            step("Simulated receiver: no gain stage")
        elif modem:
            gain = None
            step(f"{cap['reason']}")
        elif gain_db is None:
            gw.authorize("optimize_gain", {"device_id": device["id"], "freqs_hz": list(profile.freqs_hz),
                                           "gain_bounds_db": [0, 60]})
            gain = auto_gain(self.radio, device, profile.freqs_hz, profile.sample_rate_sps, log=step,
                             should_stop=self._stop.is_set)
        else:
            gain = float(gain_db)
            step(f"gain fixed at {gain:.0f} dB by request")
        gw.authorize("start_scan", {"device_id": device["id"], "profile": profile.name, "gain_db": gain,
                                    "duration_s": task.duration_s})

        detections: dict[str, dict[str, Any]] = {}
        decoder: dict[str, dict[str, Any]] = {}  # per-channel decoder counters reported by the plugin
        adjustments = 0
        gains_used = [gain] if gain is not None else []
        self._started_mono = time.monotonic()
        self._deadline = self._started_mono + task.duration_s
        while time.monotonic() < self._deadline and not self._stop.is_set():
            command_line = (simulate_command(task, device, getattr(self, "sim_clock", None)) if self.simulate
                            else plugin_command(task, device, gain, self.radio.base_url, self.radio.token))
            proc = subprocess.Popen(command_line,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            peaks: list[float] = []
            classic_metrics: dict[str, Any] | None = None
            proc_started = time.monotonic()
            window_end = time.monotonic() + WINDOW_S
            decided = None
            try:
                for line in self._lines(proc):
                    event = self._event(line)
                    kind = event.get("kind") if event else None
                    if kind in profile.burst_kinds and isinstance(event.get("peak_dbfs"), (int, float)):
                        peaks.append(float(event["peak_dbfs"]))
                    elif kind in profile.detection_kinds:
                        self._record(detections, profile, event, gain, on_detection)
                    elif kind and kind.endswith("_decoder_stats"):
                        self._decoder_stats(decoder, event)
                    elif event and event.get("type") == "metrics" and profile.name == "classic":
                        classic_metrics = event  # cumulative for this plugin process
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
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if classic_metrics:
                    streamed_s = max(1e-3, time.monotonic() - proc_started)
                    item = decoder.setdefault("classic", {})
                    item["streamed_s"] = round(item.get("streamed_s", 0) + streamed_s, 1)
                    self._decoder_stats(decoder, {**{k: v for k, v in classic_metrics.items()
                                                     if k not in ("time_us", "bins", "active_laps")
                                                     and not k.startswith("fhs_expected")}, "channel": "classic"})
            if decided is None or self._stop.is_set():
                break
            new_gain, reason = decided
            try:
                gw.authorize("adjust_gain", {"device_id": device["id"], "from_db": gain, "to_db": new_gain})
            except PermissionDenied as exc:
                step(f"{reason}; gain change not made ({exc}). Continuing at {gain:.0f} dB")
                adjustments = MAX_ADJUSTMENTS  # do not keep asking
                continue
            step(f"{reason}: adjusting gain {gain:.0f} -> {new_gain:.0f} dB and restarting the receiver")
            gain, adjustments = new_gain, adjustments + 1
            gains_used.append(gain)
        if self._stop.is_set():
            step("Stopped by operator; receiver released")
        elif self.stop_reason:
            step(f"Collection ended early: {self.stop_reason}; receiver released")

        classic = decoder.get("classic")
        if classic and classic.get("streamed_s"):
            # samples_processed counts 1 MS/s per channel bin, so it equals seconds of stream decoded x 1e6.
            coverage = min(1.0, classic.get("samples_processed", 0) / 1e6 / classic["streamed_s"])
            classic["realtime_coverage"] = round(coverage, 2)
            if coverage < 0.95:
                step(f"Classic decoder processed {coverage:.0%} of the stream (it runs slower than real time "
                     f"at this bandwidth); packets in the skipped portions were not examined")
        devices = sorted(detections.values(), key=lambda d: -d["count"])
        step(f"Done: {len(devices)} {profile.name} device(s), {sum(d['count'] for d in devices)} detections; "
             + (f"gain {', '.join(f'{g:.0f}' for g in gains_used)} dB" if gains_used else "no gain stage"))
        companion_summary = None
        if child_thread is not None:
            child_thread.join(timeout=90)
            child = child_result.get("result")
            companion_summary = {"profile": companion["profile"], "receiver": companion["device"]["id"],
                                 "error": child_result.get("error")}
            if child:
                devices = devices + child["devices"]
                decoder.update(child.get("decoder_stats") or {})
                companion_summary.update({"devices": len(child["devices"]), "gains_db": child["gains_db"],
                                          "detections": sum(d["count"] for d in child["devices"])})
        from .report import finalize
        return finalize({"companion": companion_summary, "capabilities": capabilities,"command": command, "profile": profile.name, "title": profile.title, "receiver": device["id"],
                         "receiver_label": device.get("label"), "duration_s": task.duration_s, "gains_db": gains_used,
                         "adjustments": adjustments, "devices": devices, "steps": steps,
                         "elapsed_s": round(time.time() - started, 1), "receive_only": True,
                         "mode": gw.mode, "stopped": self._stop.is_set(), "stop_reason": self.stop_reason,
                         "simulated": self.simulate,
                         "audit": gw.audit,
                         "decoder_stats": decoder}, started)

    def _start_companion(self, companion: dict[str, Any], task: Task, step: Callable[[str], None],
                         on_detection, gain_db: float | None, out: dict[str, Any]) -> threading.Thread:
        """Run a second protocol on its own receiver for the same duration, under the same gateway and stop."""
        child = self.child()
        name = companion["profile"]
        label = "Classic" if name == "classic" else "BLE"
        what = "bluetooth classic" if name == "classic" else "ble"
        command = f"find {what} devices for {task.duration_s} seconds on {companion['device']['id']}"

        def work():
            try:
                out["result"] = child.run(command, on_step=lambda m: step(f"[{label}] {m}"),
                                          on_detection=on_detection, gain_db=gain_db)
            except Exception as exc:  # reported in the merged result; the primary scan carries on
                out["error"] = f"{type(exc).__name__}: {exc}"
                step(f"[{label}] stopped: {exc}")
        step(f"Also running {label} on {companion['device']['id']} for the same {task.duration_s} s")
        thread = threading.Thread(target=work, name=f"agent-{name}", daemon=True)
        thread.start()
        return thread

    def _lines(self, proc: subprocess.Popen):
        """Plugin stdout lines until the (possibly extended or shortened) deadline, without blocking past it."""
        queue: list[str] = []
        lock = threading.Lock()

        def pump():
            for line in proc.stdout:
                with lock:
                    queue.append(line)
        threading.Thread(target=pump, daemon=True).start()
        while time.monotonic() < self._deadline and not self._stop.is_set() and (proc.poll() is None or queue):
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
    def _decoder_stats(totals: dict[str, dict[str, Any]], event: dict[str, Any]) -> None:
        item = totals.setdefault(str(event.get("channel", "all")), {})
        for field, value in event.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and field != "channel":
                item[field] = round(item.get(field, 0) + value, 2)
        candidates = item.get("aa_candidates", 0)
        item["crc_valid_fraction"] = round(item.get("crc_valid", 0) / candidates, 3) if candidates else None

    @staticmethod
    def _record(detections, profile: Profile, event, gain, on_detection) -> None:
        if profile.name == "classic":
            from .profiles import classic_identity
            classic_identity(event)
        key = profile.key(event)
        if not key:
            return
        old = event.get("supersedes")
        if old and old != key and old in detections:  # a cell first heard by PCI only, now named: one record
            earlier = detections.pop(old)
            item = detections.setdefault(key, {**earlier, "key": key, "count": 0})
            item["count"] += earlier["count"] if item is not earlier else 0
            if earlier.get("first_seen") is not None:
                item["first_seen"] = min(x for x in (item.get("first_seen"), earlier["first_seen"]) if x is not None)
            if "best_rssi_dbfs" in earlier:
                item["best_rssi_dbfs"] = max(earlier["best_rssi_dbfs"], item.get("best_rssi_dbfs", -200.0))
        item = detections.setdefault(key, {"key": key, "count": 0, "first_seen": event.get("seen_at"),
                                           "protocol": profile.name})
        item["count"] += 1
        item["last_seen"] = event.get("seen_at")
        item["gain_db"] = gain
        confirmed = item.get("device_type") == "confirmed drone"
        for field in ("identity", "name", "address_type", "pdu_type", "device_type_detail", "channel", "confidence",
                      "frequency_hz", "device_type"):
            if confirmed and field in ("identity", "device_type", "confidence"):
                continue  # a drone the detector confirmed stays confirmed when a later, weaker sighting arrives
            if event.get(field) not in (None, "", []):
                item[field] = event[field]
        rssi = event.get("rssi_dbfs")
        if isinstance(rssi, (int, float)):
            item["best_rssi_dbfs"] = max(float(rssi), item.get("best_rssi_dbfs", -200.0))
        if isinstance(event.get("manufacturer"), dict):
            item["manufacturer"] = event["manufacturer"].get("company_name")
        if on_detection:
            event.setdefault("agent_profile", profile.name)
            event.setdefault("agent_key", key)
            on_detection(event)
