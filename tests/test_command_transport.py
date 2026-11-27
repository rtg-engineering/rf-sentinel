"""Command transport against a real Mosquitto broker (TLS, client certificates, per-station ACL).

Skipped when mosquitto, openssl or paho-mqtt is not installed. Covers the FOC-002 acceptance points:
a station cannot publish as another station or send missions; expired and replayed missions are rejected;
redelivered envelopes do not duplicate; nothing connects without a site certificate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

from rf_platform.command import broker, envelope as E, pki
from rf_platform.command.topics import station_topic
from rf_platform.command.transport import Hub, StationLink, TLSFiles

SITE = "lab"
A = {"id": "node-a/x86_64", "hostname": "node-a", "arch": "x86_64"}
B = {"id": "node-b/aarch64", "hostname": "node-b", "arch": "aarch64"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@unittest.skipUnless(mqtt and shutil.which("mosquitto") and shutil.which("openssl"), "needs mosquitto, openssl, paho-mqtt")
class TransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.pki = Path(cls.tmp.name)
        pki.init(d, ["localhost", "127.0.0.1"])
        pki.issue_hub(d)
        pki.issue_station(d, "node-a", "x86_64")
        pki.issue_station(d, "node-b", "aarch64")
        cls.port = _free_port()
        conf = broker.write(d, cls.port, "127.0.0.1")
        cls.broker = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def listening():
            with socket.socket() as s:
                return s.connect_ex(("127.0.0.1", cls.port)) == 0
        assert wait_for(listening, 5), "broker did not start"

    @classmethod
    def tearDownClass(cls):
        cls.broker.terminate()
        cls.broker.wait(5)
        cls.tmp.cleanup()

    def tls(self, name):
        return TLSFiles(self.pki / "ca.crt", self.pki / f"{name}.crt", self.pki / f"{name}.key")

    def hub(self):
        hub = Hub("127.0.0.1", self.port, self.tls("hub"), site=SITE)
        self.assertTrue(hub.start())
        self.addCleanup(hub.stop)
        return hub

    def station(self, ident, on_mission=None):
        name = f"station-{ident['hostname']}-{ident['arch']}"
        link = StationLink("127.0.0.1", self.port, self.tls(name), SITE, station=ident, on_mission=on_mission)
        self.assertTrue(link.start())
        self.addCleanup(link.stop)
        return link

    def test_observation_reaches_the_hub_with_its_receive_time(self):
        hub = self.hub()
        a = self.station(A)
        env = a.publish("observation", {"protocol": "ble", "device_id": "AA:BB"}, clock={"synchronized": True, "source": "t"})
        self.assertTrue(wait_for(lambda: any(m["envelope"]["id"] == env["id"] for m in hub.messages)))
        record = next(m for m in hub.messages if m["envelope"]["id"] == env["id"])
        self.assertEqual(record["envelope"]["station"]["id"], "node-a/x86_64")
        self.assertTrue(record["received_at"].endswith("+00:00"))

    def test_redelivered_envelope_is_kept_once(self):
        hub = self.hub()
        a = self.station(A)
        env = E.make("observation", {"protocol": "ble", "device_id": "CC:DD"}, station=A)
        a.publish_envelope(env)
        a.publish_envelope(env)  # e.g. resent after a reconnect
        self.assertTrue(wait_for(lambda: hub.dropped["duplicate"] >= 1))
        self.assertEqual(sum(m["envelope"]["id"] == env["id"] for m in hub.messages), 1)

    def test_a_station_cannot_publish_as_another_station(self):
        hub = self.hub()
        a = self.station(A)
        # 1. Station A's certificate, station B's topic: the broker drops it (ACL).
        forged = E.make("observation", {"protocol": "ble", "device_id": "EE:FF"}, station=B)
        a.client.publish(station_topic(SITE, B["id"], "observation"), json.dumps(forged), qos=1)
        # 2. Station A's topic, an envelope claiming station B: the hub drops it (topic mismatch).
        a.client.publish(station_topic(SITE, A["id"], "observation"), json.dumps(forged), qos=1)
        self.assertTrue(wait_for(lambda: hub.dropped["topic_mismatch"] >= 1))
        time.sleep(0.5)
        self.assertFalse(any(m["envelope"]["id"] == forged["id"] for m in hub.messages))
        with self.assertRaises(ValueError):
            a.publish_envelope(forged)  # the link itself refuses, too

    def test_a_station_cannot_send_missions(self):
        got = []
        self.station(B, on_mission=lambda env: got.append(env) or None)
        a = self.station(A)
        mission = E.make("mission_request", {"command": "find bluetooth devices"}, station=A, ttl_s=60,
                         idempotency_key="forged-1")
        a.client.publish(station_topic(SITE, B["id"], "mission_request"), json.dumps(mission), qos=1)
        time.sleep(1.0)
        self.assertEqual(got, [])  # ACL: only the hub writes mission_request

    def test_missions_are_acknowledged_and_expired_or_replayed_ones_rejected(self):
        hub = self.hub()
        ran = []
        self.station(A, on_mission=lambda env: ran.append(env["idempotency_key"]) or "queued")
        hub.send_mission(SITE, A["id"], {"command": "find bluetooth devices for 30 seconds"}, ttl_s=60,
                         idempotency_key="cmd-1")
        hub.send_mission(SITE, A["id"], {"command": "find bluetooth devices for 30 seconds"}, ttl_s=60,
                         idempotency_key="cmd-1")  # replay
        stale = E.make("mission_request", {"command": "x"}, ttl_s=5, idempotency_key="cmd-old",
                       source_time=datetime.now(timezone.utc) - timedelta(minutes=5))
        hub.client.publish(station_topic(SITE, A["id"], "mission_request"), json.dumps(stale), qos=1)

        def acks():
            return {m["envelope"]["payload"]["outcome"] for m in hub.messages if m["envelope"]["kind"] == "mission_ack"}
        self.assertTrue(wait_for(lambda: len(acks()) >= 3))
        self.assertEqual(ran, ["cmd-1"])  # ran once
        self.assertIn("accepted: queued", acks())
        self.assertTrue(any(a.startswith("rejected: duplicate of cmd-1") for a in acks()))
        self.assertIn("rejected: expired before it arrived", acks())

    def test_health_online_then_offline_on_disconnect(self):
        hub = self.hub()
        b = self.station(B)
        b.publish("heartbeat", {"status": "online"})
        self.assertTrue(wait_for(lambda: [s["status"] for s in hub.health() if s["station_id"] == B["id"]] == ["online"]))
        b.client.socket().close()  # unclean loss: the broker publishes the station's last will
        self.assertTrue(wait_for(lambda: [s["status"] for s in hub.health() if s["station_id"] == B["id"]] == ["offline"], 15))
        stale = hub.health(now=time.time() + 3600)
        self.assertIn(next(s["status"] for s in stale if s["station_id"] == B["id"]), ("offline", "stale"))

    def test_nothing_connects_without_a_site_certificate(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="intruder")
        client.tls_set(ca_certs=str(self.pki / "ca.crt"), cert_reqs=ssl.CERT_REQUIRED)
        with self.assertRaises(Exception):
            client.connect("127.0.0.1", self.port, keepalive=5)
            client.loop(timeout=2)
            client.loop(timeout=2)
            if not client.is_connected():
                raise ConnectionError("refused")

    def test_dashboard_station_and_hub_modes(self):
        from rf_platform.command.mode import CommandMode
        pki.issue_station(self.pki)  # this machine's own <hostname>/<arch>
        env = {"RF_SENTINEL_COMMAND_PKI": str(self.pki), "RF_SENTINEL_MQTT_HOST": "127.0.0.1",
               "RF_SENTINEL_MQTT_PORT": str(self.port), "RF_SENTINEL_SITE": SITE}
        self.assertIsNone(CommandMode.from_env(env={}))
        hub = CommandMode.from_env(env={**env, "RF_SENTINEL_COMMAND": "hub"})
        station = CommandMode.from_env(capabilities=lambda: {"receivers": [{"id": "sim:0"}]},
                                       env={**env, "RF_SENTINEL_COMMAND": "station"})
        hub.start()
        station.start()
        self.addCleanup(hub.stop)
        self.addCleanup(station.stop)
        me = E.station_identity()["id"]
        station.observation({"kind": "ble_adv", "agent_key": "AA:BB", "rssi_dbfs": -60.5, "iq_b64": "AAAA"})
        self.assertTrue(wait_for(lambda: any(m["envelope"]["kind"] == "observation" and m["envelope"]["station"]["id"] == me
                                             for m in hub.recent())))
        obs = next(m for m in hub.recent() if m["envelope"]["kind"] == "observation")
        self.assertNotIn("iq_b64", obs["envelope"]["payload"])  # a bounded summary, never IQ
        self.assertTrue(wait_for(lambda: any(s["station_id"] == me and s["status"] == "online"
                                             and s["capabilities"].get("receivers") for s in hub.status()["stations"])))
        hub.link.send_mission(SITE, me, {"command": "find bluetooth devices"}, ttl_s=60, idempotency_key="cmd-x")
        self.assertTrue(wait_for(lambda: station.status()["missions"]))
        ack = lambda: [m for m in hub.recent() if m["envelope"]["kind"] == "mission_ack"]  # noqa: E731
        self.assertTrue(wait_for(lambda: ack()))
        self.assertIn("not run", ack()[-1]["envelope"]["payload"]["outcome"])

    def test_identity_must_match_the_certificate(self):
        with self.assertRaisesRegex(ValueError, "certificate is for"):
            StationLink("127.0.0.1", self.port, self.tls("station-node-a-x86_64"), SITE, station=B)


if __name__ == "__main__":
    unittest.main()
