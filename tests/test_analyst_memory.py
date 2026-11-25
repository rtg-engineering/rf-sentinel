"""Phase 3: persistent, append-only environment memory."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from rf_platform.analyst.evidence import Snapshot
from rf_platform.analyst.memory import EnvironmentMemory, MemoryStoreError, assess
from rf_platform.analyst.report import render_html

CHAIN = Path(__file__).parent / "fixtures" / "analyst_memory"
TELEMETRY_AP = "wifi:ap:02:10:7A:00:30:11:Telemetry-GW"


def day(name):
    return Snapshot.from_dict(json.loads((CHAIN / f"{name}.json").read_text()))


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "memory.sqlite"
        self.memory = EnvironmentMemory(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_assessments_chain_from_baseline_then_last(self):
        with self.assertRaisesRegex(MemoryStoreError, "baseline"):
            assess(self.memory, day("day2"))
        self.memory.set_baseline(day("day1"), reason="Initial survey")
        quiet = assess(self.memory, day("day2"))
        self.assertIn("baseline:default:v1", quiet["memory"]["compared_against"])
        self.assertEqual([f["category"] for f in quiet["findings"]], ["collection_quality"])
        outage = assess(self.memory, day("day3"))
        self.assertEqual(outage["memory"]["compared_against"], "last assessment")
        self.assertEqual(outage["scope"]["reference_snapshot_id"], day("day2").snapshot_id)
        missing = [f for f in outage["findings"] if f["category"] == "missing_entity"]
        self.assertEqual(len(missing), 2)
        self.assertTrue(outage["grounding"]["passed"])
        with self.assertRaisesRegex(MemoryStoreError, "Nothing new"):
            assess(self.memory, day("day3"))

    def test_against_baseline_ignores_last_assessment(self):
        self.memory.set_baseline(day("day1"))
        assess(self.memory, day("day2"))
        result = assess(self.memory, day("day3"), against="baseline")
        self.assertEqual(result["scope"]["reference_snapshot_id"], day("day1").snapshot_id)

    def test_stored_assessments_reproduce(self):
        self.memory.set_baseline(day("day1"))
        assess(self.memory, day("day2"))
        assess(self.memory, day("day3"))
        for seq in (1, 2):
            result = self.memory.verify_assessment(seq)
            self.assertTrue(result["reproduced"], result)
            self.assertEqual(result["stored_investigation_id"], result["recomputed_investigation_id"])

    def test_baseline_versions_supersede_and_remain(self):
        first = self.memory.set_baseline(day("day1"), reason="Initial survey")
        second = self.memory.set_baseline(day("day2"), author="analyst-2", reason="Re-baselined after maintenance")
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(second["supersedes"], first["baseline_id"])
        history = self.memory.baseline_history()
        self.assertEqual([b["reason"] for b in history], ["Initial survey", "Re-baselined after maintenance"])
        self.assertEqual(self.memory.active_baseline()["baseline_id"], "baseline:default:v2")

    def test_every_table_is_append_only(self):
        self.memory.set_baseline(day("day1"))
        assess(self.memory, day("day2"))
        self.memory.annotate("note", "checked", entity_key=TELEMETRY_AP)
        db = sqlite3.connect(self.db)
        try:
            for statement in ("UPDATE baselines SET reason = 'tampered'", "DELETE FROM baselines",
                              "UPDATE snapshots SET payload = '{}'", "DELETE FROM snapshots",
                              "UPDATE assessments SET grounding_passed = 0", "DELETE FROM assessments",
                              "UPDATE annotations SET text = 'x'", "DELETE FROM annotations"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    db.execute(statement)
        finally:
            db.close()
        self.assertEqual(self.memory.baseline_history()[0]["reason"], "Baseline set")

    def test_tampered_snapshot_payload_is_rejected_on_read(self):
        snapshot = day("day1")
        payload = snapshot.to_dict()
        payload["status"]["discovery_table"][0]["detections"] += 1
        db = sqlite3.connect(self.db)
        db.execute("INSERT INTO snapshots (snapshot_id, label, captured_at, stored_at, payload) VALUES (?, ?, ?, ?, ?)",
                   (snapshot.snapshot_id, "day1", snapshot.captured_at, "now", json.dumps(payload)))
        db.commit(); db.close()
        with self.assertRaises(ValueError):
            self.memory.snapshot(snapshot.snapshot_id)

    def test_annotations_are_context_not_evidence(self):
        self.memory.set_baseline(day("day1"))
        assess(self.memory, day("day2"))
        note = self.memory.annotate("known_device", "Telemetry gateway, maintenance this week",
                                    author="ops", entity_key=TELEMETRY_AP)
        with self.assertRaises(MemoryStoreError):
            self.memory.annotate("rewrite", "x", entity_key=TELEMETRY_AP)
        with self.assertRaises(MemoryStoreError):
            self.memory.annotate("note", "no target")
        result = assess(self.memory, day("day3"))
        self.assertEqual([n["annotation_id"] for n in result["operator_context"]], [note["annotation_id"]])
        plain = EnvironmentMemory(Path(self.temp.name) / "other.sqlite")
        plain.set_baseline(day("day1"))
        assess(plain, day("day2"))
        unannotated = assess(plain, day("day3"))
        self.assertEqual(result["findings"], unannotated["findings"])  # notes never change findings
        self.assertEqual(result["investigation_id"], unannotated["investigation_id"])
        page = render_html(result)
        self.assertIn("Telemetry gateway, maintenance this week", page)
        self.assertIn("never change evidence or findings", page)

    def test_entity_timeline_and_audit_log(self):
        self.memory.set_baseline(day("day1"))
        assess(self.memory, day("day2"))
        assess(self.memory, day("day3"))
        timeline = self.memory.entity_timeline(TELEMETRY_AP)
        self.assertEqual([t["detections"] for t in timeline], [2, 5, 5])
        tables = [entry["table"] for entry in self.memory.audit_log()]
        self.assertEqual(tables.count("snapshots"), 3)
        self.assertEqual(tables.count("assessments"), 2)
        self.assertEqual(tables.count("baselines"), 1)

    def test_identity_candidate_is_a_hypothesis_not_a_merge(self):
        from rf_platform.analyst.evidence import EvidenceStore
        from rf_platform.analyst.investigate import run_investigation
        fixtures = Path(__file__).parent / "fixtures" / "analyst" / "new_entity"
        store = EvidenceStore([Snapshot.from_dict(json.loads((fixtures / f"{n}.json").read_text())) for n in ("baseline", "current")])
        inv = run_investigation(store)
        new = next(f for f in inv["findings"] if f["category"] == "new_entity")
        identity = next(h for h in new["hypotheses"] if "rotated" in h["statement"])
        self.assertEqual(identity["status"], "open")
        self.assertIn("entity:ble:D0:EF:42:10:AA:01@" + store.order[0], identity["supporting_refs"])
        self.assertIn("not proof of identity", identity["statement"])
        self.assertIn("ble:02:00:00:00:99:01", [f["observed_facts"][0]["value"] for f in inv["findings"] if f["category"] == "new_entity"])


if __name__ == "__main__":
    unittest.main()
