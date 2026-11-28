"""Agent workflows: tell RF Sentinel what to find, what to keep, and what to do when something shows up.

    "Watch for bluetooth trackers for 5 minutes, only stronger than -60. If a tracker is seen for more
     than 2 minutes, alert me and record 5 seconds of IQ. If an AirTag appears, scan bluetooth classic
     for 60 seconds."

A request becomes a typed WorkflowSpec (collect / filters / triggers). Two planners produce the same spec:
a deterministic grammar (always available) and an optional model planner (Pydantic AI). Either way the
spec is validated and bounded here, and every action goes through the run's AgentGateway, so a model can
propose a plan but cannot widen permissions, exceed bounds, or transmit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .profiles import MAX_DURATION_S, drone_band

Protocol = Literal["ble", "classic", "zigbee", "tpms", "cellular", "drone"]
FieldName = Literal["protocol", "label", "name", "manufacturer", "device_type", "device_class", "address",
                    "rssi_dbfs", "count", "dwell_s", "new", "operator", "rat", "generation", "role"]
Op = Literal["eq", "ne", "contains", "not_contains", "ge", "le"]
TEXT_FIELDS = {"protocol", "label", "name", "manufacturer", "device_type", "device_class", "address", "operator",
               "rat", "generation", "role"}
NUMBER_FIELDS = {"rssi_dbfs", "count", "dwell_s"}
MAX_RECORD_S = 10
MAX_FOLLOW_UP_S = 300
MAX_INQUIRY_S = 30
# Tracker kinds. "find my" alone is not enough: iPhones, Macs and AirPods send Find My frames too.
CONFIRMED_DRONE = "confirmed drone"  # device_type the drone adapter gives a detector-confirmed link
DRONE_WORDS = r"\b(?:drones?|uavs?|uas|quadcopters?|dji|ocusync)\b"
TRACKER_WORDS = ("airtag", "find my accessory", "tile", "tracker", "smarttag", "chipolo", "pebblebee")


class Condition(BaseModel):
    field: FieldName
    op: Op
    value: str | float | bool

    @model_validator(mode="after")
    def _types(self):
        if self.field in NUMBER_FIELDS:
            if self.op not in ("ge", "le", "eq", "ne") or isinstance(self.value, bool):
                raise ValueError(f"{self.field} compares numbers with ge/le/eq/ne")
            self.value = float(self.value)
        elif self.field == "new":
            if not isinstance(self.value, bool):
                self.value = str(self.value).lower() in ("true", "1", "yes")
            if self.op not in ("eq", "ne"):
                raise ValueError("new compares with eq/ne true/false")
        else:
            if self.op in ("ge", "le"):
                raise ValueError(f"{self.field} is text: use eq/ne/contains/not_contains")
            self.value = str(self.value)
        return self

    def describe(self) -> str:
        if self.field == "new":
            return "new (not seen in earlier runs)" if (self.value is True) == (self.op == "eq") else "previously seen"
        words = {"eq": "is", "ne": "is not", "contains": "contains", "not_contains": "does not contain",
                 "ge": "≥", "le": "≤"}
        unit = {"rssi_dbfs": " dBFS", "dwell_s": " s"}.get(self.field, "")
        value = f"{self.value:g}" if isinstance(self.value, float) else f'"{self.value}"'
        name = {"rssi_dbfs": "signal", "dwell_s": "seen for", "count": "sightings", "device_class": "kind",
                "label": "identity", "device_type": "device type"}.get(self.field, self.field)
        if self.field == "count" and self.op == "ge" and self.value == 1:
            return "any device"
        return f"{name} {words[self.op]} {value}{unit}"


class Action(BaseModel):
    # inquiry_scan and name_lookup TRANSMIT (host Bluetooth adapter): each waits for a named operator's approval.
    kind: Literal["alert", "tag", "record_iq", "start_scan", "extend", "stop", "inquiry_scan", "name_lookup"]
    message: str | None = None                 # alert
    severity: Literal["info", "warning", "critical"] = "warning"
    label: str | None = None                   # tag
    seconds: float | None = None               # record_iq / start_scan / extend
    protocol: Protocol | None = None           # start_scan

    @model_validator(mode="after")
    def _bounds(self):
        if self.kind == "record_iq":
            self.seconds = min(float(self.seconds or 5), MAX_RECORD_S)
        elif self.kind == "start_scan":
            if not self.protocol:
                raise ValueError("start_scan needs a protocol")
            self.seconds = min(max(float(self.seconds or 60), 5), MAX_FOLLOW_UP_S)
        elif self.kind == "extend":
            self.seconds = min(max(float(self.seconds or 60), 5), MAX_DURATION_S)
        elif self.kind == "tag":
            self.label = (self.label or "flagged").strip()[:40] or "flagged"
        elif self.kind == "inquiry_scan":
            self.seconds = min(max(float(self.seconds or 10), 1), MAX_INQUIRY_S)
        return self

    def describe(self) -> str:
        return {"alert": lambda: f"alert ({self.severity})" + (f": {self.message}" if self.message else ""),
                "tag": lambda: f'tag as "{self.label}"',
                "record_iq": lambda: f"record {self.seconds:g} s of IQ evidence",
                "start_scan": lambda: f"scan {PROTOCOL_TITLES[self.protocol]} for {self.seconds:g} s",
                "extend": lambda: f"keep watching {self.seconds:g} s longer",
                "stop": lambda: "stop collection",
                "inquiry_scan": lambda: f"Bluetooth inquiry scan for {self.seconds:g} s (transmits; needs approval)",
                "name_lookup": lambda: "ask the device its Bluetooth name (transmits; needs approval)"}[self.kind]()


class Trigger(BaseModel):
    when: list[Condition] = Field(min_length=1)
    do: list[Action] = Field(min_length=1)
    repeat: bool = False          # False: once per device; True: again after cooldown_s
    cooldown_s: float = 30.0

    def describe(self) -> str:
        return "when " + " and ".join(c.describe() for c in self.when) + " → " + ", ".join(a.describe() for a in self.do)


class WorkflowSpec(BaseModel):
    goal: str = ""
    collect: list[Protocol] = Field(min_length=1)
    duration_s: int = 60
    device_id: str | None = None
    channel: int | None = Field(default=None, ge=11, le=26)  # Zigbee / 802.15.4 channel
    filters: list[Condition] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("duration_s")
    @classmethod
    def _duration(cls, value: int) -> int:
        if not 5 <= value <= MAX_DURATION_S:
            raise ValueError(f"duration must be between 5 and {MAX_DURATION_S} seconds")
        return value

    @field_validator("collect")
    @classmethod
    def _collect(cls, value: list[str]) -> list[str]:
        ordered = list(dict.fromkeys(value))
        sdr = [p for p in ordered if p != "cellular"]
        if len(sdr) > 1 and set(sdr) != {"ble", "classic"}:
            raise ValueError("one SDR protocol per run (BLE with Bluetooth Classic counts as one), "
                             "optionally together with the cellular modem")
        return ordered

    def sdr_collect(self) -> list[str]:
        return [p for p in self.collect if p != "cellular"]

    def command(self, protocols: list[str] | None = None) -> str:
        """The bounded collection request the deterministic agent executes (for one source group)."""
        protocols = set(protocols if protocols is not None else (self.sdr_collect() or self.collect))
        what = ("bluetooth devices" if protocols == {"ble", "classic"} else "ble devices" if protocols == {"ble"}
                else "bluetooth classic devices" if protocols == {"classic"} else
                "zigbee" if protocols == {"zigbee"} else "cell towers" if protocols == {"cellular"} else
                ("drone controllers" if drone_band(self.goal) == "2g4" else "drones") if protocols == {"drone"}
                else "tire sensors")
        return (f"find {what} for {self.duration_s} seconds" + (f" on {self.device_id}" if self.device_id else "")
                + (f" on channel {self.channel}" if self.channel and "zigbee" in protocols else ""))

    def summary(self) -> dict[str, Any]:
        unit = (lambda text: text.replace(" dBFS", " dBm")) if self.collect == ["cellular"] else (lambda text: text)
        return {"find": f"{' + '.join(PROTOCOL_TITLES[p] for p in self.collect)} for {self.duration_s} s",
                "filters": [unit(c.describe()) for c in self.filters],
                "triggers": [unit(t.describe()) for t in self.triggers]}


PROTOCOL_TITLES = {"ble": "BLE", "classic": "Bluetooth Classic", "zigbee": "Zigbee", "tpms": "TPMS",
                   "cellular": "Cellular (modem)", "drone": "Drone RF"}


# --------------------------------------------------------------------------------------------- planning
UNIT_S = r"(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)"


def _seconds(value: str, unit: str | None) -> int:
    unit = (unit or "s").lower()
    return int(float(value) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1))


VENDORS = ("apple", "samsung", "google", "microsoft", "tile", "garmin", "fitbit", "sony", "bose", "amazon",
           "xiaomi", "huawei", "lg", "jbl", "logitech", "intel", "meta", "oura", "whoop")
DEVICE_NOUNS = {
    "tracker": ("device_class", "eq", "tracker"), "trackers": ("device_class", "eq", "tracker"),
    "airtag": ("label", "contains", "AirTag"), "airtags": ("label", "contains", "AirTag"),
    "tile": ("manufacturer", "contains", "Tile"), "tiles": ("manufacturer", "contains", "Tile"),
    "phone": ("device_type", "contains", "Phone"), "phones": ("device_type", "contains", "Phone"),
    "watch": ("device_type", "contains", "Watch"), "watches": ("device_type", "contains", "Watch"),
    "headphones": ("device_type", "contains", "Head"), "earbuds": ("device_type", "contains", "Head"),
    "beacon": ("device_class", "eq", "beacon"), "beacons": ("device_class", "eq", "beacon"),
}

ACTION_PATTERNS: list[tuple[str, Callable[[re.Match], Action]]] = [
    (r"\b(?:send (?:me )?an? )?(?:(urgent|critical|high[- ]priority) )?(?:alert|notify|warn|tell)(?: me)?\b"
     r"(?:\s*(?:that|with|saying)?\s*[\"']([^\"']+)[\"'])?(?:\s*(urgently|immediately))?",
     lambda m: Action(kind="alert", message=m.group(2),
                      severity="critical" if (m.group(1) or m.group(3)) else "warning")),
    (rf"\b(?:record|capture|save)\s*(?:(\d+(?:\.\d+)?)\s*({UNIT_S})\s*)?(?:of\s+)?(?:iq|samples|evidence|it|the signal)?\b",
     lambda m: Action(kind="record_iq", seconds=_seconds(m.group(1), m.group(2)) if m.group(1) else 5)),
    (rf"\b(?:grab|take|get|pull|keep)\s+(\d+(?:\.\d+)?)\s*({UNIT_S})\s+of\s+(?:iq|samples|evidence|raw iq)\b",
     lambda m: Action(kind="record_iq", seconds=_seconds(m.group(1), m.group(2)))),
    (rf"\b(?:scan|look|sweep|check|listen|survey)\s+(?:for\s+|the\s+)?(bluetooth classic|classic bluetooth|classic|br/edr|zigbee|tpms|tire sensors?|ble|cell towers?|cellular|cells|lte|5g|drones?)"
     rf"(?:\s+devices)?(?:\s+for\s+(\d+(?:\.\d+)?)\s*({UNIT_S}))?",
     lambda m: Action(kind="start_scan", protocol=_protocol_word(m.group(1)),
                      seconds=_seconds(m.group(2), m.group(3)) if m.group(2) else 60)),
    (rf"\b(?:keep (?:watching|scanning|listening|going)|extend(?: the (?:scan|run))?)(?:\s+(?:by|for|another))?(?:\s+(\d+(?:\.\d+)?)\s*({UNIT_S}))?",
     lambda m: Action(kind="extend", seconds=_seconds(m.group(1), m.group(2)) if m.group(1) else 60)),
    (r"\b(?:tag|mark|label|flag)(?: (?:it|them|the device))?(?: as)? [\"']([^\"']+)[\"']",
     lambda m: Action(kind="tag", label=m.group(1))),
    (r"\b(?:tag|mark|flag)(?: (?:it|them|the device))?(?: as)? ([a-z][\w-]*)",
     lambda m: Action(kind="tag", label=m.group(1))),
    (r"\b(?:flag|tag) (?:it|them)\b", lambda m: Action(kind="tag", label="flagged")),
    (rf"\b(?:run|do|start|send|try)\s+(?:an?\s+)?(?:bluetooth\s+)?inquiry(?:\s+scan)?(?:\s+for\s+(\d+(?:\.\d+)?)\s*({UNIT_S}))?",
     lambda m: Action(kind="inquiry_scan", seconds=_seconds(m.group(1), m.group(2)) if m.group(1) else 10)),
    (r"\b(?:look\s*up|get|ask (?:it )?for|request|query)\s+(?:its|their|the(?: device'?s)?)\s+(?:bluetooth\s+)?(?:remote\s+)?names?\b",
     lambda m: Action(kind="name_lookup")),
    (r"\bstop\b(?: (?:the )?(?:scan|run|collection|watching))?", lambda m: Action(kind="stop")),
]


def _protocol_word(word: str) -> str:
    word = word.lower()
    return ("classic" if "classic" in word or "br/edr" in word else "zigbee" if "zigbee" in word
            else "drone" if "drone" in word
            else "cellular" if re.search(r"cell|lte|5g|tower", word)
            else "tpms" if "tpms" in word or "tire" in word else "ble")


def _conditions(text: str) -> list[Condition]:
    """Device conditions in a phrase: strength, vendor, device kind, name, newness, persistence."""
    found: list[Condition] = []
    neg = bool(re.search(r"\b(?:ignore|except|exclude|excluding|not|no)\b", text))
    m = re.search(r"\b(?:stronger|louder|closer|above|over|greater) than (-?\d+(?:\.\d+)?)|\b(?:rssi|signal)\s*(?:>|>=|above|over) ?(-?\d+(?:\.\d+)?)", text)
    if m:
        found.append(Condition(field="rssi_dbfs", op="ge", value=float(m.group(1) or m.group(2))))
    m = re.search(r"\b(?:weaker|quieter|farther|below|under) than (-?\d+(?:\.\d+)?)", text)
    if m:
        found.append(Condition(field="rssi_dbfs", op="le", value=float(m.group(1))))
    for vendor in VENDORS:
        if re.search(rf"\b(?:from|by|made by|vendor) {vendor}\b|\b{vendor} (?:devices?|gear)\b", text):
            found.append(Condition(field="manufacturer", op="not_contains" if neg else "contains", value=vendor.title()))
    if re.search(r"\bconfirmed\b", text) and re.search(DRONE_WORDS, text):
        found.append(Condition(field="device_class", op="eq", value="drone"))  # not drone-like links
    for noun, (field, op, value) in DEVICE_NOUNS.items():
        pattern = rf"\b{noun}\b(?! (?:for|out|over|over|it))" if noun == "watch" else rf"\b{noun}\b"
        if noun == "watch" and re.match(r"\s*watch\b", text):
            continue  # "watch for ..." is the verb
        if re.search(pattern, text) and not any(c.field == field and c.value == value for c in found):
            found.append(Condition(field=field, op=("ne" if op == "eq" else "not_contains") if neg else op, value=value))
    m = re.search(r"\bnamed [\"']?([\w\- ]+?)[\"']?(?:\s+(?:is|are|appears?|shows?|stronger|for|and|,)\b|$)", text)
    if m:
        found.append(Condition(field="name", op="contains", value=m.group(1).strip()))
    if re.search(r"\b(?:new|unfamiliar|never seen)\b(?! (?:than|scan|serving))|\bunknown\b(?! (?:operator|network|carrier|plmn))|\banything new\b", text):
        found.append(Condition(field="new", op="eq", value=True))
    m = (re.search(rf"\b(?:seen|around|present|following|stays?|persists?|lingers?)\b[^.]*?\bfor (?:more than |over |at least )?(\d+(?:\.\d+)?)\s*({UNIT_S})", text)
         or re.search(rf"\bstill (?:there|around|here|present|seen|nearby|visible)\b (?:after|for|past) (?:more than |over |at least )?(\d+(?:\.\d+)?)\s*({UNIT_S})", text))
    if m:
        found.append(Condition(field="dwell_s", op="ge", value=_seconds(m.group(1), m.group(2))))
    carriers = {"at&t": "AT&T", "att": "AT&T", "firstnet": "FirstNet", "verizon": "Verizon", "t-mobile": "T-Mobile",
                "tmobile": "T-Mobile", "dish": "Dish", "us cellular": "US Cellular"}
    for word, operator in carriers.items():
        if re.search(rf"(?<![\w&-]){re.escape(word)}(?![\w&-])", text):
            found.append(Condition(field="operator", op="not_contains" if neg else "contains", value=operator))
    if re.search(r"\bunknown (?:operator|network|carrier|plmn)\b|\bunrecognized (?:operator|network|carrier)\b", text):
        found.append(Condition(field="operator", op="eq", value="unknown operator"))
    for generation in ("2g", "3g", "4g", "5g"):
        if re.search(rf"\b(?:downgrades?|drops?|falls?|fall back|switch(?:es)?) to {generation}\b|\b{generation} (?:cell|tower|cells|towers)\b", text):
            found.append(Condition(field="generation", op="eq", value=generation.upper()))
    for generation in ("2g", "3g"):
        # "anything 2G": suspicious generations named on their own (4G/5G alone usually describe the survey)
        if re.search(rf"\b(?:anything|any|something|a|an|every|each) {generation}\b(?! (?:cell|tower|cells|towers))", text) \
                and not any(c.field == "generation" and c.value == generation.upper() for c in found):
            found.append(Condition(field="generation", op="eq", value=generation.upper()))
    if re.search(r"\b(?:gsm)\b", text):
        found.append(Condition(field="generation", op="eq", value="2G"))
    if re.search(r"\bserving cell (?:changes?|switches|is new)\b|\bnew serving cell\b|\bhands? ?over\b", text):
        found.append(Condition(field="role", op="eq", value="serving"))
    m = re.search(r"\bseen (?:more than |at least )?(\d+) times\b", text)
    if m:
        found.append(Condition(field="count", op="ge", value=float(m.group(1))))
    return found


def _split_triggers(text: str) -> tuple[str, list[str]]:
    parts = re.split(r"(?:[.;]\s*|,\s*|\s+)(?=\b(?:if|when|whenever|once|as soon as|each time|every time)\b)", text)
    main = [p for p in parts if not re.match(r"(?:if|when|whenever|once|as soon as|each time|every time)\b", p)]
    triggers = [p for p in parts if re.match(r"(?:if|when|whenever|once|as soon as|each time|every time)\b", p)]
    return " ".join(main), triggers


def _parse_trigger(clause: str) -> Trigger | None:
    triggers = _parse_triggers(clause)
    return triggers[0] if len(triggers) == 1 else (triggers or [None])[0] if not triggers else _OrTriggers(triggers)


class _OrTriggers(list):
    """Several triggers parsed from one clause ("if X or Y, alert"): each condition set gets the actions."""


def _parse_triggers(clause: str) -> list[Trigger]:
    repeat = bool(re.match(r"(?:whenever|each time|every time)\b", clause))
    actions: list[Action] = []
    spans: list[tuple[int, int]] = []
    for pattern, build in ACTION_PATTERNS:
        for m in re.finditer(pattern, clause):
            if any(a <= m.start() < b for a, b in spans):
                continue
            try:
                actions.append(build(m))
            except ValueError:
                continue
            spans.append((m.start(), m.end()))
    if not actions:
        return []
    first_action = min(a for a, _ in spans)
    condition_text = clause[:first_action]
    order = sorted(range(len(actions)), key=lambda i: spans[i][0])
    do = [actions[i] for i in order]
    parts = [p for p in re.split(r"\bor\b", condition_text) if p.strip()]
    groups = [_conditions(p) for p in parts]
    if len(groups) > 1 and all(groups):
        return [Trigger(when=g, do=do, repeat=repeat) for g in groups]
    when = _conditions(condition_text)
    if not when:
        # "if anything shows up" / "when something appears": any device in scope
        when = [Condition(field="count", op="ge", value=1)]
    return [Trigger(when=when, do=do, repeat=repeat)]


def plan_deterministic(text: str, standing_actions: bool = False) -> WorkflowSpec:
    """Plain-language request → WorkflowSpec with a fixed grammar. Raises ValueError when unsure.

    standing_actions: actions with no condition ("... and tag them as suspect") apply to every device in scope
    (used by playbook steps that focus on devices an earlier step found)."""
    from .profiles import IntentError, parse
    raw = " ".join(str(text).split())
    lower = raw.lower()
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", lower):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    # "alert me on anything 2G or any unknown network" = "alert me if ...": the condition follows the action.
    lower = re.sub(r"\b((?:send (?:me )?an? )?(?:(?:urgent|critical|high[- ]priority) )?(?:alert|notify|warn)(?: me)?) "
                   r"(?:on|about|for|of) (?=(?:any|anything|every|each|new|unknown)\b|(?:a|an) (?!\d))", r"\1 if ", lower)
    main, trigger_clauses = _split_triggers(lower)
    # A sentence boundary ends a rule: "if X, alert. Send an urgent alert if Y" - the second sentence's action
    # leads the next condition rather than joining the first rule.
    leads: dict[int, str] = {}
    for index, clause in enumerate(trigger_clauses):
        head, _, tail = clause.partition(". ")
        if tail.strip():
            trigger_clauses[index] = head
            if index + 1 < len(trigger_clauses):
                leads[index + 1] = tail.strip()
            else:
                main = f"{main}. {tail}"
    # "if X, and alert me when Y": the trailing ", and <action>" belongs to the next (action-less) clause.
    for index in range(len(trigger_clauses) - 1):
        m = re.search(r",\s*and\s+([^,]+)$", trigger_clauses[index])
        if m and not _parse_triggers(trigger_clauses[index + 1]) and _parse_triggers("x " + m.group(1)):
            trigger_clauses[index + 1] = trigger_clauses[index + 1].rstrip(" .") + " " + m.group(1)
            trigger_clauses[index] = trigger_clauses[index][: m.start()]
    triggers = []
    for index, clause in enumerate(trigger_clauses):
        or_clause = clause.rstrip().endswith(" or")
        clause = re.sub(r"\s+or$", "", clause.rstrip())
        trigger = _parse_trigger(clause)
        if trigger is None and index > 0 and triggers and trigger_clauses[index - 1].rstrip().endswith(" or"):
            # "... if X or if Y": Y shares X's actions
            conditions = _conditions(clause)
            trigger = Trigger(when=conditions, do=triggers[-1].do, repeat=triggers[-1].repeat) if conditions else None
        if trigger is None and index in leads:
            trigger = _parse_trigger(clause + " " + leads[index])
        if trigger is None:
            # "<action> when <condition>": the action ends the preceding sentence ("... and stop when ...")
            segments = re.split(r"[.;,]|\band\b|\bthen\b", main)
            tail = segments[-1]
            if len(segments) == 1 and re.search(r"\b(?:find|watch|monitor|survey|scan|look|listen|search|track|sweep)\b", tail):
                tail = ""  # never consume the collection clause itself
            trigger = _parse_trigger(clause + " " + tail) if tail.strip() else None
            if trigger is not None:
                main = main[: len(main) - len(tail)].rstrip(" ,.;").removesuffix(" and").removesuffix(" then")
        if trigger is None and or_clause and index + 1 < len(trigger_clauses):
            # "if X or if Y, alert": X takes Y's actions
            follow = _parse_trigger(re.sub(r"\s+or$", "", trigger_clauses[index + 1].rstrip()))
            follow = follow[0] if isinstance(follow, _OrTriggers) else follow
            conditions = _conditions(clause)
            if follow and conditions:
                trigger = Trigger(when=conditions, do=follow.do, repeat=follow.repeat)
        if trigger is not None and index == 0 and not isinstance(trigger, _OrTriggers):
            # "send an urgent alert if X, and record 5 s of IQ": the action before "if" belongs to this rule too.
            segments = re.split(r"[.;,]|\band\b|\bthen\b", main)
            tail = segments[-1]
            lead_rules = [] if re.search(r"\b(?:find|watch|monitor|survey|scan|look|listen|search|track|sweep)\b", tail) \
                else _parse_triggers("x " + tail)
            lead = [a for a in lead_rules[0].do if a.kind in ("alert", "tag", "record_iq")] if lead_rules else []
            if lead:
                trigger = Trigger(when=trigger.when, do=lead + [a for a in trigger.do if a.kind not in {x.kind for x in lead}],
                                  repeat=trigger.repeat)
                main = main[: len(main) - len(tail)].rstrip(" ,.;").removesuffix(" and").removesuffix(" then")
        if isinstance(trigger, _OrTriggers):
            triggers.extend(trigger)
        elif trigger is not None:
            triggers.append(trigger)
    if standing_actions:
        segments = [seg for seg in re.split(r"[.;,]|\band\b|\bthen\b", main)[1:] if seg.strip()]
        standing = [a for seg in segments for t in _parse_triggers("x " + seg) for a in t.do
                    if a.kind in ("alert", "tag", "record_iq")]
        if standing:
            triggers.append(Trigger(when=[Condition(field="count", op="ge", value=1)], do=standing))
            for seg in segments:
                if _parse_triggers("x " + seg):
                    main = main.replace(seg, " ")
    # "... and run an inquiry scan": a once-per-run transmit step, asked for (and approved) when collection first hears
    # a device, never repeated per device.
    segments = [seg for seg in re.split(r"[.;,]|\band\b|\bthen\b", main)[1:] if seg.strip()]
    once = [a for seg in segments for t in _parse_triggers("x " + seg) for a in t.do if a.kind == "inquiry_scan"]
    if once and not any(a.kind == "inquiry_scan" for t in triggers for a in t.do):
        triggers.append(Trigger(when=[Condition(field="count", op="ge", value=1)], do=once[:1]))
        for seg in segments:
            if any(a.kind == "inquiry_scan" for t in _parse_triggers("x " + seg) for a in t.do):
                main = main.replace(seg, " ")
    trackers = bool(re.search(r"\b(?:trackers?|airtags?|tiles?|find my|smarttags?)\b", main))
    cellular_words = r"\b(?:cell|cells|cellular|towers?|lte|5g|4g|2g|3g|gsm|base stations?|carriers?|plmn|(?:unknown|unrecognized) (?:network|operator|carrier))\b"
    implies_cellular = bool(re.search(cellular_words, main)) or (
        not re.search(r"\b(bluetooth|ble|zigbee|tpms|tire|classic|trackers?|airtags?|beacons?|drones?|uavs?|uas|quadcopters?|dji)\b", main)
        and bool(re.search(cellular_words, " ".join(trigger_clauses))))
    if implies_cellular and not re.search(cellular_words, main):
        main = f"survey cell towers {main}"
    implies_bluetooth = (not implies_cellular) and trackers or bool(not implies_cellular and re.search(
        rf"\b(?:{'|'.join(VENDORS)}|phones?|watches|headphones|earbuds|beacons?|wearables?|devices?)\b", main))
    whole_duration = re.search(rf"\b(?:in|over|during|for) the next (\d+(?:\.\d+)?)\s*({UNIT_S})", lower)
    if whole_duration and not re.search(rf"\b\d+(?:\.\d+)?\s*{UNIT_S}\b", main):
        main = f"{main} for {_seconds(whole_duration.group(1), whole_duration.group(2))} seconds"
    probe = main if re.search(r"\b(drones?|uavs?|uas|quadcopters?|dji|ocusync|bluetooth|ble|btle|zigbee|802\.15\.4|thread|tpms|tire|tyre|classic|beacons?|airtags?|cell|cells|cellular|towers?|lte|5g|4g|base stations?|carriers?)\b", main) \
        else main if re.search(r"\bwi-?fi\b", main) else (
            # Vendors and device kinds are identified from BLE advertisements; Classic only yields LAPs.
            "find ble devices " if re.search(rf"\b(?:{'|'.join(VENDORS)}|phones?|watches|headphones|earbuds|wearables?)\b", main)
            else "find bluetooth devices ") + main
    try:
        task = parse(re.sub(rf"\b\d+(?:\.\d+)?\s*{UNIT_S}\s+of\s+(?:iq|samples)", "", probe))
    except IntentError as exc:
        raise ValueError(str(exc)) from exc
    other_sdr = re.search(r"\b(?:bluetooth|ble|btle|trackers?|airtags?|tiles?|beacons?|zigbee|tpms|tire|classic)\b", main)
    if re.search(DRONE_WORDS, main) and other_sdr:
        raise ValueError("Drone watching takes its own receiver for the whole step, so it cannot share a step with "
                         f"{other_sdr.group(0)}. Ask for them one after the other, e.g. 'watch for drones for 60 "
                         "seconds then find trackers for 60 seconds'.")
    collect = [task.profile.name, *task.companions]
    if trackers and task.profile.name == "ble":
        collect = ["ble"]  # trackers are BLE advertisers
    mixed_bt = re.search(r"\b(?:bluetooth|ble|trackers?|airtags?|tiles?|beacons?|zigbee|tpms|tire|drones?|uavs?|dji)\b", main)
    if task.profile.name != "cellular" and re.search(cellular_words, main) and mixed_bt:
        collect = collect + ["cellular"]  # e.g. "watch for trackers and rogue cells": SDR + modem together
    elif task.profile.name == "cellular" and mixed_bt:
        rest = re.sub(cellular_words, " ", probe)
        if trackers and not re.search(r"\b(?:bluetooth|ble|btle)\b", rest):
            rest = "find ble devices " + rest  # trackers, AirTags and Tiles are BLE advertisers
        other = parse(rest)
        collect = [other.profile.name, *other.companions, "cellular"]
        if trackers and other.profile.name == "ble":
            collect = ["ble", "cellular"]
    filters = _conditions(main)
    notes = []
    if any(c.field == "rssi_dbfs" for c in filters + [w for t in triggers for w in t.when]):
        notes.append("For cells, signal is RSRP in dBm reported by the modem." if collect == ["cellular"] else
                     "Signal strength is relative (dBFS at the receiver), not calibrated dBm.")
    if len(triggers) < len(trigger_clauses):
        notes.append("Some 'if/when' clauses had no action I recognise and were left out.")
    if collect == ["drone"] and re.search(r"\bconfirm(?:ed|s)?\b", lower):
        # "alert me when one is confirmed": the clause names no drone, but in a drone watch "confirmed" can only
        # mean the detector's confirmation, never just any drone-like link.
        for trigger in triggers:
            if not any(c.field == "device_class" for c in trigger.when):
                trigger.when.append(Condition(field="device_class", op="eq", value="drone"))
    return WorkflowSpec(goal=raw, collect=collect, duration_s=task.duration_s, device_id=task.device_id,
                        channel=task.channel if task.profile.name == "zigbee" else None,
                        filters=filters, triggers=triggers, notes=notes)


PLANNER_INSTRUCTIONS = """\
You turn an RF operator's request into a WorkflowSpec for RF Sentinel, a RECEIVE-ONLY RF monitoring system.

