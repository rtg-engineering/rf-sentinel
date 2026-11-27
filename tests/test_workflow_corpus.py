"""Every phrase in tests/fixtures/workflow_phrases.json must plan exactly as reviewed."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from rf_platform.agent.workflow import plan_deterministic

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "workflow_phrases.json").read_text())["cases"]


def shape(spec) -> dict:
    return {"collect": spec.collect, "duration_s": spec.duration_s, "device_id": spec.device_id, "channel": spec.channel,
            "filters": sorted([c.field, c.op, c.value] for c in spec.filters),
            "triggers": [{"when": sorted([c.field, c.op, c.value] for c in t.when), "do": [a.kind for a in t.do],
                          "severity": next((a.severity for a in t.do if a.kind == "alert"), None), "repeat": t.repeat}
                         for t in spec.triggers]}


class CorpusTest(unittest.TestCase):
    def test_rules_planner_matches_reviewed_plans(self):
        for case in CORPUS:
            with self.subTest(case["text"]):
                if case.get("refused"):
                    with self.assertRaises(ValueError):
                        plan_deterministic(case["text"])
                    continue
                expected = {k: case[k] for k in ("collect", "duration_s", "device_id", "channel", "filters", "triggers")}
                self.assertEqual(json.loads(json.dumps(shape(plan_deterministic(case["text"])))), expected)


if __name__ == "__main__":
    unittest.main()
