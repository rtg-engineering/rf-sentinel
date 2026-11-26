"""Built-in SoapySDR IQ service, tested against a fake SoapySDR module (no radio required)."""
from __future__ import annotations

import base64
import os
import threading
import time
import unittest

import numpy as np

from rf_platform.soapy_gateway.backend import DeviceBusy, SoapyBackend, cf32_to_cs8
from rf_platform.soapy_gateway.server import create_app

TONE_OFFSET_HZ = 250_000


class _Range:
    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi

    def minimum(self):
        return self.lo

    def maximum(self):
        return self.hi


class _Result:
    def __init__(self, ret):
        self.ret = ret


class FakeDevice:
    opened = 0

    def __init__(self, kwargs):
        FakeDevice.opened += 1
        self.kwargs, self.rate, self.freq, self.gain, self.agc = kwargs, 2e6, 0.0, 0.0, True
        self.phase = 0.0

    def getFrequencyRange(self, d, c):
        return [_Range(70e6, 6e9)]

    def getSampleRateRange(self, d, c):
        return [_Range(0.5e6, 61.44e6)]

    def listGains(self, d, c):
        return ["full"]

    def getGainRange(self, d, c, name=None):
        return _Range(-15, 60)

    def hasGainMode(self, d, c):
        return True

    def setGainMode(self, d, c, automatic):
        self.agc = automatic

    def setGain(self, d, c, *args):
        self.gain = float(args[-1])

    def setSampleRate(self, d, c, rate):
        self.rate = rate

    def setFrequency(self, d, c, freq):
        self.freq = freq

    def setBandwidth(self, d, c, bw):
        pass

    def setupStream(self, d, fmt, chans):
        return object()

    def activateStream(self, s):
        pass

    def deactivateStream(self, s):
        pass

    def closeStream(self, s):
        pass

    def readStream(self, s, bufs, n, timeoutUs=0):
        n = min(n, 8192)
        t = (np.arange(n) + self.phase) / self.rate
        amplitude = 10 ** ((self.gain - 60) / 20)  # louder with more gain
        bufs[0][:n] = (amplitude * np.exp(2j * np.pi * TONE_OFFSET_HZ * t)).astype(np.complex64)
        self.phase += n
        time.sleep(n / self.rate / 8)  # faster than real time, but not a busy loop
        return _Result(n)


class FakeSoapy:
    SOAPY_SDR_RX = 1
    SOAPY_SDR_CF32 = "CF32"

    class Device(FakeDevice):
        @staticmethod
        def enumerate():
            return [{"driver": "audio", "label": "Dummy"},
                    {"driver": "bladerf", "serial": "aaa", "label": "BladeRF #0"},
                    {"driver": "bladerf", "serial": "bbb", "label": "BladeRF #1"}]


class OverflowingSoapy(FakeSoapy):
    class Device(FakeSoapy.Device):
        calls = 0

        def readStream(self, s, bufs, n, timeoutUs=0):
            type(self).calls += 1
            if type(self).calls % 2 == 0:
                return _Result(-4)  # SOAPY_SDR_OVERFLOW: samples were lost
            return super().readStream(s, bufs, n, timeoutUs)