- collect: protocols to receive. "ble", "classic" (Bluetooth Classic / BR/EDR), "zigbee", "tpms", "drone" (drone RF
  links via the station's drone detector: a drone the detector confirmed has device_class "drone"; a drone-like
  link it could not confirm has device_class "drone link"; "confirmed" always means device_class eq "drone"), or "cellular"
  (cell towers via a cellular modem: fields operator, rat ("LTE", "5G NR", "3G WCDMA", "2G GSM"), generation
  ("2G".."5G"), role ("serving", "neighbour", "scanned"); rssi_dbfs holds RSRP in dBm for cells). A plain
  "bluetooth" request means ["ble", "classic"]. Trackers, AirTags, Tiles and beacons are BLE.
- duration_s: 5 to 600. Default 30 if not stated.
- filters: conditions a device must meet to be shown (all must hold).
- triggers: "when <conditions> do <actions>". Conditions use the same fields. repeat=false fires once per device;
  "whenever", "each time" or "every time" mean repeat=true.
- "watch for trackers" means a filter device_class eq "tracker"; "airtag" means label contains "AirTag"; text
  conditions use contains unless the operator gives an exact value; keep the operator's capitalisation of names.
- "when the serving cell changes" means a trigger when [role eq "serving"] (it fires once for each new serving
  cell); it is not a filter.
