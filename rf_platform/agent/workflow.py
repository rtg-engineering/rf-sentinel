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

from .profiles import MAX_DURATION_S

Protocol = Literal["ble", "classic", "zigbee", "tpms"]
FieldName = Literal["protocol", "label", "name", "manufacturer", "device_type", "device_class", "address",
                    "rssi_dbfs", "count", "dwell_s", "new"]
Op = Literal["eq", "ne", "contains", "not_contains", "ge", "le"]
TEXT_FIELDS = {"protocol", "label", "name", "manufacturer", "device_type", "device_class", "address"}
NUMBER_FIELDS = {"rssi_dbfs", "count", "dwell_s"}
MAX_RECORD_S = 10
MAX_FOLLOW_UP_S = 300
# Tracker kinds. "find my" alone is not enough: iPhones, Macs and AirPods send Find My frames too.
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
    kind: Literal["alert", "tag", "record_iq", "start_scan", "extend", "stop"]
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
        return self

    def describe(self) -> str:
        return {"alert": lambda: f"alert ({self.severity})" + (f": {self.message}" if self.message else ""),
                "tag": lambda: f'tag as "{self.label}"',
                "record_iq": lambda: f"record {self.seconds:g} s of IQ evidence",
                "start_scan": lambda: f"scan {PROTOCOL_TITLES[self.protocol]} for {self.seconds:g} s",
                "extend": lambda: f"keep watching {self.seconds:g} s longer",
                "stop": lambda: "stop collection"}[self.kind]()


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
        if len(ordered) > 1 and set(ordered) != {"ble", "classic"}:
            raise ValueError("one protocol per run, except BLE together with Bluetooth Classic")
        return ordered

    def command(self) -> str:
        """The bounded collection request the deterministic agent executes."""
        protocols = set(self.collect)
        what = ("bluetooth devices" if protocols == {"ble", "classic"} else "ble devices" if protocols == {"ble"}
                else "bluetooth classic devices" if protocols == {"classic"} else
                "zigbee" if protocols == {"zigbee"} else "tire sensors")
        return f"find {what} for {self.duration_s} seconds" + (f" on {self.device_id}" if self.device_id else "")

    def summary(self) -> dict[str, Any]:
        return {"find": f"{' + '.join(PROTOCOL_TITLES[p] for p in self.collect)} for {self.duration_s} s",
                "filters": [c.describe() for c in self.filters], "triggers": [t.describe() for t in self.triggers]}


PROTOCOL_TITLES = {"ble": "BLE", "classic": "Bluetooth Classic", "zigbee": "Zigbee", "tpms": "TPMS"}


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
    (rf"\b(?:scan|look|sweep|check|listen)\s+(?:for\s+)?(bluetooth classic|classic bluetooth|classic|br/edr|zigbee|tpms|tire sensors?|ble)"
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
    (r"\bstop\b(?: (?:the )?(?:scan|run|collection|watching))?", lambda m: Action(kind="stop")),
]