class ReceiverInstrumentationTest(unittest.TestCase):
    def test_overflows_and_samples_are_counted(self):
        backend = SoapyBackend(OverflowingSoapy)
        try:
            self.assertFalse(backend.native_bladerf)  # an injected SoapySDR never opens real hardware
            session = backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000,
                                            "sample_rate_sps": 2_000_000})
            deadline = time.time() + 5
            while session.worker.samples_received < 200_000 and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(session.worker.samples_received, 131_072)
            self.assertGreater(session.worker.overflows, 0)
            self.assertEqual(session.worker.driver_path, "soapysdr")
        finally:
            backend.stop_all()

    def test_native_bladerf_converts_sc16_to_cf32(self):
        from rf_platform.soapy_gateway import bladerf_native

        dev = bladerf_native.NativeBladeRF.__new__(bladerf_native.NativeBladeRF)
        dev._scratch = np.zeros(2 * bladerf_native._READ_SAMPLES, dtype=np.int16)
        dev._dev, dev._streaming = None, False

        class Lib:
            @staticmethod
            def bladerf_sync_rx(_dev, _ptr, n, _meta, _timeout):
                dev._scratch[: 2 * n: 2] = 1024    # I = +0.5 full scale
                dev._scratch[1: 2 * n: 2] = -2048  # Q = -1.0 full scale
                return 0
        dev._lib = Lib()
        out = np.zeros(100, dtype=np.complex64)
        self.assertEqual(dev.readStream(None, [out], 100).ret, 100)
        self.assertTrue(np.allclose(out, 0.5 - 1.0j))
        Lib.bladerf_sync_rx = staticmethod(lambda *a: -6)
        self.assertEqual(dev.readStream(None, [out], 100).ret, -1)  # libbladeRF timeout -> SoapySDR timeout


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = SoapyBackend(FakeSoapy)

    def tearDown(self):
        self.backend.stop_all()

    def test_devices_exclude_audio_and_report_driver_ranges(self):
        devices = self.backend.devices()
        self.assertEqual([d["id"] for d in devices], ["bladerf:0", "bladerf:1"])
        self.assertEqual((devices[0]["freq_min_hz"], devices[0]["max_sample_rate_sps"]), (70_000_000, 61_440_000))
        self.assertEqual(devices[0]["notes"], "ranges reported by the driver")
        self.assertEqual([d["id"] for d in SoapyBackend(FakeSoapy, include_non_rf=True).devices()][0], "audio:0")

    def test_stream_delivers_cs8_with_manual_gain(self):
        session = self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000,
                                             "sample_rate_sps": 2_000_000, "lna_gain_db": 0, "vga_gain_db": 54})
        cursor, chunk = session.worker.read(session.worker.cursor(), timeout_s=5)
        self.assertGreater(len(chunk), 100_000)
        iq = np.frombuffer(chunk, dtype=np.int8).astype(float)
        amplitude = np.sqrt(np.mean(iq[0::2] ** 2 + iq[1::2] ** 2))
        self.assertAlmostEqual(amplitude, 127 * 10 ** (-6 / 20), delta=3)  # 54 dB gain -> -6 dBFS tone
        self.assertEqual(self.backend._handles["bladerf:0"].agc, False)

    def test_one_owner_per_device_and_release(self):
        first = self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 2_000_000})
        with self.assertRaises(DeviceBusy):
            self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_426_000_000, "sample_rate_sps": 2_000_000})
        second = self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_426_000_000,
                                            "sample_rate_sps": 2_000_000, "replace_existing": True})
        self.assertTrue(first.extra.get("stopped"))
        self.backend.stop(second.id)
        self.assertFalse(any(d["occupied"] for d in self.backend.devices()))

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            self.backend.start_stream({"device_id": "bladerf:1", "center_freq_hz": 10_000_000, "sample_rate_sps": 2_000_000})
        with self.assertRaises(ValueError):
            self.backend.start_stream({"device_id": "bladerf:1", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 100_000_000})

    def test_device_handle_is_reused(self):
        self.backend.devices()  # describing devices opens each once to query its ranges
        FakeDevice.opened = 0
        for _ in range(3):
            s = self.backend.start_stream({"device_id": "bladerf:1", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 2_000_000})
            s.worker.read(s.worker.cursor(), timeout_s=5)
            self.backend.stop(s.id)
        self.assertEqual(FakeDevice.opened, 0)  # streams reuse the open handle

    def test_power_sweep_finds_tone(self):
        session = self.backend.start_sweep({"device_id": "bladerf:0", "start_freq_hz": 2_400_000_000,
                                            "stop_freq_hz": 2_410_000_000, "bin_width_hz": 50_000, "vga_gain_db": 50})
        deadline = time.time() + 10
        while not session.extra["samples"] and time.time() < deadline:
            time.sleep(0.1)
        row = session.extra["samples"][0]
        freqs = np.linspace(row["hz_low"], row["hz_high"], len(row["db_values"]))
        peak = freqs[int(np.argmax(row["db_values"]))]
        center = session.extra["centers"][0]
        self.assertAlmostEqual(peak - center, TONE_OFFSET_HZ, delta=100_000)

    def test_iq_sweep_hops_all_centers(self):
        centers = [2_402_000_000, 2_426_000_000, 2_480_000_000]
        session = self.backend.start_iq_sweep({"device_id": "bladerf:1", "center_freqs_hz": centers,
                                               "sample_rate_sps": 2_000_000, "dwell_s": 0.1})
        seen = set()
        deadline = time.time() + 10
        while seen != set(centers) and time.time() < deadline:
            chunk = self.backend.iq_sweep_chunk(session.id, 65536)
            self.assertGreater(chunk["byte_count"], 0)
            self.assertEqual(len(base64.b64decode(chunk["iq_i8_b64"])), chunk["byte_count"])
            seen.add(chunk["center_freq_hz"])
        self.assertEqual(seen, set(centers))

    def test_dead_device_fails_stream_and_drops_handle(self):
        from unittest.mock import patch
        from rf_platform.soapy_gateway import backend as backend_module

        class BrokenDevice(FakeDevice):
            def readStream(self, s, bufs, n, timeoutUs=0):
                time.sleep(0.01)
                return _Result(-2)  # SOAPY_SDR_STREAM_ERROR, e.g. a USB I/O failure

        self.backend.devices()
        self.backend._handles["bladerf:0"] = BrokenDevice({})
        with patch.object(backend_module, "NO_DATA_TIMEOUT_S", 0.3):
            session = self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000,
                                                 "sample_rate_sps": 2_000_000})
            deadline = time.time() + 5
            while session.worker.status != "error" and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(session.worker.status, "error")
        self.assertIn("driver read error -2", session.worker.error)
        self.assertNotIn("bladerf:0", self.backend._handles)  # next stream reopens the device
        session.worker._thread.join(2)
        # The failed session no longer owns the receiver: a new stream starts without replace_existing.
        recovered = self.backend.start_stream({"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000,
                                               "sample_rate_sps": 2_000_000})
        deadline = time.time() + 5
        while recovered.worker.samples_received == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreater(recovered.worker.samples_received, 0)
        self.assertEqual(session.status, "stopped")
        self.assertEqual(self.backend.devices()[0]["occupied_id"], recovered.id)

    def test_cs8_conversion_clips_and_scales(self):
        out = np.frombuffer(cf32_to_cs8(np.array([1 + 1j, -2 - 0.5j, 0.5j], dtype=np.complex64)), dtype=np.int8)
        self.assertEqual(out.tolist(), [127, 127, -128, -64, 0, 64])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.backend = SoapyBackend(FakeSoapy)
        self.client = create_app(self.backend, token="secret").test_client()
        self.auth = {"Authorization": "Bearer secret"}

    def tearDown(self):
        self.backend.stop_all()

    def test_auth_and_gateway_shapes(self):
        self.assertEqual(self.client.get("/devices").status_code, 401)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/devices?token=secret").status_code, 200)
        start = self.client.post("/streams/start", headers=self.auth,
                                 json={"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 2_000_000})
        state = start.get_json()
        self.assertEqual(set(state), {"stream_id", "status", "config", "receiver"})
        self.assertEqual(set(state["receiver"]), {"driver_path", "samples_received", "overflows"})
        busy = self.client.post("/streams/start", headers=self.auth,
                                json={"device_id": "bladerf:0", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 2_000_000})
        self.assertEqual(busy.status_code, 409)
        self.assertIn("detail", busy.get_json())
        retuned = self.client.post(f"/streams/{state['stream_id']}/retune", headers=self.auth,
                                   json={"device_id": "bladerf:0", "center_freq_hz": 2_426_000_000, "sample_rate_sps": 2_000_000})
        self.assertEqual(retuned.get_json()["config"]["center_freq_hz"], 2_426_000_000)
        self.assertEqual(self.client.post("/streams/nope/stop", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.post(f"/streams/{state['stream_id']}/stop", headers=self.auth).get_json(), {"ok": True})
        self.assertEqual(self.client.get("/streams", headers=self.auth).get_json(), [])
        self.assertEqual(self.client.get("/wifi/interfaces", headers=self.auth).get_json(), [])

    def test_planned_sweep_clips_to_device_range(self):
        plan = self.client.post("/sweeps/plan/start", headers=self.auth,
                                json={"device_id": "bladerf:1", "frequencies_hz": [2_402_000_000, 2_480_000_000],
                                      "margin_hz": 2_000_000, "label": "ble"}).get_json()
        self.assertEqual((plan["config"]["start_freq_hz"], plan["config"]["stop_freq_hz"], plan["label"]),
                         (2_400_000_000, 2_482_000_000, "ble"))
        self.assertEqual(self.client.post(f"/sweeps/{plan['sweep_id']}/stop", headers=self.auth).status_code, 200)

    def test_websocket_streams_binary_iq(self):
        import websocket
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", 0, create_app(self.backend, token=""), threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            session = self.backend.start_stream({"device_id": "bladerf:1", "center_freq_hz": 2_402_000_000, "sample_rate_sps": 2_000_000})
            ws = websocket.create_connection(f"ws://127.0.0.1:{server.server_port}/ws/iq/{session.id}?keep=1", timeout=10)
            payload = ws.recv()
            ws.close()
            self.assertIsInstance(payload, bytes)
            self.assertGreater(len(payload), 100_000)
            time.sleep(0.2)
            self.assertIn(session.id, [s.id for s in self.backend.list("stream")])  # keep=1 leaves it running
        finally:
            server.shutdown()


class BackendSelectionTest(unittest.TestCase):
    def test_precedence(self):
        from unittest.mock import patch
        from rf_platform import sdr_backend
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SDR_BACKEND", None)
            self.assertEqual(sdr_backend.configure(demo_mode=True)["backend"], "demo")
            os.environ["SDR_BACKEND"] = "rfiq"
            self.assertEqual(sdr_backend.configure()["backend"], "rfiq")
            os.environ.pop("SDR_BACKEND")
            os.environ["SDR_GATEWAY_BASE_URL"] = "http://gateway.example:8080"
            result = sdr_backend.configure()
            self.assertEqual((result["backend"], result["gateway_url"]), ("gateway", "http://gateway.example:8080"))


if __name__ == "__main__":
    unittest.main()