Condition fields: protocol, label (best identity, e.g. "AirTag", a local name or vendor), name, manufacturer,
device_type (e.g. Phone, Watch), device_class ("tracker" or "beacon"), address, rssi_dbfs (relative, e.g. -60),
count (detections so far), dwell_s (seconds between first and last sighting: use for "following me" /
"around for N minutes"), new (true when never seen in earlier runs).
Operators: eq, ne, contains, not_contains (text); ge, le, eq, ne (numbers).
Actions: alert(message, severity info|warning|critical), tag(label), record_iq(seconds <= 10),
start_scan(protocol, seconds <= 300), extend(seconds), stop, inquiry_scan(seconds <= 30), name_lookup.
inquiry_scan and name_lookup make the station TRANSMIT (host Bluetooth adapter); RF Sentinel asks a named operator to
approve each one before it runs. Use them only when the operator asks for an inquiry scan or a device's name.
Examples (request -> spec):
- "watch for trackers for 5 minutes, only stronger than -60. if a tracker is seen for more than 2 minutes, send an
  urgent alert and record 5 seconds of iq" -> collect ["ble"], duration_s 300, filters [device_class eq tracker,
  rssi_dbfs ge -60], triggers [when [device_class eq tracker, dwell_s ge 120] do [alert critical, record_iq 5]]
