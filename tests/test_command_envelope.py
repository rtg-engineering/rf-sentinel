"""Command envelope v1: station identity (hostname + arch), dedup ids, strict validation, TTL, no IQ."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from rf_platform.command import envelope as E
from rf_platform.events import RFEvent

STATION = E.station_identity("Node-7.lab.example", "AMD64")
CLOCK = {"synchronized": True, "source": "test"}


class IdentityTest(unittest.TestCase):
    def test_hostname_and_arch(self):
        self.assertEqual(STATION, {"id": "node-7/x86_64", "hostname": "node-7", "arch": "x86_64"})
        self.assertEqual(E.station_identity("pi", "arm64")["id"], "pi/aarch64")
        with self.assertRaises(E.EnvelopeError):
            E.station_identity("bad host!", "x86_64")

    def test_this_machine_has_an_identity(self):
        ident = E.station_identity()
        self.assertEqual(ident["id"], f"{ident['hostname']}/{ident['arch']}")


class EnvelopeTest(unittest.TestCase):
    def env(self, kind="observation", payload=None, **kw):
        return E.make(kind, payload if payload is not None else {"protocol": "ble"}, station=STATION, clock=CLOCK, **kw)

    def test_observation_from_an_rf_event_keeps_its_time_and_dedups(self):
        ev = RFEvent(protocol="ble", subtype="adv", timestamp="2026-11-27T21:40:02.512+00:00", device_id="AA:BB")
        a, b = E.observation(ev, station=STATION, clock=CLOCK), E.observation(ev, station=STATION, clock=CLOCK)
        self.assertEqual(a["source_time"], "2026-11-27T21:40:02.512+00:00")
        self.assertEqual(a["id"], b["id"])  # the same logical event: same id, so the hub drops the duplicate
        self.assertNotEqual(a["seq"], b["seq"])
        other = E.observation(RFEvent(protocol="ble", subtype="adv", timestamp=ev.timestamp, device_id="CC:DD"),
                              station=STATION, clock=CLOCK)
        self.assertNotEqual(a["id"], other["id"])

    def test_mission_messages_need_ttl_and_idempotency(self):
        req = self.env("mission_request", {"command": "watch for drones for 60 seconds"}, ttl_s=120,
                       idempotency_key="cmd-1")
        again = self.env("mission_request", {"command": "watch for drones for 60 seconds"}, ttl_s=120,
                         idempotency_key="cmd-1")
        self.assertEqual(req["id"], again["id"])  # a re-sent command is the same command
        with self.assertRaisesRegex(E.EnvelopeError, "ttl_s"):
            self.env("mission_request", {"command": "x"})
        self.assertTrue(self.env("mission_ack", {})["idempotency_key"])  # generated when not given

    def test_expiry_is_measured_from_source_time(self):
        start = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        req = self.env("mission_request", {"command": "x"}, ttl_s=60, source_time=start, idempotency_key="c")
        self.assertFalse(E.expired(req, start + timedelta(seconds=59)))
        self.assertTrue(E.expired(req, start + timedelta(seconds=61)))
        self.assertFalse(E.expired(self.env()))

    def test_strict_validation(self):
        good = self.env()
        cases = {
            "unknown fields": {**good, "extra": 1},
            "missing fields": {k: v for k, v in good.items() if k != "clock"},
            "unsupported schema": {**good, "schema": "rf-sentinel.command.envelope/2"},
            "unknown kind": {**good, "kind": "shell"},
            "station id must be": {**good, "station": {**good["station"], "id": "someone-else/x86_64"}},
            "clock must be": {**good, "clock": {"synchronized": "yes", "source": "x"}},
            "IQ samples never travel": {**good, "payload": {"evidence": {"iq_b64": "AAAA"}}},
            "larger than": {**good, "payload": {"blob": "x" * (E.MAX_PAYLOAD_BYTES + 1)}},
            "time zone": {**good, "source_time": "2026-11-27T21:40:02"},
        }
        for message, env in cases.items():
            with self.subTest(message), self.assertRaisesRegex(E.EnvelopeError, message):
                E.validate(env)
        E.validate(json.loads(json.dumps(good)))  # survives a JSON round trip

    def test_published_schema_matches_the_code(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/command/schemas/envelope-v1.schema.json").read_text())
        self.assertEqual(schema["properties"]["schema"]["const"], E.SCHEMA)
        self.assertEqual(tuple(schema["required"]), E.FIELDS)
        self.assertEqual(tuple(schema["properties"]["kind"]["enum"]), E.KINDS)


if __name__ == "__main__":
    unittest.main()
