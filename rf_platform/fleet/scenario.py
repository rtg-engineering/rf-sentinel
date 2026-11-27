"""A scripted fleet scenario for demos and tests (SIMULATED: no radio, no real place's devices).

Five nodes around a public park. Over 15 minutes: people with phones walk through; one tracker travels with a
person it does not belong to; a rogue 2G cell switches on; one node drops offline for three minutes and returns.
Levels follow a log-distance path-loss model with noise, and a node only reports protocols it is listening for, so
the coordinator's tasking decides what the fleet can see. The true positions are kept alongside, so the fixes can
be scored (median error against the reported uncertainty).
"""
from __future__ import annotations

import math
import random
from typing import Any

from .coordinator import Coordinator, Node
from .geo import haversine_m, offset

SITE = {"name": "Demo site (public park)", "lat": 40.7812, "lon": -73.9665}  # Central Park, NYC: a public place
DURATION_S, TICK_S = 900, 5


def nodes() -> list[Node]:
    layout = [("north", "North", 170, -10), ("east", "East", 5, 180), ("south", "South", -165, 20),
              ("west", "West", 15, -175), ("center", "Center", 0, 0)]
    out = []
    for nid, name, n, e in layout:
        lat, lon = offset(SITE["lat"], SITE["lon"], n, e)
        # Center surveys cells too; East and West watch the road for tire sensors and listen for BLE only when cued.
        base = {"center": ("ble", "cellular"), "east": ("tpms",), "west": ("tpms",)}.get(nid, ("ble",))
        out.append(Node(nid, name, lat, lon, base=base))
    return out


class Mover:
    def __init__(self, key, label, kind, protocol, path, speed, start, power=-40.0, leader=None, lag_m=0.0):
        self.key, self.label, self.kind, self.protocol = key, label, kind, protocol
        self.path, self.speed, self.start, self.power = path, speed, start, power
        self.leader, self.lag_m = leader, lag_m

    def position(self, t: float) -> tuple[float, float] | None:
        if self.leader is not None:
            base = self.leader.position(t - self.lag_m / max(0.1, self.leader.speed))
            if base is None:
                return None
            return offset(base[0], base[1], 6 * math.sin(t / 40), 6 * math.cos(t / 55))
        if t < self.start:
            return None
        travelled = (t - self.start) * self.speed
        if len(self.path) == 1:
            return self.path[0]  # stationary
        for (a, b) in zip(self.path, self.path[1:]):
            leg = haversine_m(*a, *b)
            if travelled <= leg:
                f = travelled / max(1e-6, leg)
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            travelled -= leg
        return None  # walked off the map


def _point(n: float, e: float) -> tuple[float, float]:
    return offset(SITE["lat"], SITE["lon"], n, e)


def movers(seed: int = 3) -> list[Mover]:
    rng = random.Random(seed)
    out: list[Mover] = []
    brands = ["Apple, Inc.", "Samsung", "Google", "Apple, Inc.", "Garmin", "Bose", "Apple, Inc.", "Fitbit"]
    for i in range(14):
        side = rng.choice([(-1, 0), (1, 0), (0, -1), (0, 1)])
        a = _point(side[0] * 300 + rng.uniform(-120, 120) * (side[1] != 0), side[1] * 300 + rng.uniform(-120, 120) * (side[0] != 0))
        b = _point(-side[0] * 300 + rng.uniform(-150, 150), -side[1] * 300 + rng.uniform(-150, 150))
        out.append(Mover(f"phone-{i + 1:02d}", f"{brands[i % len(brands)]} phone", "phone", "ble", [a, b],
                         rng.uniform(1.0, 1.5), rng.uniform(0, 600)))
    walker = Mover("phone-15", "Samsung phone", "phone", "ble",
                   [_point(*p) for p in ((-230, -210), (-60, -40), (100, -80), (130, 90), (-40, 120), (-120, 0),
                                          (20, -120), (150, 40), (60, 180), (260, 250))], 1.0, 60)
    out.append(walker)
    out.append(Mover("tracker-01", "AirTag (separated from owner)", "tracker", "ble", [], 0, 0, power=-44, leader=walker, lag_m=8))
    for i, (n, e) in enumerate(((90, -100), (-60, 120), (120, 90))):
        out.append(Mover(f"beacon-{i + 1:02d}", "Beacon", "beacon", "ble", [_point(n, e)], 0, 0))
    for i in range(6):  # cars on the roads at the east and west edges of the park
        x = 230 if i % 2 else -230
        out.append(Mover(f"tpms-{i + 1:02d}", "Tire-pressure sensor", "vehicle", "tpms",
                         [_point(-600 if i < 3 else 600, x), _point(600 if i < 3 else -600, x)], 11.0, 40 + i * 140, power=-30))
    out.append(Mover("cell-rogue", "2G cell, unknown network", "rogue_cell", "cellular", [_point(-90, -110)], 0, 480, power=-10))
    for i, (n, e, op) in enumerate(((900, 700, "AT&T"), (-1100, -300, "Verizon"), (200, -1400, "T-Mobile"))):
        out.append(Mover(f"cell-{i + 1:02d}", f"{op} LTE cell", "cell", "cellular", [_point(n, e)], 0, 0, power=5))
    return out


def run(seed: int = 3) -> dict[str, Any]:
    """The whole scenario, tick by tick: frames for playback, the coordinator's decisions, and a fix score."""
    rng = random.Random(seed)
    fleet = nodes()
    coordinator = Coordinator(fleet)
    things = movers(seed)
    frames, errors = [], []
    for t in range(0, DURATION_S + 1, TICK_S):
        fleet[2].online = not (570 <= t < 750)  # South drops out for three minutes
        sightings = []
        truth = {}
        for m in things:
            pos = m.position(t)
            if pos is None:
                continue
            truth[m.key] = pos
            for node in fleet:
                if not node.online or m.protocol not in node.listening(t):
                    continue
                d = max(3.0, haversine_m(node.lat, node.lon, *pos))
                exponent = 2.4 if m.protocol == "ble" else 3.2  # open park; buildings would be higher
                level = m.power - 10 * exponent * math.log10(d) + rng.gauss(0, 4)
                floor = -97 if m.protocol == "ble" else -125
                if level > floor and rng.random() < (0.85 if m.protocol == "ble" else 0.95):
                    sightings.append({"node": node.id, "key": m.key, "label": m.label, "kind": m.kind,
                                      "protocol": m.protocol, "level": round(level, 1)})
        coordinator.tick(t, sightings)
        snap = coordinator.snapshot(t)
        for ent in snap["entities"]:
            if ent["fix"] and ent["fix"]["t"] == t and ent["key"] in truth:
                err = haversine_m(ent["fix"]["lat"], ent["fix"]["lon"], *truth[ent["key"]])
                ent["truth"] = list(truth[ent["key"]])
                errors.append((err, ent["fix"]["uncertainty_m"], ent["kind"]))
        frames.append(snap)
    errors.sort()
    within = sum(1 for e, u, _ in errors if e <= u)
    score = {"fixes": len(errors), "median_error_m": round(errors[len(errors) // 2][0], 1) if errors else None,
             "within_uncertainty": f"{within}/{len(errors)}"}
    return {"simulated": True, "site": SITE, "tick_s": TICK_S, "duration_s": DURATION_S,
            "nodes": [{"id": n.id, "name": n.name, "lat": n.lat, "lon": n.lon, "base": list(n.base)} for n in fleet],
            "frames": frames, "decisions": coordinator.decisions, "score": score}