- "find bluetooth devices for 3 minutes. when a new device stronger than -45 appears, alert me" -> collect
  ["ble", "classic"], duration_s 180, triggers [when [rssi_dbfs ge -45, new eq true] do [alert warning]]
- "survey cell towers for 3 minutes. send an urgent alert if an unknown network appears or if a 2G cell shows up"
  -> collect ["cellular"], duration_s 180, triggers [when [operator eq "unknown operator"] do [alert critical]],
  [when [generation eq "2G"] do [alert critical]] ("or" means separate triggers sharing the actions)
- "find apple devices for 1 minute" -> collect ["ble"] (vendors come from BLE advertisements), filters
  [manufacturer contains "Apple"]
- "watch for trackers and rogue cell towers for 2 minutes ..." -> collect ["ble", "cellular"] (one SDR protocol
  group plus the cellular modem is allowed)
Use exactly the values the operator gave; do not invent filters or triggers they did not ask for.
Never plan transmitting, jamming, spoofing, replaying, or paging. If the request asks for that, return a spec
that only receives and explain in notes. Keep the operator's wording in goal. Put assumptions in notes.
"""


def default_planner_model() -> str | None:
    configured = os.getenv("RF_SENTINEL_PLANNER_MODEL", "").strip()
    if configured:
        return None if configured.lower() in ("none", "off", "deterministic") else configured
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic:claude-opus-5-5"
    if (Path.home() / ".codex" / "auth.json").exists():
        return "openai-codex:gpt-6-astra"
    return None


def plan(text: str, planner: str = "auto", model: Any = None) -> dict[str, Any]:
    """Plan with the model when available (planner="auto"/"model"), else the deterministic grammar.

    Returns {"spec", "summary", "planner", "model", "fallback_reason"}. The spec is validated either way.
    """
    reason = None
    model = model or (default_planner_model() if planner in ("auto", "model") else None)
    if planner != "deterministic" and model is not None:
        try:
            spec = _plan_with_model(text, model)
            return {"spec": spec.model_dump(), "summary": spec.summary(), "planner": "model",
                    "model": model if isinstance(model, str) else type(model).__name__, "fallback_reason": None}
        except Exception as exc:  # any model failure falls back to the deterministic grammar
            reason = _short_reason(exc)
    elif planner == "model":
        reason = "no planner model configured (set RF_SENTINEL_PLANNER_MODEL)"
    spec = plan_deterministic(text)
    return {"spec": spec.model_dump(), "summary": spec.summary(), "planner": "deterministic", "model": None,
            "fallback_reason": reason}


def _short_reason(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "RateLimit" in type(exc).__name__ or "usage_limit" in text:
        resets = re.search(r"'resets_in_seconds': (\d+)", text)
        return "model rate-limited" + (f", resets in ~{int(resets.group(1)) // 3600} h" if resets else "")
    if "401" in text or "403" in text or "auth" in text.lower():
        return "model login unavailable"
    return f"{type(exc).__name__}: {text[:120]}"


def _plan_with_model(text: str, model: Any) -> WorkflowSpec:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    from pydantic_ai import Agent, ModelRetry, UsageLimits
    agent = Agent(model, output_type=WorkflowSpec, instructions=PLANNER_INSTRUCTIONS, retries=2)

    @agent.output_validator
    def receive_only(spec: WorkflowSpec) -> WorkflowSpec:
        if re.search(r"\b(transmit|jam|spoof|replay|page)\b", " ".join(spec.notes).lower()) and spec.triggers:
            pass  # notes may mention a refusal; actions are already restricted by the schema
        if not spec.goal:
            spec.goal = text
        if not spec.collect:
            raise ModelRetry("collect must name at least one protocol")
        known = {"tracker", "beacon", "drone", "drone link"}
        conditions = spec.filters + [c for t in spec.triggers for c in t.when]
        bad = sorted({str(c.value) for c in conditions if c.field == "device_class" and str(c.value) not in known})
        if bad:  # a class no device can have makes a rule that never fires
            raise ModelRetry(f"device_class must be one of {sorted(known)}, not {bad}")
        return spec

    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", text.lower()):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    if re.search(r"\bwi-?fi\b", text.lower()) and not re.search(r"\b(cell|cellular|lte|5g)\b", text.lower()):
        raise ValueError("WiFi needs a monitor-mode WiFi adapter, not an SDR; it is not a workflow source.")
    return agent.run_sync(text, usage_limits=UsageLimits(request_limit=4)).output


# --------------------------------------------------------------------------------------------- execution
def device_class(record: dict[str, Any]) -> str:
    if record.get("protocol") == "drone":  # the detector's own verdict, never inferred from the label
        return "drone" if record.get("device_type") == CONFIRMED_DRONE else "drone link"
    text = " ".join(str(record.get(k) or "") for k in ("label", "name", "manufacturer", "device_type")).lower()
    if any(word in text for word in TRACKER_WORDS):
        return "tracker"
    if "beacon" in text or "ibeacon" in text or "eddystone" in text:
        return "beacon"
    return ""


def _matches(condition: Condition, record: dict[str, Any]) -> bool:
    value = record.get(condition.field)
    if condition.field in NUMBER_FIELDS:
        if value is None:
            return False
        target = float(condition.value)
        return {"ge": value >= target, "le": value <= target, "eq": value == target, "ne": value != target}[condition.op]
    if condition.field == "new":
        return (bool(value) == condition.value) == (condition.op == "eq")
    text, target = str(value or "").lower(), str(condition.value).lower()
    return {"eq": text == target, "ne": text != target, "contains": target in text,
            "not_contains": target not in text}[condition.op]


def runs_dir() -> Path:
    return Path(os.getenv("RF_SENTINEL_AGENT_RUNS_DIR") or ".demo/agent-runs")


CELL_FIELDS = {"operator", "rat", "generation", "role"}


def filter_applies(condition: Condition, record: dict[str, Any], mixed: bool) -> bool:
    """In a workflow that mixes SDR protocols with the cellular modem, a filter describes one side only:
    cell fields filter cells; everything else (tracker kind, dBFS signal, name, ...) filters SDR devices."""
    if not mixed:
        return True
    is_cell = record.get("protocol") == "cellular"
    return (condition.field in CELL_FIELDS) == is_cell


def known_devices_path(base: Path | None = None, simulated: bool = False) -> Path:
    # Simulated runs keep their own list, so synthetic devices never make real ones look "previously seen".
    return (base or runs_dir()) / ("known_devices.simulated.json" if simulated else "known_devices.json")


class WorkflowEngine:
    """Applies a spec's filters and triggers to the agent's live detections and carries out actions."""

    def __init__(self, spec: WorkflowSpec, agent, step: Callable[[str], None],
                 on_event: Callable[[dict[str, Any]], None] | None = None, evidence_dir: Path | None = None,
                 base_dir: Path | None = None, focus: set[str] | None = None,
                 carried_tags: dict[str, list[str]] | None = None):
        self.spec, self.agent, self.step = spec, agent, step
        # A playbook step can focus on devices an earlier step found: anything else is ignored by this engine.
        self.focus = set(focus) if focus is not None else None
        self.carried_tags = carried_tags or {}
        self.on_event = on_event or (lambda e: None)
        self.base_dir = base_dir or runs_dir()
        self.evidence_dir = evidence_dir or self.base_dir / "evidence"
        self.devices: dict[str, dict[str, Any]] = {}
        self.matched: set[str] = set()
        self.fired: dict[tuple[int, str], float] = {}
        self.timeline: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.follow_ups: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._record_lock = threading.Lock()
        self._trigger_conditions: dict[str, list[Condition]] = {}
        self.evidence: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []
        self._started = time.time()
        self._simulated = bool(getattr(agent, "simulate", False))
        try:
            self.known = set(json.loads(known_devices_path(self.base_dir, self._simulated).read_text()))
        except (OSError, ValueError):
            self.known = set()

    # events ---------------------------------------------------------------------------------
    def _emit(self, kind: str, **fields) -> dict[str, Any]:
        event = {"t": round(time.time() - self._started, 1), "kind": kind, **fields}
        with self._lock:
            self.timeline.append(event)
        self.on_event(event)
        return event

    def observe(self, event: dict[str, Any]) -> None:
        key = event.get("agent_key")
        old = event.get("supersedes")
        if key and old and old != key and self.focus is not None and old in self.focus:
            self.focus.add(key)  # a focused cell, now known by its cell ID
        if not key or (self.focus is not None and key not in self.focus):
            return
        now = time.time()
        with self._lock:
            if old and old != key and old in self.devices:
                self._fold(old, key)
            record = self.devices.get(key)
            if record is None:
                manufacturer = event.get("manufacturer")
                record = self.devices[key] = {
                    "key": key, "protocol": event.get("agent_profile", ""), "first_seen": now, "count": 0,
                    "label": event.get("identity") or event.get("name") or (f"LAP {event['lap']}" if event.get("lap") else key),
                    "name": event.get("name") or "", "address": event.get("address") or event.get("lap") or "",
                    "manufacturer": (manufacturer or {}).get("company_name", "") if isinstance(manufacturer, dict) else str(manufacturer or ""),
                    "device_type": event.get("device_type_detail") or event.get("device_type") or "",
                    "new": key not in self.known, "tags": list(self.carried_tags.get(key, [])), "best_rssi_dbfs": None}
                for field in ("operator", "rat", "generation", "role"):
                    record[field] = event.get(field) or ""
                if record["protocol"] == "cellular":
                    record["manufacturer"] = record["operator"]
                    record["device_type"] = record["rat"]
                record["device_class"] = device_class(record)
            record["count"] += 1
            record["last_seen"] = now
            if record["protocol"] == "drone" and event.get("device_type") == CONFIRMED_DRONE != record["device_type"]:
                # A track first seen as a drone-like link, now confirmed by the detector: same record, upgraded.
                record["device_type"], record["label"] = CONFIRMED_DRONE, event.get("identity") or record["label"]
                record["device_class"] = device_class(record)
            if record["protocol"] == "cellular" and event.get("operator") and not str(event["operator"]).startswith(("unverified", "PLMN not")):
                # A later full scan can resolve a cell first seen with an unverified PLMN.
                record["operator"] = record["manufacturer"] = event["operator"]
                record["label"] = event.get("identity") or record["label"]
            record["dwell_s"] = round(now - record["first_seen"], 1)
            rssi = event.get("rssi_dbfs")
            if isinstance(rssi, (int, float)):
                record["rssi_dbfs"] = float(rssi)
                best = record["best_rssi_dbfs"]
                record["best_rssi_dbfs"] = float(rssi) if best is None else max(best, float(rssi))
            if event.get("role") == "serving" or not record.get("role"):
                record["role"] = event.get("role") or record.get("role", "")
            for field in ("channel", "center_freq_hz"):
                if event.get(field) is not None:
                    record[field] = event[field]
            mixed = "cellular" in self.spec.collect and bool(self.spec.sdr_collect())
            in_scope = all(_matches(c, record) for c in self.spec.filters if filter_applies(c, record, mixed))
            if in_scope and key not in self.matched:
                self.matched.add(key)
                self._emit("match", device=self._public(record))
            if not in_scope:
                return
            for index, trigger in enumerate(self.spec.triggers):
                if not all(_matches(c, record) for c in trigger.when):
                    continue
                last = self.fired.get((index, key))
                if last is not None and (not trigger.repeat or now - last < trigger.cooldown_s):
                    continue
                self.fired[(index, key)] = now
                self._trigger_conditions[key] = [c for c in trigger.when if not (c.field == "count" and c.value == 1)]
                snapshot = self._public(record)
                self._emit("trigger", trigger=index, rule=trigger.describe(), device=snapshot)
                thread = threading.Thread(target=self._run_actions, args=(index, trigger, snapshot),
                                          name=f"workflow-trigger-{index}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def _fold(self, old: str, key: str) -> None:
        """One physical cell seen under two keys (PCI-only, then named): keep one record, fired rules carry over."""
        earlier = self.devices.pop(old)
        record = self.devices.get(key)
        if record is None:
            self.devices[key] = {**earlier, "key": key, "new": earlier["new"] and key not in self.known}
        else:
            record["new"] = record["new"] and earlier["new"]
            record["count"] += earlier["count"]
            record["first_seen"] = min(record["first_seen"], earlier["first_seen"])
            record["dwell_s"] = round(record["last_seen"] - record["first_seen"], 1)
            if earlier.get("best_rssi_dbfs") is not None:
                record["best_rssi_dbfs"] = max(earlier["best_rssi_dbfs"], record.get("best_rssi_dbfs") or -200.0)
            record["tags"] = list(dict.fromkeys((record.get("tags") or []) + (earlier.get("tags") or [])))
        if old in self.matched:
            self.matched.discard(old)
            self.matched.add(key)
        for (index, fired_key), at in list(self.fired.items()):
            if fired_key == old:
                self.fired.setdefault((index, key), self.fired.pop((index, fired_key)))
        if old in self._trigger_conditions:
            self._trigger_conditions.setdefault(key, self._trigger_conditions.pop(old))

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: record.get(k) for k in ("key", "protocol", "label", "manufacturer", "device_type", "device_class",
                                           "rssi_dbfs", "best_rssi_dbfs", "count", "dwell_s", "new", "tags", "channel",
                                           "operator", "rat", "generation", "role")}

    # actions --------------------------------------------------------------------------------
    def _run_actions(self, index: int, trigger: Trigger, device: dict[str, Any]) -> None:
        from .gateway import PermissionDenied
        for action in trigger.do:
            if self.agent._stop.is_set():
                return
            try:
                result = getattr(self, f"_do_{action.kind}")(action, device)
                self._emit("action", trigger=index, action=action.kind, status="done", detail=result, device_key=device["key"])
            except PermissionDenied as exc:
                self._emit("action", trigger=index, action=action.kind, status="denied", detail=str(exc), device_key=device["key"])
                self.step(f"Trigger action {action.describe()} not carried out: {exc}")
            except Exception as exc:
                self._emit("action", trigger=index, action=action.kind, status="failed",
                           detail=f"{type(exc).__name__}: {exc}"[:300], device_key=device["key"])
                self.step(f"Trigger action {action.describe()} failed: {exc}")

    def _do_alert(self, action: Action, device: dict[str, Any]) -> str:
        self.agent.gateway.authorize("raise_alert", {"device": device["key"], "severity": action.severity})
        reason = " and ".join(c.describe() for c in self._trigger_conditions.get(device["key"], []))
        unit = "dBm" if device.get("protocol") == "cellular" else "dBFS"
        reason = reason.replace(" dBFS", f" {unit}")
        now = round(time.time() - self._started, 1)
        with self._lock:
            # Several rules matching the same device at once become one alert with every reason.
            recent = next((a for a in reversed(self.alerts) if a["device"]["key"] == device["key"]
                           and now - a["t"] <= 2.0 and not action.message and not a.get("custom")
                           and reason and reason not in a["reasons"]), None)
            if recent is not None:
                if reason and reason not in recent["reasons"]:
                    recent["reasons"].append(reason)
                    recent["message"] = f"{device['label']}: " + "; ".join(recent["reasons"])
                if action.severity == "critical":
                    recent["severity"] = "critical"
                self._emit("alert", severity=recent["severity"], message=recent["message"], device=device, merged=True)
                return recent["message"]
            message = action.message or (f"{device['label']}: {reason}" if reason else f"{device['label']} matched")
            alert = {"t": now, "severity": action.severity, "message": message, "device": device,
                     "reasons": [reason] if reason else [], "custom": bool(action.message)}
            self.alerts.append(alert)
        self._emit("alert", severity=action.severity, message=message, device=device)
        self._deliver(alert)
        rssi = device.get("rssi_dbfs")
        self.step(f"ALERT ({action.severity}): {message}" + (f" ({rssi:.0f} {unit})" if isinstance(rssi, (int, float)) else ""))
        return message

    def _deliver(self, alert: dict[str, Any]) -> None:
        """Also send the alert to the operator's webhook, when one is configured (off by default)."""
        from . import notify
        if not notify.webhook_url():
            return
        host = notify.describe()["host"]
        try:
            self.agent.gateway.authorize("deliver_alert", {"host": host, "severity": alert["severity"]})
        except Exception as exc:
            self._emit("delivery", status="denied", detail=str(exc), device_key=alert["device"]["key"])
            return
        thread = notify.deliver(alert, self.agent.gateway.run_id, self._simulated,
                                on_done=lambda status, detail: self._emit("delivery", status=status, detail=detail,
                                                                          device_key=alert["device"]["key"]))
        if thread is not None:
            self._threads.append(thread)

    def _do_tag(self, action: Action, device: dict[str, Any]) -> str:
        self.agent.gateway.authorize("tag_device", {"device": device["key"], "label": action.label})
        with self._lock:
            tags = self.devices[device["key"]]["tags"]
            if action.label not in tags:
                tags.append(action.label)
        return action.label

    def _do_extend(self, action: Action, device: dict[str, Any]) -> str:
        self.agent.gateway.authorize("extend_scan", {"seconds": action.seconds, "limit_s": MAX_DURATION_S})
        added = self.agent.extend(action.seconds, MAX_DURATION_S)
        self.step(f"Extended collection by {added:.0f} s (trigger on {device['label']})")
        return f"+{added:.0f} s"

    def _do_stop(self, action: Action, device: dict[str, Any]) -> str:
        self.agent.gateway.authorize("stop_scan", {"reason": "workflow trigger", "device": device["key"]})
        self.agent.finish_early(f"workflow trigger on {device['label']}")
        return "collection ending"

    def _do_inquiry_scan(self, action: Action, device: dict[str, Any]) -> str:
        """Transmits: a BR/EDR inquiry on the host adapter, after a named operator approves it."""
        from . import transmit
        if getattr(self, "_inquiry_ran", False):
            return "inquiry scan already ran in this run"
        self._inquiry_ran = True  # once per run, approved or not: never a stream of approval prompts
        self.agent.gateway.authorize("bt_inquiry", {"seconds": action.seconds, "trigger_device": device["key"]})
        if getattr(self.agent, "simulate", False):
            return f"simulated inquiry scan ({action.seconds:g} s); nothing transmitted"
        transmit.inquiry(action.seconds)
        self.step(f"Ran an approved Bluetooth inquiry scan for {action.seconds:g} s (trigger on {device['label']})")
        return f"inquiry scan {action.seconds:g} s"

    def _do_name_lookup(self, action: Action, device: dict[str, Any]) -> str:
        """Transmits: a remote name request to one device (needs its full address), after approval."""
        from . import transmit
        address = transmit.full_address(device.get("address"))
        if address is None:
            raise RuntimeError(f"{device['label']} has no full Bluetooth address (only "
                               f"{device.get('address') or 'a partial id'}), so its name cannot be requested")
        self.agent.gateway.authorize("bt_remote_name", {"address": address})
        name = f"Simulated device {address[-5:]}" if getattr(self.agent, "simulate", False) else transmit.remote_name(address)
        if name:
            with self._lock:
                record = self.devices.get(device["key"])
                if record is not None:
                    record["name"] = name
        self.step(f"Approved name request to {address}: {name or 'no answer'}")
        return name or "no answer"

    def _do_record_iq(self, action: Action, device: dict[str, Any]) -> str:
        if device["protocol"] == "cellular":
            raise RuntimeError("IQ capture applies to SDR detections; cells come from the modem, which has no IQ output")
        from .capabilities import CLASSIC_RATE_SPS
        from .radio import capture_iq
        freq = device.get("center_freq_hz") or {37: 2_402_000_000, 38: 2_426_000_000, 39: 2_480_000_000}.get(
            device.get("channel"), 2_442_000_000 if device["protocol"] == "classic" else 2_402_000_000)
        rate = CLASSIC_RATE_SPS if device["protocol"] == "classic" else 4_000_000
        with self._record_lock:  # one capture at a time; wait for a receiver to free up rather than failing
            return self._record(action, device, freq, rate)

    def _record(self, action: Action, device: dict[str, Any], freq: int, rate: int) -> str:
        from .radio import capture_iq
        deadline = time.monotonic() + 20
        while True:
            try:
                receiver = self.agent.radio.choose((int(freq),), rate)  # a free receiver; the scan keeps its own
                break
            except RuntimeError:
                if time.monotonic() > deadline or self.agent._stop.is_set():
                    raise
                time.sleep(0.5)
        params = {"device_id": receiver["id"], "center_freq_hz": int(freq), "seconds": action.seconds}
        self.agent.gateway.authorize("record_iq", params)
        gain = 46.0
        if getattr(self.agent, "simulate", False):
            import random
            rng = random.Random(hash(device["key"]) & 0xFFFF)
            n = int(min(action.seconds, 1.0) * rate) * 2  # a short synthetic noise file stands in for the capture
            data = bytes((int(rng.gauss(0, 12)) & 0xFF) for _ in range(min(n, 400_000)))
        else:
            data = capture_iq(self.agent.radio, receiver, int(freq), rate, gain, action.seconds)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.agent.gateway.run_id}-{re.sub(r'[^A-Za-z0-9]+', '', device['key'])[:24]}-{int(time.time())}"
        path = self.evidence_dir / f"{stem}.cs8"
        path.write_bytes(data)
        meta = {"format": "cs8", "device_id": receiver["id"], "center_freq_hz": int(freq), "sample_rate_sps": rate,
                "gain_db": gain, "seconds": len(data) / 2 / rate, "sha256": hashlib.sha256(data).hexdigest(),
                "trigger_device": device, "run_id": self.agent.gateway.run_id, "recorded_at": time.time(),
                "simulated": bool(getattr(self.agent, "simulate", False))}
        path.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
        with self._lock:
            self.evidence.append({"file": path.name, "path": str(path), "sha256": meta["sha256"],
                                  "seconds": round(meta["seconds"], 2), "receiver": receiver["id"],
                                  "center_freq_hz": int(freq), "device": device["key"]})
        self.step(f"Recorded {meta['seconds']:.1f} s of IQ on {receiver['id']} at {freq / 1e6:.0f} MHz -> {path.name}")
        return str(path)

    def _do_start_scan(self, action: Action, device: dict[str, Any]) -> str:
        from .profiles import PROFILES
        profile = PROFILES[action.protocol]
        receiver = self.agent.radio.choose(profile.freqs_hz, profile.sample_rate_sps)
        what = {"ble": "ble devices", "classic": "bluetooth classic devices", "zigbee": "zigbee",
                "tpms": "tire sensors", "cellular": "cell towers", "drone": "drones"}[action.protocol]
        child = self.agent.child()
        label = PROTOCOL_TITLES[action.protocol]
        self.step(f"Starting a {label} scan on {receiver['id']} for {action.seconds:.0f} s (trigger on {device['label']})")
        result = child.run(f"find {what} for {int(action.seconds)} seconds on {receiver['id']}",
                           on_step=lambda m: self.step(f"[{label} follow-up] {m}"), on_detection=self.observe)
        summary = {"protocol": action.protocol, "receiver": receiver["id"], "devices": len(result["devices"]),
                   "detections": sum(d["count"] for d in result["devices"]), "trigger_device": device["key"],
                   "result_devices": result["devices"]}
        with self._lock:
            self.follow_ups.append(summary)
        return f"{summary['devices']} {label} device(s)"

    # results --------------------------------------------------------------------------------
    def finish(self, timeout_s: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        joined = 0
        while joined < len(self._threads):  # actions may start more threads (e.g. alert delivery) while we wait
            self._threads[joined].join(max(0.0, deadline - time.monotonic()))
            joined += 1
        with self._lock:
            matched = [self._public(self.devices[k]) for k in self.matched]
            try:
                path = known_devices_path(self.base_dir, self._simulated)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(sorted(self.known | set(self.devices))))
            except OSError:
                pass
            return {"spec": self.spec.model_dump(), "summary": self.spec.summary(),
                    "matched": sorted(matched, key=lambda d: -(d["count"] or 0)),
                    "all_devices": [self._public(r) for r in self.devices.values()],
                    "seen": len(self.devices), "alerts": self.alerts, "timeline": self.timeline,
                    "triggers_fired": sum(1 for e in self.timeline if e["kind"] == "trigger"),
                    "evidence": list(self.evidence),
                    "follow_up_scans": [{k: v for k, v in f.items() if k != "result_devices"} for f in self.follow_ups]}


