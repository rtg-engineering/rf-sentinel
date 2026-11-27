"""Fleet coordinator: collective decisions from many nodes' sightings, judged against the scenario's ground truth."""
from __future__ import annotations

import unittest

from rf_platform.fleet.coordinator import Coordinator, Node
from rf_platform.fleet.geo import haversine_m, offset, wcl
from rf_platform.fleet.scenario import run


class GeoTest(unittest.TestCase):
    def test_wcl_pulls_toward_the_strongest_observer_and_needs_three(self):
        a, b, c = (40.0, -73.0), offset(40.0, -73.0, 200, 0), offset(40.0, -73.0, 0, 200)
        fix = wcl([(*a, -50), (*b, -80), (*c, -80)])
        self.assertLess(haversine_m(fix["lat"], fix["lon"], *a), 30)
        self.assertIsNone(wcl([(*a, -50), (*b, -80)]))


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run()
        cls.kinds = [(d["kind"], d.get("entity")) for d in cls.result["decisions"]]

    def text(self, kind, entity=None):
        return [d["text"] for d in self.result["decisions"] if d["kind"] == kind and (entity is None or d.get("entity") == entity)]

    def test_cue_then_locate_the_tracker(self):
        self.assertTrue(any("cueing" in t for t in self.text("cue", "tracker-01")))
        cue_t = next(d["t"] for d in self.result["decisions"] if d["kind"] == "cue")
        locate_t = next(d["t"] for d in self.result["decisions"] if d["kind"] == "locate" and d.get("entity") == "tracker-01")
        self.assertGreater(locate_t, cue_t)

    def test_far_towers_are_reported_outside_not_pinned(self):
        for key in ("cell-01", "cell-02", "cell-03"):
            said = self.text("locate", key)
            self.assertTrue(said and "outside the fleet's area" in said[0], (key, said))
            self.assertFalse(any(t.startswith("Located") for t in said), key)

    def test_rogue_cell_alerts_tasks_every_node_and_is_located(self):
        alert = self.text("alert", "cell-rogue")
        self.assertTrue(alert and "Tasking all" in alert[0])
        self.assertTrue(any(t.startswith("Located 2G cell") for t in self.text("locate", "cell-rogue")))

    def test_follow_alert_names_the_right_pair(self):
        follow = [d for d in self.result["decisions"] if d["kind"] == "alert" and d.get("entity") == "tracker-01"]
        self.assertEqual(len(follow), 1)
        self.assertEqual(follow[0]["other"], "phone-15")  # the walker the tracker was scripted to follow

    def test_coverage_moves_when_a_node_drops(self):
        cover = self.text("cover")
        self.assertIn("South went offline", cover[0])
        self.assertIn("back online", cover[1])

    def test_fixes_are_scored_against_truth(self):
        score = self.result["score"]
        self.assertGreater(score["fixes"], 200)
        self.assertLess(score["median_error_m"], 120)
        inside, total = map(int, score["within_uncertainty"].split("/"))
        self.assertGreater(inside / total, 0.6)


class TaskingTest(unittest.TestCase):
    def test_offline_node_hears_nothing_and_cue_skips_it(self):
        nodes = [Node("a", "A", 40.0, -73.0), Node("b", "B", *offset(40.0, -73.0, 100, 0), base=("tpms",)),
                 Node("c", "C", *offset(40.0, -73.0, 0, 100), base=("tpms",), online=False)]
        co = Coordinator(nodes)
        co.tick(0, [{"node": "a", "key": "t1", "label": "Tile", "kind": "tracker", "protocol": "ble", "level": -70},
                    {"node": "c", "key": "t1", "label": "Tile", "kind": "tracker", "protocol": "ble", "level": -60}])
        self.assertEqual(list(co.entities["t1"].heard), ["a"])  # the offline node's report is ignored
        self.assertIn("ble", nodes[1].listening(1))
        self.assertNotIn("ble", nodes[2].listening(1))


if __name__ == "__main__":
    unittest.main()
