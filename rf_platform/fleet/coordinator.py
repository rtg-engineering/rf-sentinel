"""Fleet coordinator: many RF Sentinel nodes, one picture, collective decisions.

Each tick, every online node reports what it heard (sightings: node, device key, protocol, level). The coordinator
keeps one record per device across the fleet and decides, in code and with a stated reason for each decision:

- CUE      one node hears something worth locating that its neighbours are not listening for: the nearest online
           neighbours are tasked to listen on that protocol for a while.
- LOCATE   three or more nodes heard it recently: weighted-centroid fix with an uncertainty radius (fleet.geo.wcl).
- TRACK    successive fixes far enough apart, beyond their uncertainty: heading and speed.
- FOLLOW   a tracker whose fixes stay close to another device's fixes while both move a long way: an alert.
- ROGUE    a 2G cell or a cell on an unknown network: an alert, and every node is tasked to survey cells to locate it.
- COVER    a node goes offline: its neighbours take over its listening; and the reverse when it returns.

Tasks feed back into what nodes listen for, so decisions change what the fleet can see next. Receive-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .geo import bearing_deg, compass, haversine_m, wcl

RECENT_S = 20          # sightings this recent count towards a fix
CUE_S = 90             # how long a cued node listens
FOLLOW_NEAR_M = 80     # a follower's fixes stay within this of the leader's
FOLLOW_MIN_S = 240     # ... for at least this long
# Fixes from fixed nodes are pulled toward the middle of the fleet (see geo.wcl), so a long walk shows as a shorter
# track: 120 m of fix travel is already a real walk across the area.
FOLLOW_MIN_TRAVEL_M = 120
INTERESTING = {"tracker", "unknown", "rogue_cell"}
SMOOTH_S = 60            # per-node level = median of the last minute
PATH_LOSS = {"ble": 2.4, "classic": 2.4, "zigbee": 2.4, "tpms": 2.6, "cellular": 3.2}  # assumed exponents


@dataclass
class Node:
    id: str
    name: str
    lat: float
    lon: float
    online: bool = True
    base: tuple[str, ...] = ("ble",)                       # what it listens for by default
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)  # protocol -> {"until", "reason"}

    def listening(self, t: float) -> set[str]:
        return set(self.base) | {p for p, task in self.tasks.items() if task["until"] > t}


@dataclass
class Entity:
    key: str
    label: str
    protocol: str
    kind: str
    first_t: float
    heard: dict[str, tuple[float, float]] = field(default_factory=dict)   # node -> (t, smoothed level)
    history: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # node -> recent (t, level)
    fixes: list[dict[str, Any]] = field(default_factory=list)
    cued: bool = False
    cued_nodes: list[str] = field(default_factory=list)
    alerted: set[str] = field(default_factory=set)
    moving: bool | None = None
    heading: str | None = None
    pending: tuple[bool, str | None, float] | None = None
    outside: str | None = None  # compass direction when it is beyond the fleet's area (no fix possible)
    outside_said: bool = False  # a change of motion waiting to hold before it is reported


class Coordinator:
    def __init__(self, nodes: list[Node]):
        self.nodes = {n.id: n for n in nodes}
        self.entities: dict[str, Entity] = {}
        self.decisions: list[dict[str, Any]] = []
        self._was_online = {n.id: n.online for n in nodes}

    # ------------------------------------------------------------------------------------------------- helpers
    def _say(self, t: float, kind: str, text: str, **extra) -> None:
        self.decisions.append({"t": round(t, 1), "kind": kind, "text": text, **extra})

    def _nearest(self, node_id: str, count: int, exclude: set[str] = frozenset()) -> list[Node]:
        origin = self.nodes[node_id]
        others = [n for n in self.nodes.values() if n.online and n.id != node_id and n.id not in exclude]
        return sorted(others, key=lambda n: haversine_m(origin.lat, origin.lon, n.lat, n.lon))[:count]

    def _task(self, node: Node, protocol: str, t: float, seconds: float, reason: str) -> bool:
        if protocol in node.base or node.tasks.get(protocol, {}).get("until", 0) > t + seconds / 2:
            return False
        node.tasks[protocol] = {"until": t + seconds, "reason": reason}
        return True

    # ---------------------------------------------------------------------------------------------------- tick
    def tick(self, t: float, sightings: list[dict[str, Any]]) -> None:
        self._coverage(t)
        for s in sightings:
            node = self.nodes.get(s["node"])
            if node is None or not node.online:
                continue
            e = self.entities.get(s["key"])
            if e is None:
                e = self.entities[s["key"]] = Entity(s["key"], s.get("label") or s["key"], s["protocol"],
                                                     s.get("kind") or "device", t)
            # One reading jitters by several dB; the median of the last minute is what locating and the
            # "outside the fleet" test use.
            h = [x for x in e.history.get(node.id, []) if t - x[0] <= SMOOTH_S] + [(t, float(s["level"]))]
            e.history[node.id] = h
            e.heard[node.id] = (t, sorted(x[1] for x in h)[len(h) // 2])
        for e in self.entities.values():
            recent = {n: v for n, v in e.heard.items() if t - v[0] <= RECENT_S and self.nodes[n].online}
            if not recent:
                continue
            self._rogue(t, e, recent)
            self._cue(t, e, recent)
            self._locate(t, e, recent)
        self._follow(t)

    def _coverage(self, t: float) -> None:
        for node in self.nodes.values():
            was = self._was_online[node.id]
            if was and not node.online:
                helpers = self._nearest(node.id, 2)
                for h in helpers:
                    for p in node.listening(t) | set(node.base):
                        self._task(h, p, t, 600, f"covering for {node.name}")
                self._say(t, "cover", f"{node.name} went offline. {' and '.join(h.name for h in helpers) or 'No node'} "
                          f"widen their listening to cover its area until it returns.", node=node.id,
                          nodes=[h.id for h in helpers])
            elif not was and node.online:
                self._say(t, "cover", f"{node.name} is back online and resumes its own area.", node=node.id)
            self._was_online[node.id] = node.online

    def _cue(self, t: float, e: Entity, recent: dict[str, tuple[float, float]]) -> None:
        if e.cued:  # keep the helpers listening while it is still around (quietly: the decision was already stated)
            for nid in e.cued_nodes:
                node = self.nodes[nid]
                if node.online and e.protocol in node.tasks:
                    node.tasks[e.protocol]["until"] = max(node.tasks[e.protocol]["until"], t + CUE_S)
            return
        if e.kind not in INTERESTING or len(recent) >= 3:
            return
        source = max(recent, key=lambda n: recent[n][1])
        helpers = [n for n in self._nearest(source, 3, exclude=set(recent)) if e.protocol not in n.listening(t)]
        tasked = [n for n in helpers[:2] if self._task(n, e.protocol, t, CUE_S, f"cued to help locate {e.label}")]
        e.cued = True
        e.cued_nodes = [n.id for n in tasked]
        if tasked:
            self._say(t, "cue", f"{self.nodes[source].name} heard {e.label} at {recent[source][1]:.0f} dB, but only "
                      f"{len(recent)} node(s) can hear it: cueing {' and '.join(n.name for n in tasked)} to listen "
                      f"for {e.protocol.upper()} for {CUE_S} s so it can be located.", entity=e.key,
                      nodes=[n.id for n in tasked])

    def _locate(self, t: float, e: Entity, recent: dict[str, tuple[float, float]]) -> None:
        # Only nodes with a few readings: one reading per node is too noisy to locate or to call "outside".
        recent = {n: v for n, v in recent.items() if sum(1 for x in e.history.get(n, []) if t - x[0] <= SMOOTH_S) >= 3}
        if len(recent) < 3:
            return
        levels = {n: lvl for n, (_, lvl) in recent.items()}
        fix = wcl([(self.nodes[n].lat, self.nodes[n].lon, lvl) for n, lvl in levels.items()])
        if fix is None:
            return
        # Is it really where the weighted centroid says? If it were, path loss over the nodes' distances to that
        # point predicts how much the level should differ between the nearest and farthest node. An emitter far
        # outside the fleet shows much less difference than that (every node is about equally far from it), and a
        # weighted centroid would still put a confident pin inside the fleet. So: measured spread well under the
        # predicted spread means "outside", with a direction and no pin.
        dists = {n: max(10.0, haversine_m(fix["lat"], fix["lon"], self.nodes[n].lat, self.nodes[n].lon)) for n in levels}
        exponent = PATH_LOSS.get(e.protocol, 2.7)
        predicted = 10 * exponent * math.log10(max(dists.values()) / min(dists.values()))
        measured = max(levels.values()) - min(levels.values())
        ratio = measured / predicted if predicted > 3 else 1.0
        if ratio < (0.7 if e.outside else 0.5):  # hysteresis: clearer evidence to come back inside
            cy = sum(n.lat for n in self.nodes.values()) / len(self.nodes)  # the middle of the whole fleet
            cx = sum(n.lon for n in self.nodes.values()) / len(self.nodes)
            direction = compass(bearing_deg(cy, cx, fix["lat"], fix["lon"]))
            if e.outside is None and not e.fixes and (e.kind in INTERESTING or e.protocol == "cellular"):
                self._say(t, "locate", f"{e.label} is outside the fleet's area, roughly to the {direction}: its level "
                          f"differs by {measured:.0f} dB across {len(levels)} nodes where a nearby emitter would show "
                          f"about {predicted:.0f} dB, so it is farther away and its distance cannot be estimated from "
                          "here.", entity=e.key, outside=direction)
                e.outside_said = True
            e.outside = direction
            return
        if e.outside is not None and e.outside_said:
            self._say(t, "locate", (f"{e.label} has come into the fleet's area." if e.kind == "tracker" else
                                    f"More readings place {e.label} within the fleet's area after all."), entity=e.key)
            e.outside_said = False
        e.outside = None
        fix = {"t": round(t, 1), **fix, "nodes": sorted(recent)}
        first = not e.fixes
        e.fixes.append(fix)
        if first and (e.kind in INTERESTING or e.protocol == "cellular"):
            self._say(t, "locate", f"Located {e.label} from {fix['observers']} nodes: within about "
                      f"{fix['uncertainty_m']:.0f} m.", entity=e.key, fix=fix)
        # Motion from the mean of the oldest and newest fixes in the last two minutes (single fixes jitter by tens
        # of metres), and a change is reported only once it has held for 20 s: no flapping between "moving" and
        # "stationary" on noise.
        # Only fixes made by the same nodes: when a node drops out, every fix shifts, which is not motion.
        span = [f for f in e.fixes if t - f["t"] <= 120 and f["nodes"] == fix["nodes"]]
        if len(span) >= 6 and span[-1]["t"] - span[0]["t"] >= 60:
            k = max(2, len(span) // 3)
            a = (sum(f["lat"] for f in span[:k]) / k, sum(f["lon"] for f in span[:k]) / k)
            b = (sum(f["lat"] for f in span[-k:]) / k, sum(f["lon"] for f in span[-k:]) / k)
            dist = haversine_m(*a, *b)
            dt = max(1.0, (sum(f["t"] for f in span[-k:]) - sum(f["t"] for f in span[:k])) / k)
            unc = sum(f["uncertainty_m"] for f in span) / len(span)
            # Fixes are compressed toward the fleet's middle, so "moving" has a low bar; "stationary" needs strong
            # evidence; in between, nothing new is said.
            if dist > max(40.0, 0.4 * unc):
                moving = True
            elif dist < max(15.0, 0.15 * unc):
                moving = False
            else:
                return
            heading = compass(bearing_deg(*a, *b)) if moving else None
            changed = moving != e.moving or (moving and heading != e.heading)
            if not changed:
                e.pending = None
            elif e.pending is None or e.pending[:2] != (moving, heading):
                e.pending = (moving, heading, t)
            elif t - e.pending[2] >= 20:
                if e.kind in INTERESTING:
                    self._say(t, "track", f"{e.label} is " + (f"moving {heading} at about {dist / dt:.1f} m/s."
                                                             if moving else "stationary."), entity=e.key)
                e.moving, e.heading, e.pending = moving, heading, None

    def _rogue(self, t: float, e: Entity, recent: dict[str, tuple[float, float]]) -> None:
        if e.kind != "rogue_cell" or "rogue" in e.alerted:
            return
        e.alerted.add("rogue")
        tasked = [n for n in self.nodes.values() if n.online and self._task(n, "cellular", t, 300, f"locating {e.label}")]
        self._say(t, "alert", f"{e.label} appeared, heard by {', '.join(self.nodes[n].name for n in recent)}. A 2G or "
                  f"unknown-network cell can indicate a fake base station. Tasking all {len(tasked)} other node(s) to "
                  "survey cells for 5 minutes to locate it.", entity=e.key, severity="critical",
                  nodes=[n.id for n in tasked])

    def _follow(self, t: float) -> None:
        trackers = [e for e in self.entities.values() if e.kind == "tracker" and "follow" not in e.alerted and len(e.fixes) >= 4]
        others = [e for e in self.entities.values() if e.kind != "tracker" and e.protocol != "cellular" and len(e.fixes) >= 4]
        for tr in trackers:
            window = [f for f in tr.fixes if t - f["t"] <= FOLLOW_MIN_S * 1.5]
            if len(window) < 4 or window[-1]["t"] - window[0]["t"] < FOLLOW_MIN_S:
                continue
            travel = haversine_m(window[0]["lat"], window[0]["lon"], window[-1]["lat"], window[-1]["lon"])
            if travel < FOLLOW_MIN_TRAVEL_M:
                continue
            for other in others:
                pairs = []
                for f in window:
                    near = min(other.fixes, key=lambda g: abs(g["t"] - f["t"]))
                    if abs(near["t"] - f["t"]) <= 15:
                        pairs.append(haversine_m(f["lat"], f["lon"], near["lat"], near["lon"]))
                if len(pairs) >= 0.7 * len(window) and sorted(pairs)[len(pairs) // 2] <= FOLLOW_NEAR_M:
                    tr.alerted.add("follow")
                    self._say(t, "alert", f"{tr.label} has travelled about {travel:.0f} m with {other.label} over "
                              f"{(window[-1]['t'] - window[0]['t']) / 60:.0f} min, staying within about "
                              f"{sorted(pairs)[len(pairs) // 2]:.0f} m of it. A tracker moving with a person who does not "
                              "own it is the pattern of unwanted tracking.", entity=tr.key, other=other.key,
                              severity="critical")
                    break

    # -------------------------------------------------------------------------------------------------- output
    def snapshot(self, t: float) -> dict[str, Any]:
        entities = []
        for e in self.entities.values():
            recent = {n: v for n, v in e.heard.items() if t - v[0] <= RECENT_S}
            if not recent:
                continue
            fix = e.fixes[-1] if e.fixes and t - e.fixes[-1]["t"] <= RECENT_S else None
            entities.append({"key": e.key, "label": e.label, "kind": e.kind, "protocol": e.protocol,
                             "heard_by": sorted(recent), "level": round(max(v[1] for v in recent.values()), 1),
                             "fix": fix, "trail": [[f["lat"], f["lon"]] for f in e.fixes[-12:]] if fix else [],
                             "outside": e.outside,
                             "alert": bool(e.alerted), "moving": e.moving})
        return {"t": round(t, 1), "nodes": [{"id": n.id, "online": n.online, "listening": sorted(n.listening(t)),
                                             "tasks": {p: v["reason"] for p, v in n.tasks.items() if v["until"] > t}}
                                            for n in self.nodes.values()],
                "entities": entities}