def narrative(result: dict[str, Any]) -> list[str]:
    """Plain-language account of a finished workflow run, built only from the run's own records."""
    wf = result.get("workflow") or {}
    lines: list[str] = []
    seen, matched = wf.get("seen", 0), len(wf.get("matched") or [])
    what = (wf.get("summary") or {}).get("find", "")
    prefix = "Simulated run. " if result.get("simulated") else ""
    lines.append(f"{prefix}Collected {what} and saw {seen} device(s); {matched} matched the filters."
                 if (wf.get("spec") or {}).get("filters") else f"{prefix}Collected {what} and saw {seen} device(s).")
    new = [d for d in wf.get("all_devices") or [] if d.get("new")]
    if wf.get("all_devices") is not None:
        if new:
            names = list(dict.fromkeys(d.get("label") or d.get("key") for d in new))
            lines.append(f"{len(new)} device(s) not seen in any earlier run: {', '.join(names[:5])}{'…' if len(names) > 5 else ''}.")
        elif seen:
            lines.append("Every device had been seen in an earlier run.")
    by_rule: dict[str, list[str]] = {}
    for event in wf.get("timeline") or []:
        if event["kind"] == "trigger":
            by_rule.setdefault(event["rule"], []).append((event.get("device") or {}).get("label") or "?")
    for rule, labels in by_rule.items():
        unique = list(dict.fromkeys(labels))
        lines.append(f"Rule fired {len(labels)}×: {rule.split(' → ')[0].replace('when ', 'when ')} "
                     f"(on {', '.join(unique[:4])}{'…' if len(unique) > 4 else ''}).")
    if not by_rule and (wf.get("spec") or {}).get("triggers"):
        lines.append("No trigger conditions were met.")
    alerts = wf.get("alerts") or []
    if alerts:
        critical = sum(1 for a in alerts if a["severity"] == "critical")
        lines.append(f"{len(alerts)} alert(s) raised" + (f", {critical} critical." if critical else "."))
    if wf.get("evidence"):
        lines.append(f"{len(wf['evidence'])} IQ evidence file(s) captured, each with a SHA-256 sidecar.")
    for follow in wf.get("follow_up_scans") or []:
        lines.append(f"Follow-up {PROTOCOL_TITLES.get(follow['protocol'], follow['protocol'])} scan on "
                     f"{follow['receiver']} found {follow['devices']} device(s).")
    denied = [e for e in wf.get("timeline") or [] if e["kind"] == "action" and e.get("status") == "denied"]
    if denied:
        lines.append(f"{len(denied)} action(s) were not carried out because the operating mode does not allow them.")
    if result.get("stop_reason") and not result.get("stopped"):
        lines.append(f"Collection ended early: {result['stop_reason']}.")
    elif result.get("stopped"):
        lines.append("Stopped by the operator.")
    return lines


