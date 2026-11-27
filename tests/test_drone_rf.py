"""drone-rf adapter: event mapping, the IQ-service receiver, and the full listen path against a stand-in detector.

The real detector is external (not in this repo). The stand-in below has the same hooks the adapter relies on:
a module-level source_class_for_device(device_args), a main() that reads --radio5g-device and --collector-port,
reads IQ blocks through the source it is given, and sends JSON events over UDP.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import unittest.mock

import numpy as np

PLUGIN_SRC = Path(__file__).resolve().parents[1] / "rf_platform" / "plugins" / "drone-rf" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from drone_rf import shim  # noqa: E402
from drone_rf.events import to_detection  # noqa: E402
from drone_rf.source import RfSentinelIqSource  # noqa: E402
from rf_platform.soapy_gateway.backend import SoapyBackend  # noqa: E402
from rf_platform.soapy_gateway.server import create_app  # noqa: E402
from tests.test_soapy_gateway import TaggingSoapy  # noqa: E402

DETECTION = {"schema": "passive_shield.station_detection.v1", "event_type": "passive_shield.station.detection",
             "track_id": "trk-1", "track_state": "confirmed", "frequency_hz": 5_745_500_000.0, "radio_band": "ism_5g8",
             "confidence": 0.82, "candidate_family": "dji_ocusync", "detector_details": {"score_db": 14.5},
             "occupied_bandwidth_hz": 10e6}

STAND_IN = textwrap.dedent('''
    import argparse, json, socket
    import numpy as np

    class OwnSource:  # what the detector would use on its own (opens the radio directly)
        pass

    def source_class_for_device(device_args):
        return OwnSource

    def main():
        p = argparse.ArgumentParser()
        p.add_argument("--collector-host"); p.add_argument("--collector-port", type=int)
        p.add_argument("--radio5g-device", default=""); p.add_argument("--radio2g-device", default="")
        p.add_argument("--radio5g-gain", type=float); p.add_argument("--detection-bands"); p.add_argument("--station-id")
        p.add_argument("--dwell-ms", type=float, default=100.0)
        a = p.parse_args()
        cls = source_class_for_device(a.radio5g_device)
        src = cls(device_args=a.radio5g_device, channel=0, sample_rate=2e6, freq=5_745_000_000, gain=a.radio5g_gain,
                  agc=False, antenna=None, bandwidth=2e6, stream_args=None)
        tags = []
        for f in (5_745_000_000, 5_760_000_000):
            src.set_frequency(f)
            iq = src.read_iq(20_000)
            tags.append(sorted(set(np.round(iq[:, 0] * 127).astype(int).tolist())))
        src.close()
        event = json.loads(%r)
        checks = (cls.__name__, tags, a.dwell_ms)
        if checks != ("RfSentinelIqSource", [[45], [60]], 30.0):  # 5745 then 5760 MHz, settled; tuning file applied
            event["track_id"] = "fail: " + repr(checks)
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(json.dumps(event).encode(), ("127.0.0.1", a.collector_port))
        import time; time.sleep(1.0)
        return 0
''') % json.dumps(DETECTION)


class EventMappingTest(unittest.TestCase):
    def test_confirmed_and_unconfirmed_stay_distinct(self):
        d = to_detection(DETECTION)
        self.assertEqual((d["kind"], d["key"], d["level_db"]), ("drone_detection", "trk-1", 14.5))
        self.assertEqual(d["identity"], "Drone link (confirmed): dji_ocusync, 5.8 GHz")
        link = to_detection({"event_type": "passive_shield.station.link_occupancy", "frequency_hz": 2_421_165_000.0,
                             "radio_band": "ism_2g4", "confidence": 0.4})
        self.assertEqual(link["kind"], "drone_link")
        self.assertEqual(link["identity"], "Drone-like link (not confirmed): drone, 2.4 GHz")
        self.assertEqual(link["key"], "drone_link:ism_2g4:2421165000")
        self.assertIsNone(to_detection({"schema": "passive_shield.live_hop_channel_metrics.v1"}))


class ServiceThread:
    """The real IQ service (fake radio) on a local port, for clients that speak HTTP."""

    def __init__(self):
        from werkzeug.serving import make_server
        self.backend = SoapyBackend(TaggingSoapy)
        self.server = make_server("127.0.0.1", 0, create_app(self.backend, token="tok"), threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.backend.stop_all()


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.service = ServiceThread()

    def tearDown(self):
        self.service.close()

    def test_reads_settled_blocks_and_releases_the_receiver(self):
        src = RfSentinelIqSource(device_args="driver=rfsentinel,device_id=bladerf:0,settle_us=15000", sample_rate=2e6,
                                 freq=5_745_000_000, gain=40, base_url=self.service.url, token="tok")
        src.set_frequency(5_760_000_000)
        iq = src.read_iq(30_000)
        self.assertEqual((iq.shape, iq.dtype), ((30_000, 2), np.float32))
        self.assertEqual(set(np.round(iq[:, 0] * 127).astype(int).tolist()), {60})  # only 5760 MHz samples
        src.close()
        self.assertEqual(self.service.backend.list("stream"), [])

    def test_needs_a_receiver_id(self):
        with self.assertRaises(ValueError):
            RfSentinelIqSource(device_args="driver=rfsentinel", sample_rate=2e6, freq=5.7e9, base_url=self.service.url)


class ShimTest(unittest.TestCase):
    def test_only_rfsentinel_devices_are_redirected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "det.py"
            path.write_text(STAND_IN)
            module = shim.load(str(path))
            shim.install(module)
            self.assertIs(module.source_class_for_device("driver=rfsentinel,device_id=bladerf:0"), RfSentinelIqSource)
            self.assertEqual(module.source_class_for_device("driver=bladerf,serial=abc").__name__, "OwnSource")


class ListenTest(unittest.TestCase):
    def setUp(self):
        self.service = ServiceThread()
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = Path(self.tmp.name) / "detector.py"
        self.detector.write_text(STAND_IN)
        (Path(self.tmp.name) / "detector.args").write_text("# station tuning\n--dwell-ms 30\n")

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def listen(self, *extra):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(PLUGIN_SRC), str(Path(__file__).resolve().parents[1])]))
        return subprocess.run([sys.executable, "-m", "drone_rf.cli", "listen", "--base-url", self.service.url,
                               "--token", "tok", "--device-id", "bladerf:0", "--settle-us", "15000",
                               "--detector", str(self.detector), "--duration-s", "20", *extra],
                              capture_output=True, text=True, timeout=60, env=env)

    def test_detector_reads_through_the_service_and_its_event_becomes_a_detection(self):
        result = self.listen()
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        self.assertEqual(lines[0]["kind"], "drone_detection")
        self.assertEqual(lines[0]["key"], "trk-1")
        self.assertEqual(self.service.backend.list("stream"), [])  # the detector closed its stream

    def test_missing_detector_is_a_plain_error(self):
        result = subprocess.run([sys.executable, "-m", "drone_rf.cli", "listen", "--device-id", "bladerf:0",
                                 "--detector", "/nonexistent/detector.py"], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PYTHONPATH=str(PLUGIN_SRC), DRONE_RF_DETECTOR=""))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "drone detector not installed")


if __name__ == "__main__":
    unittest.main()


class AgentDroneTest(unittest.TestCase):
    """Drone requests through the planner, readiness, and a simulated mission (no detector needed)."""

    def plan(self, text):
        from rf_platform.agent.playbook import plan_any
        from rf_platform.agent.workflow import WorkflowSpec
        return WorkflowSpec(**plan_any(text, planner="deterministic")["spec"])

    def test_planning(self):
        spec = self.plan("look for drones for 5 minutes and alert me when a drone is confirmed")
        self.assertEqual((spec.collect, spec.duration_s, spec.command()), (["drone"], 300, "find drones for 300 seconds"))
        self.assertEqual([c.model_dump(include={"field", "op", "value"}) for c in spec.triggers[0].when],
                         [{"field": "device_class", "op": "eq", "value": "drone"}])
        self.assertEqual(self.plan("find drone controllers for 60 seconds").command(),
                         "find drone controllers for 60 seconds")
        from rf_platform.agent.profiles import drone_band
        self.assertEqual((drone_band("find drones"), drone_band("find drone controllers")), ("5g8", "2g4"))

    def test_drones_do_not_silently_share_a_step_with_another_radio_protocol(self):
        with self.assertRaisesRegex(ValueError, "one after the other"):
            self.plan("watch for drones and trackers for 60 seconds")

    def test_confirmed_drone_stays_confirmed(self):
        from rf_platform.agent.core import Agent
        from rf_platform.agent.profiles import PROFILES
        detections = {}
        link = {"kind": "drone_link", "key": "t1", "identity": "Drone-like link (not confirmed): drone, 5.8 GHz",
                "device_type": "drone-like link", "confidence": 0.4}
        confirmed = {**link, "kind": "drone_detection", "identity": "Drone link (confirmed): dji_ocusync, 5.8 GHz",
                     "device_type": "confirmed drone", "confidence": 0.8}
        for event in (link, confirmed, link):
            Agent._record(detections, PROFILES["drone"], dict(event), 40, None)
        self.assertEqual((detections["t1"]["device_type"], detections["t1"]["count"]), ("confirmed drone", 3))

    def test_readiness_names_the_missing_detector(self):
        from rf_platform.agent.readiness import drone_capability
        self.assertEqual(drone_capability(None)[0], "fail")
        with unittest.mock.patch.dict(os.environ, {"DRONE_RF_DETECTOR": "/nonexistent"}):
            status, detail = drone_capability("/usr/bin/true")
        if not Path("/opt/passive-shield/live_multiband_hop_detector_sender.py").is_file():
            self.assertEqual(status, "fail")
            self.assertIn("does not include one", detail)

    def test_simulated_mission_alerts_only_on_the_confirmed_track(self):
        from rf_platform.agent.core import Agent
        from rf_platform.agent.workflow import run_workflow
        steps = []
        out = run_workflow(Agent(simulate=True, mode="passive-auto"),
                           self.plan("look for drones for 12 seconds and alert me when a drone is confirmed"),
                           on_step=steps.append)
        types = {d["key"]: d.get("device_type") for d in out["devices"]}
        self.assertEqual(types.get("sim-drone-track-1"), "confirmed drone")
        alerts = [s for s in steps if s.startswith("ALERT")]
        self.assertEqual(len(alerts), 1, steps)
        self.assertIn("Drone link (confirmed)", alerts[0])


class DroneGainBandTest(unittest.TestCase):
    def test_gain_is_measured_in_the_band_the_detector_listens_to(self):
        from rf_platform.agent.core import drone_profile_for
        from rf_platform.agent.profiles import PROFILES
        self.assertEqual(drone_profile_for(PROFILES["drone"], "find drones").freqs_hz[0], 5_745_500_000)
        self.assertEqual(drone_profile_for(PROFILES["drone"], "find drone controllers for 60 seconds").freqs_hz[0],
                         2_405_500_000)


class ReceiverCardTest(unittest.TestCase):
    """The dashboard says which radio a running mission holds (receiver cards), from the mission's own steps."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend = BackendValidationTest, BackendValidationTest.backend

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_running_mission_receivers(self):
        b = self.backend
        with b.agent_jobs_lock:
            b.agent_jobs.clear()
            b.agent_jobs["run-x"] = {"job_id": "run-x", "status": "running", "summary": {"find": "Drone RF for 60 s"},
                                     "requested_by": "ops", "steps": [{"message": "Receiver: bladerf:1 (BladeRF #1)"},
                                                                      {"message": "Starting a BLE scan on bladerf:0 for 30 s"}]}
            b.agent_jobs["run-old"] = {"job_id": "run-old", "status": "done", "steps": [{"message": "Receiver: hackrf:0"}]}
        try:
            held = b._agent_receivers()
        finally:
            with b.agent_jobs_lock:
                b.agent_jobs.clear()
        self.assertEqual(sorted(held), ["bladerf:0", "bladerf:1"])
        self.assertEqual(held["bladerf:1"]["find"], "Drone RF for 60 s")