def _protocol_word(word: str) -> str:
    word = word.lower()
    return ("classic" if "classic" in word or "br/edr" in word else "zigbee" if "zigbee" in word
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
    for noun, (field, op, value) in DEVICE_NOUNS.items():
        pattern = rf"\b{noun}\b(?! (?:for|out|over|over|it))" if noun == "watch" else rf"\b{noun}\b"
        if noun == "watch" and re.match(r"\s*watch\b", text):
            continue  # "watch for ..." is the verb
        if re.search(pattern, text) and not any(c.field == field and c.value == value for c in found):
            found.append(Condition(field=field, op=("ne" if op == "eq" else "not_contains") if neg else op, value=value))
    m = re.search(r"\bnamed [\"']?([\w\- ]+?)[\"']?(?:\s+(?:is|are|appears?|shows?|stronger|for|and|,)\b|$)", text)
    if m:
        found.append(Condition(field="name", op="contains", value=m.group(1).strip()))
    if re.search(r"\b(?:new|unknown|unfamiliar|never seen)\b(?! (?:than|scan))|\banything new\b", text):
        found.append(Condition(field="new", op="eq", value=True))
    m = re.search(rf"\b(?:seen|around|present|following|stays?|persists?|lingers?)\b[^.]*?\bfor (?:more than |over |at least )?(\d+(?:\.\d+)?)\s*({UNIT_S})", text)
    if m:
        found.append(Condition(field="dwell_s", op="ge", value=_seconds(m.group(1), m.group(2))))
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
        return None
    first_action = min(a for a, _ in spans)
    condition_text = clause[:first_action]
    when = _conditions(condition_text)
    if not when:
        # "if anything shows up" / "when something appears": any device in scope
        when = [Condition(field="count", op="ge", value=1)]
    order = sorted(range(len(actions)), key=lambda i: spans[i][0])
    return Trigger(when=when, do=[actions[i] for i in order], repeat=repeat)


def plan_deterministic(text: str) -> WorkflowSpec:
    """Plain-language request → WorkflowSpec with a fixed grammar. Raises ValueError when unsure."""
    from .profiles import IntentError, parse
    raw = " ".join(str(text).split())
    lower = raw.lower()
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", lower):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    main, trigger_clauses = _split_triggers(lower)
    triggers = []
    for clause in trigger_clauses:
        trigger = _parse_trigger(clause)
        if trigger is None:
            # "<action> when <condition>": the action is the end of the preceding text ("... and stop when ...")
            tail = re.split(r",|\band\b|\bthen\b", main)[-1]
            trigger = _parse_trigger(clause + " " + tail) if tail.strip() else None
            if trigger is not None:
                main = main[: len(main) - len(tail)].rstrip(" ,").removesuffix(" and").removesuffix(" then")
        if trigger is not None:
            triggers.append(trigger)
    trackers = bool(re.search(r"\b(?:trackers?|airtags?|tiles?|find my|smarttags?)\b", main))
    implies_bluetooth = trackers or bool(re.search(
        rf"\b(?:{'|'.join(VENDORS)}|phones?|watches|headphones|earbuds|beacons?|wearables?|devices?)\b", main))
    whole_duration = re.search(rf"\b(?:in|over|during|for) the next (\d+(?:\.\d+)?)\s*({UNIT_S})", lower)
    if whole_duration and not re.search(rf"\b\d+(?:\.\d+)?\s*{UNIT_S}\b", main):
        main = f"{main} for {_seconds(whole_duration.group(1), whole_duration.group(2))} seconds"
    probe = main if re.search(r"\b(bluetooth|ble|btle|zigbee|802\.15\.4|thread|tpms|tire|tyre|classic|beacons?|airtags?)\b", main) \
        else main if re.search(r"\bwi-?fi\b", main) else "find bluetooth devices " + main  # Bluetooth by default
    try:
        task = parse(re.sub(rf"\b\d+(?:\.\d+)?\s*{UNIT_S}\s+of\s+(?:iq|samples)", "", probe))
    except IntentError as exc:
        raise ValueError(str(exc)) from exc
    collect = [task.profile.name, *task.companions]
    if trackers and task.profile.name == "ble":
        collect = ["ble"]  # trackers are BLE advertisers
    filters = _conditions(main)
    notes = []
    if any(c.field == "rssi_dbfs" for c in filters + [w for t in triggers for w in t.when]):
        notes.append("Signal strength is relative (dBFS at the receiver), not calibrated dBm.")
    if len(triggers) < len(trigger_clauses):
        notes.append("Some 'if/when' clauses had no action I recognise and were left out.")
    return WorkflowSpec(goal=raw, collect=collect, duration_s=task.duration_s, device_id=task.device_id,
                        filters=filters, triggers=triggers, notes=notes)


PLANNER_INSTRUCTIONS = """\
You turn an RF operator's request into a WorkflowSpec for RF Sentinel, a RECEIVE-ONLY RF monitoring system.

- collect: protocols to receive. "ble", "classic" (Bluetooth Classic / BR/EDR), "zigbee", or "tpms". A plain
  "bluetooth" request means ["ble", "classic"]. Trackers, AirTags, Tiles and beacons are BLE.
- duration_s: 5 to 600. Default 60 if not stated.
- filters: conditions a device must meet to be shown (all must hold).
- triggers: "when <conditions> do <actions>". Conditions use the same fields. repeat=false fires once per device.
Condition fields: protocol, label (best identity, e.g. "AirTag", a local name or vendor), name, manufacturer,
device_type (e.g. Phone, Watch), device_class ("tracker" or "beacon"), address, rssi_dbfs (relative, e.g. -60),
count (detections so far), dwell_s (seconds between first and last sighting: use for "following me" /
"around for N minutes"), new (true when never seen in earlier runs).
Operators: eq, ne, contains, not_contains (text); ge, le, eq, ne (numbers).
Actions: alert(message, severity info|warning|critical), tag(label), record_iq(seconds <= 10),
start_scan(protocol, seconds <= 300), extend(seconds), stop.
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
        return spec

    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", text.lower()):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    return agent.run_sync(text, usage_limits=UsageLimits(request_limit=4)).output


# --------------------------------------------------------------------------------------------- execution
def device_class(record: dict[str, Any]) -> str:
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


def known_devices_path(base: Path | None = None) -> Path:
    return (base or runs_dir()) / "known_devices.json"


class WorkflowEngine:
    """Applies a spec's filters and triggers to the agent's live detections and carries out actions."""

    def __init__(self, spec: WorkflowSpec, agent, step: Callable[[str], None],
                 on_event: Callable[[dict[str, Any]], None] | None = None, evidence_dir: Path | None = None,
                 base_dir: Path | None = None):
        self.spec, self.agent, self.step = spec, agent, step
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
        self.evidence: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []
        self._started = time.time()
        try:
            self.known = set(json.loads(known_devices_path(self.base_dir).read_text()))
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
        if not key:
            return
        now = time.time()
        with self._lock:
            record = self.devices.get(key)
            if record is None:
                manufacturer = event.get("manufacturer")
                record = self.devices[key] = {
                    "key": key, "protocol": event.get("agent_profile", ""), "first_seen": now, "count": 0,
                    "label": event.get("identity") or event.get("name") or (f"LAP {event['lap']}" if event.get("lap") else key),
                    "name": event.get("name") or "", "address": event.get("address") or event.get("lap") or "",
                    "manufacturer": (manufacturer or {}).get("company_name", "") if isinstance(manufacturer, dict) else str(manufacturer or ""),
                    "device_type": event.get("device_type_detail") or event.get("device_type") or "",
                    "new": key not in self.known, "tags": [], "best_rssi_dbfs": None}
                record["device_class"] = device_class(record)
            record["count"] += 1
            record["last_seen"] = now
            record["dwell_s"] = round(now - record["first_seen"], 1)
            rssi = event.get("rssi_dbfs")
            if isinstance(rssi, (int, float)):
                record["rssi_dbfs"] = float(rssi)
                best = record["best_rssi_dbfs"]
                record["best_rssi_dbfs"] = float(rssi) if best is None else max(best, float(rssi))
            for field in ("channel", "center_freq_hz"):
                if event.get(field) is not None:
                    record[field] = event[field]
            in_scope = all(_matches(c, record) for c in self.spec.filters)
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
                snapshot = self._public(record)
                self._emit("trigger", trigger=index, rule=trigger.describe(), device=snapshot)
                thread = threading.Thread(target=self._run_actions, args=(index, trigger, snapshot),
                                          name=f"workflow-trigger-{index}", daemon=True)
                self._threads.append(thread)
                thread.start()

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {k: record.get(k) for k in ("key", "protocol", "label", "manufacturer", "device_type", "device_class",
                                           "rssi_dbfs", "best_rssi_dbfs", "count", "dwell_s", "new", "tags", "channel")}

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
        message = action.message or f"{device['label']} ({device['protocol']}) matched"
        alert = {"t": round(time.time() - self._started, 1), "severity": action.severity, "message": message,
                 "device": device}
        with self._lock:
            self.alerts.append(alert)
        self._emit("alert", severity=action.severity, message=message, device=device)
        self.step(f"ALERT ({action.severity}): {message} - {device['label']}, {device.get('rssi_dbfs')} dBFS")
        return message

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

    def _do_record_iq(self, action: Action, device: dict[str, Any]) -> str:
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
        data = capture_iq(self.agent.radio, receiver, int(freq), rate, gain, action.seconds)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.agent.gateway.run_id}-{re.sub(r'[^A-Za-z0-9]+', '', device['key'])[:24]}-{int(time.time())}"
        path = self.evidence_dir / f"{stem}.cs8"
        path.write_bytes(data)
        meta = {"format": "cs8", "device_id": receiver["id"], "center_freq_hz": int(freq), "sample_rate_sps": rate,
                "gain_db": gain, "seconds": len(data) / 2 / rate, "sha256": hashlib.sha256(data).hexdigest(),
                "trigger_device": device, "run_id": self.agent.gateway.run_id, "recorded_at": time.time()}
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
                "tpms": "tire sensors"}[action.protocol]
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
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            matched = [self._public(self.devices[k]) for k in self.matched]
            try:
                path = known_devices_path(self.base_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(sorted(self.known | set(self.devices))))
            except OSError:
                pass
            return {"spec": self.spec.model_dump(), "summary": self.spec.summary(),
                    "matched": sorted(matched, key=lambda d: -(d["count"] or 0)),
                    "seen": len(self.devices), "alerts": self.alerts, "timeline": self.timeline,
                    "triggers_fired": sum(1 for e in self.timeline if e["kind"] == "trigger"),
                    "evidence": list(self.evidence),
                    "follow_up_scans": [{k: v for k, v in f.items() if k != "result_devices"} for f in self.follow_ups]}


def run_workflow(agent, spec: WorkflowSpec, on_step: Callable[[str], None] = print,
                 on_detection: Callable[[dict[str, Any]], None] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 gain_db: float | None = None, planner: dict[str, Any] | None = None,
                 base_dir: Path | None = None) -> dict[str, Any]:
    """Run a validated spec: deterministic collection by the agent, filters and triggers by the engine."""
    engine = WorkflowEngine(spec, agent, on_step, on_event, base_dir=base_dir)
    on_step(f"Workflow: {spec.summary()['find']}"
            + (f"; keep {' and '.join(spec.summary()['filters'])}" if spec.filters else "")
            + (f"; {len(spec.triggers)} trigger(s)" if spec.triggers else ""))

    def observe(event: dict[str, Any]) -> None:
        if on_detection:
            on_detection(event)
        engine.observe(event)

    result = agent.run(spec.command(), on_step=on_step, on_detection=observe, gain_db=gain_db)
    result["workflow"] = engine.finish()
    result["workflow"]["planner"] = planner or {"planner": "deterministic"}
    for follow in engine.follow_ups:
        result["devices"] = result["devices"] + follow["result_devices"]
    tags = {k: v["tags"] for k, v in engine.devices.items() if v["tags"]}
    for device in result["devices"]:
        if device["key"] in tags:
            device["tags"] = tags[device["key"]]
    return result