def run_workflow(agent, spec: WorkflowSpec, on_step: Callable[[str], None] = print,
                 on_detection: Callable[[dict[str, Any]], None] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 gain_db: float | None = None, planner: dict[str, Any] | None = None,
                 base_dir: Path | None = None, focus: set[str] | None = None,
                 carried_tags: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Run a validated spec: deterministic collection by the agent, filters and triggers by the engine."""
    engine = WorkflowEngine(spec, agent, on_step, on_event, base_dir=base_dir, focus=focus, carried_tags=carried_tags)
    on_step(f"Workflow: {spec.summary()['find']}"
            + (f"; keep {' and '.join(spec.summary()['filters'])}" if spec.filters else "")
            + (f"; {len(spec.triggers)} trigger(s)" if spec.triggers else ""))

    def observe(event: dict[str, Any]) -> None:
        if on_detection:
            on_detection(event)
        engine.observe(event)

    cell_thread, cell_out = None, {}
    if "cellular" in spec.collect and spec.sdr_collect():
        # The modem needs no SDR: survey cells alongside the SDR collection, same gateway, stop and engine.
        cell_agent = agent.child()

        def survey():
            try:
                cell_out["result"] = cell_agent.run(spec.command(["cellular"]), on_step=lambda m: on_step(f"[Cellular] {m}"),
                                                    on_detection=observe)
            except Exception as exc:
                cell_out["error"] = f"{type(exc).__name__}: {exc}"
                on_step(f"[Cellular] not run: {exc}")
        cell_thread = threading.Thread(target=survey, name="workflow-cellular", daemon=True)
        cell_thread.start()
    try:
        result = agent.run(spec.command(), on_step=on_step, on_detection=observe, gain_db=gain_db)
    finally:
        if cell_thread is not None:
            cell_agent.finish_early("primary collection ended")
            cell_thread.join(timeout=60)
    if cell_thread is not None:
        cell = cell_out.get("result")
        result["cellular"] = ({"devices": len(cell["devices"]), "receiver": cell["receiver"]} if cell
                              else {"error": cell_out.get("error")})
        if cell:
            result["devices"] = result["devices"] + cell["devices"]
            result["audit"] = result["audit"]  # shared gateway: the audit already holds both
    heard = len(result["devices"])
    if focus is not None:
        result["devices"] = [d for d in result["devices"] if d["key"] in engine.focus]
    result["workflow"] = engine.finish()
    result["workflow"]["planner"] = planner or {"planner": "deterministic"}
    if focus is not None:
        result["workflow"]["focus"] = sorted(engine.focus)
        result["workflow"]["heard"] = heard  # everything decoded, focused on or not
    for follow in engine.follow_ups:
        result["devices"] = result["devices"] + follow["result_devices"]
    result["workflow"]["narrative"] = narrative(result)
    from .explain import explain_workflow
    result["workflow"]["explain"] = explain_workflow(spec)
    tags = {k: v["tags"] for k, v in engine.devices.items() if v["tags"]}
    for device in result["devices"]:
        if device["key"] in tags:
            device["tags"] = tags[device["key"]]
    return result
