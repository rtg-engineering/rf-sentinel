"""Playbooks: a whole operation from one request, as ordered workflow steps that build on each other.

    "Sweep for bluetooth trackers for 2 minutes. If you find any, watch them for 5 minutes and send an urgent
     alert if one is still there after 3 minutes, and record 5 seconds of IQ. Then survey cell towers for
     1 minute and alert me if a 2G cell shows up."

Each step is an ordinary WorkflowSpec (find / keep / when -> do). A step can also
  - focus on devices an earlier step found, alerted on, or tagged ("watch them", "the flagged ones"), and
  - run only if an earlier step found something ("if you find any trackers, ..."), found nothing
    ("if nothing turns up, ..."), or raised an alert; "otherwise" inverts the previous step's condition.

Steps run one after another on the same gateway (mode, approvals, audit) and stop control, so a playbook is
exactly as receive-only and bounded as its steps: at most MAX_STEPS steps and MAX_PLAYBOOK_S of collection.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .workflow import (PLANNER_INSTRUCTIONS, PROTOCOL_TITLES, Condition, WorkflowSpec, _conditions, _matches, _short_reason,
                       _split_triggers, default_planner_model, narrative, plan_deterministic, run_workflow)

MAX_STEPS = 6
MAX_PLAYBOOK_S = 1800
MIN_REPEAT_S = 60            # shortest interval between the starts of two cycles of a standing watch
MAX_CYCLES = 48
MAX_WATCH_S = 12 * 3600      # a standing watch ends within 12 h


class Gate(BaseModel):
    """When a step runs, judged on an earlier step's outcome."""
    when: Literal["always", "found", "none_found", "alerted", "no_alerts"] = "always"
    step: int | None = None                 # earlier step index (0-based); None = the previous step
    conditions: list[Condition] = Field(default_factory=list)   # for found / none_found: which devices count

    def describe(self, ref: int) -> str:
        what = " and ".join(c.describe() for c in self.conditions) or "anything"
        return {"always": "always",
                "found": f"only if step {ref + 1} found {what if self.conditions else 'something'}",
                "none_found": f"only if step {ref + 1} found {'nothing matching ' + what if self.conditions else 'nothing'}",
                "alerted": f"only if step {ref + 1} raised an alert",
                "no_alerts": f"only if step {ref + 1} raised no alert"}[self.when]


class Step(BaseModel):
    title: str = ""
    workflow: WorkflowSpec
    focus: Literal["all", "found", "alerted", "tagged"] = "all"   # which devices this step pays attention to
    focus_step: int | None = None           # earlier step the focus refers to; None = the previous step
    gate: Gate = Field(default_factory=Gate)


class Repeat(BaseModel):
    """A standing watch: run the whole playbook `times` times, a cycle starting every `every_s` seconds."""
    every_s: int
    times: int = Field(ge=2, le=MAX_CYCLES)

    @model_validator(mode="after")
    def _bounds(self):
        if not MIN_REPEAT_S <= self.every_s <= 6 * 3600:
            raise ValueError(f"a standing watch repeats every {MIN_REPEAT_S} s to 6 h")
        return self

    def describe(self, cycle_s: int = 0) -> str:
        return (f"every {_span(self.every_s)}, {self.times} cycles "
                f"(ends after about {_span(self.every_s * (self.times - 1) + cycle_s)})")


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds // 60} min" if seconds >= 120 and seconds % 60 == 0 else f"{seconds} s"


class PlaybookSpec(BaseModel):
    goal: str = ""
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    repeat: Repeat | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self):
        total = sum(s.workflow.duration_s for s in self.steps)
        if total > MAX_PLAYBOOK_S:
            raise ValueError(f"a playbook collects for at most {MAX_PLAYBOOK_S} s in total (this one asks for {total} s)")
        if self.repeat is not None:
            if self.repeat.every_s < total:
                raise ValueError(f"repeating every {_span(self.repeat.every_s)} is shorter than one cycle ({_span(total)} of collection)")
            window = self.repeat.every_s * (self.repeat.times - 1) + total
            if window > MAX_WATCH_S:
                raise ValueError(f"a standing watch ends within {MAX_WATCH_S // 3600} h (this one would take {_span(window)})")
        for index, step in enumerate(self.steps):
            for name, ref in (("focus_step", step.focus_step), ("gate.step", step.gate.step)):
                if ref is not None and not 0 <= ref < index:
                    raise ValueError(f"step {index + 1}: {name} must name an earlier step")
            if index == 0 and (step.focus != "all" or step.gate.when != "always"):
                raise ValueError("the first step cannot depend on an earlier step")
            if not step.title:
                step.title = _title(step)
        return self

    def ref(self, index: int, value: int | None) -> int:
        return index - 1 if value is None else value

    def summary(self) -> dict[str, Any]:
        steps = []
        for index, step in enumerate(self.steps):
            item = {"title": step.title, **step.workflow.summary()}
            if index:
                gate_ref, focus_ref = self.ref(index, step.gate.step), self.ref(index, step.focus_step)
                item["gate"] = None if step.gate.when == "always" else step.gate.describe(gate_ref)
                item["focus"] = None if step.focus == "all" else (
                    {"found": "devices", "alerted": "devices that raised an alert", "tagged": "tagged devices"}[step.focus]
                    + f" from step {focus_ref + 1}")
            steps.append(item)
        total = sum(s.workflow.duration_s for s in self.steps)
        out = {"steps": steps, "total_s": total}
        if self.repeat is not None:
            out["repeat"] = self.repeat.describe(total)
            out["window_s"] = self.repeat.every_s * (self.repeat.times - 1) + total
        return out


def _title(step: "Step") -> str:
    spec = step.workflow
    what = " + ".join(PROTOCOL_TITLES[p].replace(" (modem)", "") for p in spec.collect)
    if step.focus != "all":
        return {"found": "Follow up on what was found", "alerted": "Follow up on alerts",
                "tagged": "Follow up on tagged devices"}[step.focus] + f" ({what})"
    verb = "Survey" if spec.collect == ["cellular"] else "Watch" if spec.triggers else "Sweep"
    return f"{verb} {what}"


# --------------------------------------------------------------------------------------------- rules planner
SEQUENCER = r"(?:then|after that|afterwards|afterward|next|finally|once (?:that'?s|that is|it'?s|this is) (?:done|finished))"
BREAK = re.compile(rf"((?<=[.;!?])\s+(?:and\s+)?(?:{SEQUENCER}\b[,:]?\s*)?|,?\s+(?:and\s+)?{SEQUENCER}\b[,:]?\s*)", re.I)
NUMBERED = re.compile(r"(?:^|\s+)(?:step\s*)?([1-9])[.):]\s+", re.I)
VERB = (r"(?:(?:do|run) (?:an?|another) (?:quick |short |long |full )?(?:\d+[- ]\w+ )?(?:sweep|scan|survey|pass|check)|"
        r"find|watch|monitor|survey|scan|sweep|look|listen|search|track|focus|follow|observe|check|re-?scan|spend|"
        r"try|dwell|camp|switch to|move to|repeat|keep (?:watching|an eye on|tabs on) (?:them|those|these|the)|"
        r"stay on|go back to)\b")
PROTO = (r"\b(?:bluetooth|ble|btle|zigbee|802\.15\.4|thread|tpms|tire|tyre|classic|beacons?|airtags?|trackers?|tiles?|"
         r"cell|cells|cellular|towers?|lte|5g|4g|2g|gsm|base stations?|carriers?|wi-?fi)\b")
FOCUS_FOUND = (r"\b(?:(?:the|those|these|any) (?:[\w&-]+ ){0,3}?(?:devices?|trackers?|cells?|ones?|phones?|beacons?|airtags?|towers?) (?:that )?(?:you|we|it) (?:found|saw)|them|those|these|it|the same (?:devices?|ones|cells?|trackers?)|(?:the )?ones? (?:you |we )?(?:found|saw)|"
               r"what (?:you|we|was|it) (?:found|saw)|(?:the|those|these) (?:devices?|trackers?|cells?|airtags?|beacons?|phones?)"
               r"(?: (?:you|we|it) (?:found|saw))?)\b")
FOCUS_ALERTED = r"\b(?:the )?(?:flagged|alerted|suspicious) (?:ones?|devices?|trackers?|cells?)\b|\bthe ones? that (?:alerted|triggered)\b"
FOCUS_TAGGED = r"\b(?:the )?tagged (?:ones?|devices?|trackers?|cells?)\b"
GATES = [
    # (pattern, gate kind); "what" names the devices the gate is about
    (r"^if (?:nothing|no (?P<what>.+?)) (?:is |are |was |were )?(?:found|seen|detected|turns? up|turned up|shows? up|showed up)\s*,?\s*(?:then\b\s*)?", "none_found"),
    (r"^if (?:you|we|it) (?:find|found|see|saw) (?:nothing|no (?P<what>[^,]+?))\s*(?:,|$)\s*(?:then\b\s*)?", "none_found"),
    (r"^if (?:no alerts? (?:is |are |was |were )?(?:raised|fired)|nothing (?:alerted|triggered))\s*,?\s*(?:then\b\s*)?(?P<what>)", "no_alerts"),
    (r"^if (?:you|we|it) (?:find|found|see|saw|spot|spotted|detect|detected) (?:any|an?|some|one or more)?\s*(?P<what>[^,]*?)\s*(?:,|$)\s*(?:then\b\s*)?", "found"),
    (r"^if (?:there (?:is|are|was|were) )?(?:any|an?|some) (?P<what>.+?) (?:is |are |was |were )?(?:found|seen|detected|spotted|turns? up|shows? up)\s*,?\s*(?:then\b\s*)?", "found"),
    (r"^if (?:anything|something) (?:is |was )?(?:found|seen|turns? up|turned up|shows? up|showed up)\s*,?\s*(?:then\b\s*)?(?P<what>)", "found"),
    (r"^if (?:any |an? )?alerts? (?:is |are |was |were )?(?:raised|fired|went off|triggered|goes off)\s*,?\s*(?:then\b\s*)?(?P<what>)", "alerted"),
    (r"^if (?:it|anything|something|any rule|a rule) (?:alerted|triggered|alerts|fired|went off)\s*,?\s*(?:then\b\s*)?(?P<what>)", "alerted"),
    (r"^otherwise,?\s*(?P<what>)", "otherwise"),
]
DEBRIEF = r"^(?:and )?(?:send|give|write|make|produce|email|show)(?: me)? (?:a |the |an )?(?:summary|report|debrief|write-?up)\b"
ANY_WHAT = r"^(?:anything|something|any|one|ones|devices?|of them|nothing)?$"


def _strip_gate(text: str) -> tuple[str | None, list[Condition], str]:
    for pattern, kind in GATES:
        m = re.match(pattern, text)
        if m:
            what = (m.groupdict().get("what") or "").strip()
            conditions = [] if re.match(ANY_WHAT, what) else _conditions(what)
            return kind, conditions, text[m.end():].strip()
    return None, [], text


def _is_open_condition(text: str) -> bool:
    """An if/when clause with no action yet ("if an airtag appears"): what follows is its action, not a step."""
    from .workflow import _parse_triggers
    tail = re.split(r"(?:^|[.;]\s*|,\s*)(?=\b(?:if|when|whenever|once|as soon as|each time|every time)\b)", text)[-1]
    return bool(re.match(r"(?:if|when|whenever|once|as soon as|each time|every time)\b", tail)) and not _parse_triggers(tail)


def split_steps(text: str) -> list[str]:
    """Pieces of a request that each start a step (a gate and/or a collection verb). Single-step text -> [text]."""
    raw = " ".join(str(text).split())
    lower = re.sub(r"^(?:first(?:ly)?|start by|begin by|to start)[,:]?\s*", "", raw.lower())
    if re.match(r"(?:step\s*)?1[.):]\s", lower):
        # A numbered list ("1. ... 2. ..."): markers must count up from 1 to be taken as steps.
        expected, out, last = 1, [], 0
        for m in NUMBERED.finditer(lower):
            if int(m.group(1)) == expected:
                out.append(lower[last:m.start()])
                last, expected = m.end(), expected + 1
        out.append(lower[last:])
        lower = ". then ".join(x.strip(" .;,") for x in out if x.strip(" .;,"))
    parts = BREAK.split(lower)
    pieces, seps = [parts[0]], []
    for index in range(1, len(parts), 2):
        seps.append(parts[index])
        pieces.append(parts[index + 1] if index + 1 < len(parts) else "")
    steps: list[str] = [pieces[0]]
    pending_gate: str | None = None
    pending_sep = ""
    for sep, piece in zip(seps, pieces[1:]):
        if not piece.strip():
            continue
        kind, _, rest = _strip_gate(piece.strip())
        starts = bool(re.match(VERB, rest)) and not _is_open_condition(_strip_gate(steps[-1])[2])
        if pending_gate is not None:
            if re.match(VERB, piece.strip()):
                steps.append(f"{pending_gate}, {piece.strip()}")
            else:
                steps[-1] = f"{steps[-1]}{pending_gate}{sep}{piece}"
            pending_gate = None
        elif kind is not None and not rest:
            pending_gate = piece.strip()  # "if you find any trackers" + ", then watch them ..."
            pending_sep = sep
        elif kind is not None and starts:
            steps.append(piece.strip())
        elif kind is None and starts and not re.match(DEBRIEF, piece.strip()):
            steps.append(piece.strip())
        else:
            steps[-1] = f"{steps[-1]}{sep}{piece}"
    if pending_gate is not None:
        steps[-1] = f"{steps[-1]}{pending_sep}{pending_gate}"
    if len(steps) > 1 and re.match(r"(?:if|when|whenever|once|as soon as|each time|every time)\b", steps[0]):
        # "Whenever a phone is seen 5 times, alert me. Find ble devices for 2 minutes": a rule stated before
        # its collection is one step, not two.
        steps[:2] = [f"{steps[1]}. {steps[0]}"]
    return [s.strip(" ,;") for s in steps if s.strip(" ,;.")]


UNIT = r"(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
_AMOUNT = rf"(?:(\d+(?:\.\d+)?)\s*|an?\s+|one\s+)?({UNIT})"
REPEAT_PATTERNS = [
    # "every 10 minutes for the next hour", "every 15 min, 4 times", "every hour until I stop it"
    rf"(?:^|[,.;]?\s*)(?:and\s+)?(?:(?:repeat|do|run)(?:\s+(?:this|that|it|the whole thing))?(?:\s+again)?\s+)?every\s+{_AMOUNT}"
    rf"(?:\s*(?:,\s*)?(?:for\s+(?:the\s+next\s+|another\s+)?{_AMOUNT}|(\d+)\s+times|(?:over|during)\s+the\s+next\s+{_AMOUNT}"
    rf"|until\s+(?:i\s+stop\s+it|stopped|told\s+to\s+stop)))?[,.]?",
    # "repeat 4 times, every 15 minutes" / "repeat this 3 times"
    rf"(?:^|[,.;]?\s*)(?:and\s+)?repeat(?:\s+(?:this|that|it))?\s+(\d+)\s+times(?:\s*,?\s*every\s+{_AMOUNT})?[,.]?",
]


def _seconds_of(amount: str | None, unit: str | None) -> int:
    from .workflow import _seconds
    return _seconds(amount or "1", unit)


def strip_repeat(text: str) -> tuple[str, Repeat | None, str]:
    """Remove a standing-watch phrase. Returns (text without it, Repeat or None, note)."""
    lower = text.lower()
    m = re.search(REPEAT_PATTERNS[1], lower)
    if m:
        times = int(m.group(1))
        if not m.group(3):
            raise ValueError("say how often to repeat, e.g. 'repeat 4 times, every 15 minutes'")
        rest = (text[: m.start()] + " " + text[m.end():]).strip(" ,.;")
        return rest, _repeat(_seconds_of(m.group(2), m.group(3)), times), ""
    m = re.search(REPEAT_PATTERNS[0], lower)
    if m:
        every = _seconds_of(m.group(1), m.group(2))
        window = (_seconds_of(m.group(3), m.group(4)) if m.group(4) else
                  _seconds_of(m.group(6), m.group(7)) if m.group(7) else None)
        times = int(m.group(5)) if m.group(5) else (int(window // every) if window else None)
        note = ""
        if times is None:
            times = min(MAX_CYCLES, max(2, int(MAX_WATCH_S // max(every, 1))))
            note = f"No end was given: the watch runs {times} cycles (about {_span(every * (times - 1))}) unless stopped."
        elif window and window % every:
            note = f"{_span(window)} at one cycle every {_span(every)} is {times} cycles."
        rest = (text[: m.start()] + " " + text[m.end():]).strip(" ,.;")
        return rest, _repeat(every, max(times, 2)), note
    return text, None, ""


def _repeat(every: int, times: int) -> Repeat:
    """Plain-language errors for the operator instead of schema errors."""
    if not MIN_REPEAT_S <= every <= 6 * 3600:
        raise ValueError(f"a standing watch repeats every {MIN_REPEAT_S} s to 6 h (asked: every {_span(every)})")
    if times > MAX_CYCLES:
        raise ValueError(f"a standing watch runs at most {MAX_CYCLES} cycles (asked: {times}); repeat less often or for less time")
    return Repeat(every_s=every, times=times)


def is_multistep(text: str) -> bool:
    """A playbook: several steps, or one step repeated as a standing watch."""
    try:
        rest, repeat, _ = strip_repeat(text)
    except ValueError:
        return True  # let the playbook planner explain what is missing
    return repeat is not None or len(split_steps(rest)) > 1


def _what_phrase(collect: list[str]) -> str:
    sdr = [p for p in collect if p != "cellular"]
    return WorkflowSpec(collect=collect, duration_s=30).command(sdr or collect).split(" for ")[0]


def _head(text: str) -> tuple[str, str]:
    """(collection clause, rule clauses): split before the first if/when."""
    m = re.search(r"(?:[.;,]\s*|\s+)(?=\b(?:if|when|whenever|once|as soon as|each time|every time)\b)", text)
    return (text[: m.start()], text[m.start():]) if m else (text, "")


def _has_rule_action(spec: WorkflowSpec, kinds: set[str]) -> bool:
    return any(a.kind in kinds for t in spec.triggers for a in t.do)


def plan_playbook_deterministic(text: str) -> PlaybookSpec:
    """Plain-language operation -> PlaybookSpec with the rules grammar. Raises ValueError when unsure."""
    raw = " ".join(str(text).split())
    body_text, repeat, repeat_note = strip_repeat(raw)
    pieces = split_steps(body_text)
    steps: list[Step] = []
    notes: list[str] = [repeat_note] if repeat_note else []
    for index, piece in enumerate(pieces):
        kind, conditions, rest = _strip_gate(piece) if index else (None, [], piece)
        gate = Gate()
        if kind == "otherwise":
            previous = steps[-1].gate if steps else Gate()
            inverse = {"found": "none_found", "none_found": "found", "alerted": "no_alerts", "no_alerts": "alerted"}
            if previous.when == "always":
                raise ValueError(f"step {index + 1} says 'otherwise' but step {index} has no condition to invert")
            gate = Gate(when=inverse[previous.when], step=previous.step if previous.step is not None else index - 2,
                        conditions=previous.conditions)
        elif kind is not None:
            gate = Gate(when=kind, conditions=conditions)
            previous = steps[-1].gate if steps else Gate()
            inverse = {"found": "none_found", "none_found": "found"}
            if previous.when == inverse.get(kind):
                # "If you find any airtags, ... If nothing turns up, ...": the second branch is about the same step.
                gate = Gate(when=kind, step=previous.step if previous.step is not None else index - 2,
                            conditions=conditions or previous.conditions)
            ref_step = steps[gate.step if gate.step is not None else index - 1]
            if kind in inverse and not gate.conditions and not ref_step.workflow.filters and any(
                    a.kind == "alert" for t in ref_step.workflow.triggers for a in t.do):
                # "alert me if a 2G cell shows up. If nothing turns up, ...": nothing = no alert.
                gate = Gate(when="alerted" if kind == "found" else "no_alerts", step=gate.step)
        head, rules = _head(rest)
        focus, focus_step = "all", None
        if index:
            focus = ("alerted" if re.search(FOCUS_ALERTED, head) else "tagged" if re.search(FOCUS_TAGGED, head)
                     else "found" if re.search(FOCUS_FOUND, head) and gate.when != "none_found" else "all")
        if focus == "found":
            # "watch them" / "the apple devices you found": keep any description, drop the reference words.
            focus_step = gate.step if gate.when != "always" else None
            head = re.sub(r"\b(?:that )?(?:you|we|it) (?:found|saw)\b", " ", head)
            head = re.sub(r"\b(?:them|those|these|it|the same|ones?)\b", " ", head)
        elif focus in ("alerted", "tagged"):
            head = re.sub(FOCUS_ALERTED if focus == "alerted" else FOCUS_TAGGED, " ", head, count=1)
            # "the flagged ones": the most recent step whose rules could alert / tag.
            wanted = {"alert"} if focus == "alerted" else {"tag"}
            focus_step = next((i for i in range(index - 1, -1, -1) if _has_rule_action(steps[i].workflow, wanted)), index - 1)
        body = " ".join(f"{head} {rules}".split())
        if index and not re.search(PROTO, head):
            # No protocol named: continue with the protocol of the step this one builds on.
            ref = focus_step if focus_step is not None else (gate.step if gate.step is not None else index - 1)
            verbless = re.sub(r"^" + VERB, "", body).strip()
            collect = steps[ref].workflow.collect
            ble_only = {"device_class", "label", "manufacturer", "device_type"}
            if set(collect) == {"ble", "classic"} and any(c.field in ble_only for c in gate.conditions):
                collect = ["ble"]  # trackers, AirTags and vendors come from BLE advertisements
            body = f"{_what_phrase(collect)} {verbless}"
        try:
            spec = plan_deterministic(body, standing_actions=True)
        except ValueError as exc:
            raise ValueError(f"step {index + 1} ({piece[:60]}): {exc}") from exc
        spec.goal = piece
        steps.append(Step(workflow=spec, focus=focus, focus_step=focus_step if focus_step != index - 1 else None, gate=gate))
    if re.search(DEBRIEF.replace("^", r"(?:^|[.,;]\s*|\bthen\s+|\band\s+)"), raw.lower()):
        notes.append("A playbook always ends with a debrief and a report.")
    return PlaybookSpec(goal=raw, steps=steps, repeat=repeat, notes=notes)


# --------------------------------------------------------------------------------------------- model planner
PLAYBOOK_INSTRUCTIONS = """\
You turn an RF operator's multi-step request into a PlaybookSpec for RF Sentinel, a RECEIVE-ONLY RF system.
A playbook is an ordered list of steps (at most 6, at most 1800 s of collection in total). Each step has:
- workflow: a WorkflowSpec (collect, duration_s, filters, triggers) exactly as for a single request (rules below);
  put the step's own wording in workflow.goal.
- focus: "all" (default), "found" (only the devices an earlier step matched: "watch them", "those trackers"),
  "alerted" (devices that raised an alert: "the flagged ones"), "tagged" (devices an earlier step tagged).
- focus_step: index (0-based) of the earlier step the focus refers to; null means the previous step.
- gate: when the step runs. when "always" | "found" (an earlier step found devices; conditions narrow which
  ones count, e.g. [device_class eq tracker] for "if you find any trackers") | "none_found" ("if nothing turns
  up") | "alerted" | "no_alerts"; step: earlier index or null for the previous step. "otherwise" is the inverse
  of the previous step's gate, about the same earlier step.
- title: a short imperative title, e.g. "Sweep BLE", "Watch the trackers", "Survey cell towers".
A step that focuses on earlier devices and names no protocol keeps the earlier step's protocols (BLE only when
the gate is about trackers, AirTags or vendors). Do not repeat the gate's condition as a filter of a focused step:
the focus already holds only those devices.
An action with no condition of its own ("tag them as baseline", "record 3 seconds of IQ") applies to every device
the step keeps: a trigger when [count ge 1].
"If nothing turns up" after a step that has no filters and only alert rules means no alert was raised: gate
"no_alerts" (and "if anything turns up" there means "alerted"). After "if you find any X, ...", a following "if
nothing turns up, ..." is the other branch: same earlier step, same conditions [X], gate "none_found".
Write vendor names capitalised ("Apple"). The first step always has focus "all" and gate "always". Do not add
steps the operator did not ask for, and never shorten what the operator asked for to fit the limits.

Single-step workflow rules:
""" + PLANNER_INSTRUCTIONS


def _plan_playbook_with_model(text: str, model: Any) -> PlaybookSpec:
    import os
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    from pydantic_ai import Agent, UsageLimits
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", text.lower()):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    agent = Agent(model, output_type=PlaybookSpec, instructions=PLAYBOOK_INSTRUCTIONS, retries=2)
    spec = agent.run_sync(text, usage_limits=UsageLimits(request_limit=4)).output
    spec.goal = spec.goal or text
    return spec


def plan_any(text: str, planner: str = "auto", model: Any = None) -> dict[str, Any]:
    """One entry point for the dashboard and CLI: a single workflow, or a playbook when the request has steps.

    Returns the workflow plan (see workflow.plan) or {"playbook", "summary", "planner", "model", "fallback_reason"}.
    """
    from .workflow import plan
    if not is_multistep(text):
        return plan(text, planner, model)
    reason = None
    try:
        rules = plan_playbook_deterministic(text)
    except ValueError as exc:
        if "at most" in str(exc):
            raise  # over the playbook budget: refuse rather than let a model shorten what was asked for
        rules = None
    model = model or (default_planner_model() if planner in ("auto", "model") else None)
    if planner != "deterministic" and model is not None:
        try:
            spec = _plan_playbook_with_model(text, model)
            return {"playbook": spec.model_dump(), "summary": spec.summary(), "planner": "model",
                    "model": model if isinstance(model, str) else type(model).__name__, "fallback_reason": None}
        except Exception as exc:
            if isinstance(exc, ValueError) and "only receives" in str(exc):
                raise
            reason = _short_reason(exc)
    elif planner == "model":
        reason = "no planner model configured (set RF_SENTINEL_PLANNER_MODEL)"
    spec = rules or plan_playbook_deterministic(text)
    return {"playbook": spec.model_dump(), "summary": spec.summary(), "planner": "deterministic", "model": None,
            "fallback_reason": reason}


# --------------------------------------------------------------------------------------------- execution
def _outcome(step_result: dict[str, Any] | None) -> dict[str, Any]:
    wf = (step_result or {}).get("workflow") or {}
    alerted = {(a.get("device") or {}).get("key") for a in wf.get("alerts") or []}
    tagged = {d["key"] for d in wf.get("all_devices") or [] if d.get("tags")}
    return {"matched": wf.get("matched") or [], "alerted": alerted - {None}, "tagged": tagged}


def _gate(spec: PlaybookSpec, index: int, records: list[dict[str, Any]]) -> tuple[bool, str, list[dict[str, Any]]]:
    step = spec.steps[index]
    gate = step.gate
    if gate.when == "always":
        return True, "", []
    ref = spec.ref(index, gate.step)
    if records[ref]["status"] != "done":
        return False, f"step {ref + 1} did not run", []
    outcome = _outcome(records[ref]["result"])
    hits = [d for d in outcome["matched"] if all(_matches(c, d) for c in gate.conditions)]
    what = " and ".join(c.describe() for c in gate.conditions)
    if gate.when == "found":
        return bool(hits), (f"step {ref + 1} found {len(hits)} device(s)" + (f" where {what}" if what else "") if hits
                            else f"step {ref + 1} found no device" + (f" where {what}" if what else "")), hits
    if gate.when == "none_found":
        where = f" where {what}" if what else ""
        return not hits, (f"step {ref + 1} found no device{where}" if not hits else f"step {ref + 1} found {len(hits)} device(s){where}"), []
    alerted = bool(outcome["alerted"])
    if gate.when == "alerted":
        return alerted, f"step {ref + 1} raised {len(outcome['alerted'])} alert(s)" if alerted else f"step {ref + 1} raised no alert", []
    return not alerted, f"step {ref + 1} raised no alert" if not alerted else f"step {ref + 1} raised an alert", []


def _focus(spec: PlaybookSpec, index: int, records: list[dict[str, Any]], gate_hits: list[dict[str, Any]]) -> set[str] | None:
    step = spec.steps[index]
    if step.focus == "all":
        return None
    ref = spec.ref(index, step.focus_step)
    outcome = _outcome(records[ref]["result"])
    if step.focus == "found":
        same = step.gate.when == "found" and spec.ref(index, step.gate.step) == ref and step.gate.conditions
        return {d["key"] for d in (gate_hits if same else outcome["matched"])}
    return set(outcome[step.focus])


def _merge_devices(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        for device in (record.get("result") or {}).get("devices") or []:
            item = merged.setdefault(device["key"], {**device, "count": 0, "steps": [], "cycles": []})
            item["count"] += device.get("count") or 0
            if record["index"] + 1 not in item["steps"]:
                item["steps"].append(record["index"] + 1)
            if record.get("cycle", 0) + 1 not in item["cycles"]:
                item["cycles"].append(record.get("cycle", 0) + 1)
            for field in ("first_seen",):
                if device.get(field) is not None and (item.get(field) is None or device[field] < item[field]):
                    item[field] = device[field]
            if device.get("last_seen") is not None and (item.get("last_seen") is None or device["last_seen"] > item["last_seen"]):
                item["last_seen"] = device["last_seen"]
            if isinstance(device.get("best_rssi_dbfs"), (int, float)):
                item["best_rssi_dbfs"] = max(device["best_rssi_dbfs"], item.get("best_rssi_dbfs", -999))
            tags = list(dict.fromkeys((item.get("tags") or []) + (device.get("tags") or [])))
            if tags:
                item["tags"] = tags
    return sorted(merged.values(), key=lambda d: (-len(d["cycles"]), -len(d["steps"]), -d["count"]))


def recurring(records: list[dict[str, Any]], devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Standing watch: devices decoded in more than one cycle, most cycles first."""
    ran = sorted({r["cycle"] + 1 for r in records if r["status"] == "done"})
    out = []
    for device in devices:
        if len(device.get("cycles") or []) < 2:
            continue
        span = None
        if isinstance(device.get("first_seen"), (int, float)) and isinstance(device.get("last_seen"), (int, float)):
            span = round(device["last_seen"] - device["first_seen"], 1)
        out.append({"key": device["key"], "label": device.get("identity") or device.get("name") or device["key"],
                    "protocol": device.get("protocol"), "cycles": device["cycles"], "of": len(ran), "span_s": span,
                    "tags": device.get("tags") or []})
    return sorted(out, key=lambda d: -len(d["cycles"]))


def persistence(records: list[dict[str, Any]], devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Devices decoded in more than one step: what stayed around while the operation went on."""
    out = []
    for device in devices:
        if len(device["steps"]) < 2:
            continue
        # Steps that could have decoded this device: they ran and collected its protocol.
        ran = sorted({r["index"] + 1 for r in records if r["status"] == "done"
                      and device.get("protocol") in ((r.get("result") or {}).get("workflow") or {}).get("spec", {}).get("collect", [])})
        span = None
        if isinstance(device.get("first_seen"), (int, float)) and isinstance(device.get("last_seen"), (int, float)):
            span = round(device["last_seen"] - device["first_seen"], 1)
        out.append({"key": device["key"], "label": device.get("identity") or device.get("name") or device["key"],
                    "protocol": device.get("protocol"), "steps": device["steps"], "span_s": span,
                    "every_step": device["steps"] == ran, "tags": device.get("tags") or []})
    return out


def step_summary(record: dict[str, Any], sentence: bool = False) -> str:
    """One step's outcome in a few words (stepper) or one sentence (narrative), from its own records."""
    wf = (record.get("result") or {}).get("workflow") or {}
    seen, matched = wf.get("seen", 0), len(wf.get("matched") or [])
    filtered = bool((wf.get("spec") or {}).get("filters"))
    alerts, fired = len(wf.get("alerts") or []), wf.get("triggers_fired", 0)
    evidence = len(wf.get("evidence") or [])
    if not sentence:
        parts = [f"{seen} seen"] + ([f"{matched} matched"] if filtered else []) + ([f"{alerts} alert(s)"] if alerts else []) \
            + ([f"{evidence} IQ file(s)"] if evidence else [])
        return " · ".join(parts)
    what = (wf.get("summary") or {}).get("find", "")
    text = f"collected {what}, saw {seen} device(s)" + (f", {matched} matched the filters" if filtered else "")
    rules: dict[str, set[str]] = {}
    for event in wf.get("timeline") or []:
        if event["kind"] == "trigger":
            rules.setdefault(event["rule"].split(" → ")[0], set()).add((event.get("device") or {}).get("label") or "?")
    for rule, labels in rules.items():
        names = sorted(labels)
        if rule == "when any device":
            text += f"; applied the step's actions to {len(names)} device(s)"
        else:
            text += f"; {rule} fired on {', '.join(names[:3])}{'…' if len(names) > 3 else ''}"
    if alerts:
        text += f"; {alerts} alert(s)"
    if evidence:
        text += f"; {evidence} IQ evidence file(s)"
    if not rules and (wf.get("spec") or {}).get("triggers"):
        text += "; no rule conditions were met"
    return text[0].upper() + text[1:] + "."


def playbook_narrative(result: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    prefix = "Simulated playbook. " if result.get("simulated") else ""
    records = result["steps_run"]
    cycles = result.get("cycles")
    done = sum(1 for r in records if r["status"] == "done")
    if cycles:
        lines.append(f"{prefix}Standing watch: {cycles['run']} of {cycles['planned']} cycle(s) ran, one every "
                     f"{_span(cycles['every_s'])}; {len(result['devices'])} distinct device(s) and {len(result['alerts'])} "
                     f"alert(s) in {_span(result['elapsed_s'])}.")
        for cycle in sorted({r["cycle"] for r in records}):
            mine = [r for r in records if r["cycle"] == cycle]
            seen = {d["key"] for r in mine for d in ((r.get("result") or {}).get("workflow") or {}).get("all_devices") or []}
            new = {d["key"] for r in mine for d in ((r.get("result") or {}).get("workflow") or {}).get("all_devices") or [] if d.get("new")}
            alerts = [a for a in result["alerts"] if a.get("cycle") == cycle]
            skipped = [r for r in mine if r["status"] in ("skipped", "not_run")]
            failed = [r for r in mine if r["status"] == "error"]
            if failed and len(failed) == len(mine):
                lines.append(f"Cycle {cycle + 1} failed: {failed[0]['reason'].rstrip('.')}.")
                continue
            lines.append(f"Cycle {cycle + 1}: {len(seen)} device(s), {len(new)} new" + (f", {len(alerts)} alert(s)" if alerts else "")
                         + (f"; {len(skipped)} step(s) skipped" if skipped else "") + (f"; {len(failed)} step(s) failed" if failed else "") + ".")
        for item in (result.get("recurring") or [])[:3]:
            lines.append(f"Recurring: {item['label']} was decoded in {len(item['cycles'])} of {item['of']} cycles"
                         + (f" over {_span(item['span_s'])}" if item.get("span_s") else "") + ".")
        rest = len(result.get("recurring") or []) - 3
        if rest > 0:
            lines.append(f"{rest} more device(s) recurred in more than one cycle (see the report).")
    else:
        lines.append(f"{prefix}{done} of {len(records)} step(s) ran; {len(result['devices'])} distinct device(s) "
                     f"and {len(result['alerts'])} alert(s) in {result['elapsed_s']:.0f} s.")
        for record in records:
            label = f"Step {record['index'] + 1} ({record['title']})"
            if record["status"] == "skipped":
                lines.append(f"{label} skipped: {record['reason'].rstrip('.')}.")
                continue
            if record["status"] in ("not_run", "error"):
                lines.append(f"{label} {'not run' if record['status'] == 'not_run' else 'failed'}: {record['reason'].rstrip('.')}.")
                continue
            gate = f" Ran because {record['reason']}." if record.get("reason") else ""
            focus = f" Focused on {len(record['focus'])} device(s) from earlier." if record.get("focus") is not None else ""
            lines.append(f"{label}:{gate}{focus} {step_summary(record, sentence=True)}")
        persist = result.get("persistence") or []
        alerted = {(a.get("device") or {}).get("key") for a in result["alerts"]}
        # Alerted devices first, then tagged ones: the ones the operator asked about.
        ranked = sorted(persist, key=lambda p: (p["key"] not in alerted, not p.get("tags"), -len(p["steps"])))
        for item in ranked[:3]:
            span = f" over {item['span_s'] / 60:.1f} min" if item.get("span_s") else ""
            lines.append(f"Persistent: {item['label']} was decoded in steps {', '.join(map(str, item['steps']))}{span}"
                         + (f" (every {PROTOCOL_TITLES.get(item['protocol'], '')} step)." if item["every_step"] and len(item["steps"]) > 1 else "."))
        rest = len(persist) - min(3, len(ranked))
        if rest > 0:
            lines.append(f"{rest} more device(s) were decoded in more than one step (see Persistent devices in the report).")
    if result.get("stopped"):
        lines.append("Stopped by the operator.")
    return lines


def _run_cycle(agent, spec: PlaybookSpec, cycle: int, started: float, emit, step_log, say, on_detection, planner,
               base_dir, gain_db) -> list[dict[str, Any]]:
    """One pass through the steps. Gates and focus refer to this cycle's steps only."""
    records: list[dict[str, Any]] = [{"index": i, "cycle": cycle, "title": s.title, "status": "pending", "reason": "",
                                      "result": None, "focus": None} for i, s in enumerate(spec.steps)]
    label = (lambda i: f"Cycle {cycle + 1} step {i + 1}") if spec.repeat else (lambda i: f"Step {i + 1}")
    lock = threading.Lock()

    def note(index: int, status: str, **fields) -> None:
        emit({"t": round(time.time() - started, 1), "kind": "step", "step": index, "cycle": cycle, "status": status,
              "title": spec.steps[index].title, **fields})
    for index, step in enumerate(spec.steps):
        record = records[index]
        if agent._stop.is_set():
            record.update(status="not_run", reason="the playbook was stopped")
            continue
        ok, why, hits = _gate(spec, index, records)
        if not ok:
            record.update(status="skipped", reason=why)
            say(f"{label(index)} skipped: {why}")
            note(index, "skipped", reason=why)
            continue
        focus = _focus(spec, index, records, hits)
        if focus is not None and not focus:
            record.update(status="skipped", reason="no devices from the earlier step to focus on")
            say(f"{label(index)} skipped: no devices from the earlier step to focus on")
            note(index, "skipped", reason=record["reason"])
            continue
        record.update(status="running", reason=why, focus=sorted(focus) if focus is not None else None,
                      started_s=round(time.time() - started, 1))
        note(index, "running", reason=why, focus=len(focus) if focus is not None else None)
        say(f"{label(index)}: {step.title}" + (f" (because {why})" if why else "")
            + (f", focused on {len(focus)} device(s) from step {spec.ref(index, step.focus_step) + 1}" if focus is not None else ""))
        carried: dict[str, list[str]] = {}
        for earlier in records[:index]:
            for d in ((earlier.get("result") or {}).get("workflow") or {}).get("all_devices") or []:
                if d.get("tags"):
                    carried[d["key"]] = list(dict.fromkeys(carried.get(d["key"], []) + d["tags"]))

        def relay(event: dict[str, Any], _index=index) -> None:
            with lock:
                emit({**event, "t": round(time.time() - started, 1), "step": _index, "cycle": cycle})
        try:
            result = run_workflow(agent.child(), step.workflow, on_step=step_log(f"[{label(index)}] "),
                                  on_detection=on_detection, on_event=relay, gain_db=gain_db,
                                  planner=planner, base_dir=base_dir, focus=focus, carried_tags=carried)
            record.update(status="done", result=result, finished_s=round(time.time() - started, 1))
        except Exception as exc:
            from .gateway import PermissionDenied
            if isinstance(exc, PermissionDenied) and agent._stop.is_set():
                record.update(status="not_run", reason="the playbook was stopped")
            else:
                record.update(status="error", reason=f"{type(exc).__name__}: {exc}"[:300])
                say(f"{label(index)} failed: {exc}")
        note(index, record["status"], reason=record["reason"] if record["status"] != "done" else "",
             summary=step_summary(record) if record["status"] == "done" else "")
    return records


def run_playbook(agent, spec: PlaybookSpec, on_step: Callable[[str], None] = print,
                 on_detection: Callable[[dict[str, Any]], None] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 planner: dict[str, Any] | None = None, base_dir: Path | None = None,
                 gain_db: float | None = None) -> dict[str, Any]:
    """Run the steps in order on one gateway and stop control; a standing watch repeats them on a schedule."""
    started = time.time()
    emit = on_event or (lambda e: None)
    log: list[dict[str, Any]] = []

    def step_log(prefix: str):
        def write(message: str) -> None:
            log.append({"t": round(time.time() - started, 1), "message": f"{prefix}{message}"})
            on_step(f"{prefix}{message}")
        return write

    say = step_log("")
    total = spec.summary()["total_s"]
    cycles = spec.repeat.times if spec.repeat else 1
    say(f"Playbook: {len(spec.steps)} step(s), up to {total} s of collection"
        + (f"; standing watch {spec.repeat.describe(total)}" if spec.repeat else ""))
    records: list[dict[str, Any]] = []
    cycles_run = 0
    for cycle in range(cycles):
        if agent._stop.is_set():
            break
        if cycle:
            start_at = started + cycle * spec.repeat.every_s
            wait = max(0.0, start_at - time.time())
            emit({"t": round(time.time() - started, 1), "kind": "cycle", "cycle": cycle, "status": "waiting",
                  "next_in_s": round(wait), "next_at": start_at})
            say(f"Cycle {cycle + 1} of {cycles} starts in {wait:.0f} s")
            if agent._stop.wait(wait):
                break
        if getattr(agent, "simulate", False):
            agent.sim_clock = (time.time(), float(total))  # one scripted scenario across every step of a cycle
        if spec.repeat:
            emit({"t": round(time.time() - started, 1), "kind": "cycle", "cycle": cycle, "status": "running"})
        ran = _run_cycle(agent, spec, cycle, started, emit, step_log, say, on_detection, planner, base_dir, gain_db)
        records += ran
        cycles_run += 1
        if cycle == 0 and cycles > 1 and not any(r["status"] == "done" for r in ran) and any(r["status"] == "error" for r in ran):
            say("The first cycle could not run any step; the standing watch ends here instead of waiting for later cycles")
            break
    if agent._stop.is_set():
        say("Playbook stopped by operator")
    devices = _merge_devices(records)
    alerts, evidence = [], []
    for record in records:
        wf = (record.get("result") or {}).get("workflow") or {}
        alerts += [{**a, "step": record["index"], "cycle": record["cycle"]} for a in wf.get("alerts") or []]
        evidence += [{**x, "step": record["index"], "cycle": record["cycle"]} for x in wf.get("evidence") or []]
    first = next((r["result"] for r in records if r.get("result")), None) or {}
    started_iso = datetime.fromtimestamp(started, timezone.utc).isoformat()
    result = {"kind": "playbook", "command": spec.goal, "playbook": spec.model_dump(), "summary": spec.summary(),
              "planner": planner or {"planner": "deterministic"}, "steps_run": records, "devices": devices,
              "alerts": alerts, "evidence": evidence, "steps": log, "started_at": started_iso,
              "elapsed_s": round(time.time() - started, 1), "mode": agent.gateway.mode, "receive_only": True,
              "simulated": bool(getattr(agent, "simulate", False)), "stopped": agent._stop.is_set(),
              "audit": agent.gateway.audit, "profile": spec.steps[0].workflow.collect[0],
              "receiver": first.get("receiver") or "-", "duration_s": total, "gains_db": [],
              "cycles": {"planned": cycles, "run": cycles_run, "every_s": spec.repeat.every_s} if spec.repeat else None}
    result["persistence"] = persistence(records, devices)
    result["recurring"] = recurring(records, devices) if spec.repeat else []
    result["run_id"] = "RUN-" + hashlib.sha256(f"{started_iso}|playbook|{spec.goal}".encode()).hexdigest()[:12]
    result["narrative"] = playbook_narrative(result)
    from .report import LIMITATIONS
    result["limitations"] = LIMITATIONS + ["Devices are followed between steps by address. BLE devices that rotate "
                                           "their random address (phones, AirTags every ~15 min) can appear as new."]
    return result


# --------------------------------------------------------------------------------------------- report
def render(result: dict[str, Any]) -> str:
    e = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    summary = result.get("summary") or {}
    status_word = {"done": "ran", "skipped": "skipped", "not_run": "not run", "error": "failed", "pending": "pending",
                   "running": "running"}
    cards = []
    cycles = result.get("cycles")
    shown = result["steps_run"] if not cycles else [
        {"index": i, "title": st.get("title"), "status": "pending", "reason": "", "result": None}
        for i, st in enumerate(summary.get("steps") or [])]
    for record in shown:
        index = record["index"]
        item = (summary.get("steps") or [{}] * (index + 1))[index]
        wf = (record.get("result") or {}).get("workflow") or {}
        story = "".join(f"<li>{e(x)}</li>" for x in wf.get("narrative") or [])
        cards.append(
            f"<section class='card step {e(record['status'])}'><div class='head'><span class='n'>{index + 1}</span>"
            f"<strong>{e(record['title'])}</strong>"
            + ("" if cycles else f"<span class='pill'>{e(status_word.get(record['status'], record['status']))}</span>") + "</div>"
            + (f"<p class='muted'>{e(item.get('gate'))}</p>" if item.get("gate") else "")
            + (f"<p class='muted'>Focus: {e(item.get('focus'))}</p>" if item.get("focus") else "")
            + f"<p><strong>Find:</strong> {e(item.get('find'))}<br><strong>Keep:</strong> {e('; '.join(item.get('filters') or []) or 'everything decoded')}</p>"
            + ("<ul>" + "".join(f"<li>{e(t)}</li>" for t in item.get("triggers") or []) + "</ul>" if item.get("triggers") else "")
            + (f"<p class='muted'>{e(record['reason'])}</p>" if record.get("reason") else "")
            + (f"<ul class='story'>{story}</ul>" if story else "") + "</section>")
    where = lambda x: (f"cycle {x.get('cycle', 0) + 1} · " if cycles else "") + f"step {x['step'] + 1}"  # noqa: E731
    cycle_rows = ""
    if cycles:
        for cycle in sorted({r["cycle"] for r in result["steps_run"]}):
            mine = [r for r in result["steps_run"] if r["cycle"] == cycle]
            begun = min((r.get("started_s") for r in mine if r.get("started_s") is not None), default=None)
            cycle_rows += (f"<tr><td>{cycle + 1}</td><td>{e(_span(begun) if begun is not None else '–')}</td>"
                           + "".join(f"<td>{e(status_word.get(r['status'], r['status']))}"
                                     + (f"<div class='muted'>{e(step_summary(r))}</div>" if r['status'] == 'done' else
                                        f"<div class='muted'>{e(r['reason'])}</div>" if r.get('reason') else "") + "</td>" for r in mine)
                           + f"<td>{sum(1 for a in result['alerts'] if a.get('cycle') == cycle)}</td></tr>")
        cycle_rows = ("<h2>Cycles</h2><div class='card'><p class='muted'>" + e((summary.get("repeat") or "").capitalize()) + "</p><table><tr><th>Cycle</th><th>Started</th>"
                      + "".join(f"<th>Step {i + 1}</th>" for i in range(len(summary.get("steps") or []))) + "<th>Alerts</th></tr>"
                      + cycle_rows + "</table></div>")
    recur = "".join(f"<li><strong>{e(r['label'])}</strong> <code>{e(r['key'])}</code> · {len(r['cycles'])} of {r['of']} cycles"
                    + (f" · over {e(_span(r['span_s']))}" if r.get("span_s") else "") + "</li>" for r in result.get("recurring") or [])
    alerts = "".join(f"<li class='alert {e(a['severity'])}'><span class='sev'>{e(a['severity'])}</span> {e(where(a))} · "
                     f"{e(a['message'])}</li>" for a in result["alerts"])
    evidence = "".join(f"<li><code>{e(x['file'])}</code> · {e(where(x))} · {e(x['seconds'])} s · sha256 "
                       f"<code>{e(x['sha256'][:16])}…</code></li>" for x in result["evidence"])
    persist = "".join(f"<li><strong>{e(p['label'])}</strong> <code>{e(p['key'])}</code> · steps {e(', '.join(map(str, p['steps'])))}"
                      + (f" · {p['span_s'] / 60:.1f} min" if p.get("span_s") else "") + "</li>" for p in result["persistence"])
    rows = "".join(f"<tr><td><code>{e(d['key'])}</code></td><td>{e(d.get('identity') or d.get('name') or '')}"
                   f"{''.join(f' <span class=tag>{e(t)}</span>' for t in d.get('tags') or [])}</td>"
                   f"<td>{e(', '.join(map(str, d['steps'])))}</td>"
                   + (f"<td>{e(', '.join(map(str, d.get('cycles') or [])))}</td>" if cycles else "") + f"<td>{d['count']}</td>"
                   f"<td>{e(round(d['best_rssi_dbfs'], 1)) + (' dBm' if d.get('protocol') == 'cellular' else ' dBFS') if isinstance(d.get('best_rssi_dbfs'), (int, float)) else ''}</td></tr>"
                   for d in result["devices"])
    story = "".join(f"<li>{e(x)}</li>" for x in result["narrative"])
    log = "".join(f"<li><span class='muted'>{s['t']:.1f} s</span> {e(s['message'])}</li>" for s in result["steps"])
    stats = ((("Cycles run", f"{cycles['run']}/{cycles['planned']}"),) if cycles else ()) + (
             ("Steps run", f"{sum(1 for r in result['steps_run'] if r['status'] == 'done')}/{len(result['steps_run'])}"),
             ("Devices", len(result["devices"])),
             ("Recurring" if cycles else "Persistent", len(result.get("recurring") or []) if cycles else len(result["persistence"])),
             ("Alerts", len(result["alerts"])), ("Evidence", len(result["evidence"])))
    planner = result.get("planner") or {}
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RF Sentinel Playbook</title><style>
:root{{--bg:#f6f8fa;--ink:#17212b;--muted:#5b6b78;--line:#d8e0e6;--panel:#fff;--accent:#7e3ff2}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1a22;--ink:#e7eef3;--muted:#9fb1bd;--line:#2a3b47;--panel:#16232d;--accent:#a57bff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:24px;margin:4px 0}}.muted{{color:var(--muted)}}table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}code{{font:13px ui-monospace,monospace}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;overflow-x:auto}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:14px 0}}
.stat{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.stat span{{display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}}.stat strong{{font-size:22px}}
.story{{margin:0;padding-left:18px;line-height:1.7}}.lead{{border-color:var(--accent)}}
.step .head{{display:flex;align-items:center;gap:10px}}.step .n{{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;font-weight:700;font-size:13px}}
.step.skipped,.step.not_run{{opacity:.7}}.step.skipped .n,.step.not_run .n{{background:var(--muted)}}.step.error .n{{background:#b83c32}}
.pill{{margin-left:auto;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
ul.alerts{{list-style:none;padding:0}}li.alert{{padding:8px 10px;border-radius:8px;margin:6px 0;border:1px solid var(--line)}}
li.alert.critical{{border-color:#b83c32;background:rgba(184,60,50,.1)}}li.alert.warning{{border-color:#b77b17;background:rgba(183,123,23,.1)}}
.tag{{font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;background:rgba(184,60,50,.12);color:#b83c32}}
.sev{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-right:6px}}</style></head>
<body><main>{"<div class='card' style='border-color:#b77b17'><strong>SIMULATED PLAYBOOK.</strong> No radio was used; receivers and detections came from a scripted scenario.</div>" if result.get("simulated") else ""}
<div class="muted">RF SENTINEL AGENT · RECEIVE-ONLY PLAYBOOK</div><h1>{e(result['command'])}</h1>
<div class="muted">Run {e(result['run_id'])} · {e(result['started_at'])} · {e(result['mode'])} mode · {e(result['elapsed_s'])} s ·
planned by {e(planner.get('planner'))}{(' (' + e(planner.get('model')) + ')') if planner.get('model') else ''}</div>
<div class="stats">{''.join(f"<div class='stat'><span>{label}</span><strong>{e(value)}</strong></div>" for label, value in stats)}</div>
<h2>What happened</h2><div class="card lead"><ul class="story">{story}</ul></div>
{f"<h2>Alerts</h2><ul class='alerts'>{alerts}</ul>" if alerts else ""}
{f"<h2>Recurring across cycles</h2><div class='card'><ul>{recur}</ul></div>" if recur else ""}
{f"<h2>Persistent devices</h2><div class='card'><ul>{persist}</ul></div>" if persist and not cycles else ""}
{cycle_rows}
<h2>Steps</h2>{''.join(cards)}
{f"<h2>Evidence</h2><div class='card'><ul>{evidence}</ul></div>" if evidence else ""}
<h2>Devices</h2><div class="card"><table><tr><th>Device</th><th>Identity</th><th>Steps</th>{"<th>Cycles</th>" if cycles else ""}<th>Seen</th><th>Best signal</th></tr>{rows}</table></div>
<h2>What the agent did</h2><ol>{log}</ol>
<h2>Limitations</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in result['limitations'])}</ul></main></body></html>"""


def save(result: dict[str, Any], directory: Path | None = None) -> Path:
    from .report import runs_dir
    folder = (directory or runs_dir()) / result["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    files = {"run.json": json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", "report.html": render(result)}
    sums = []
    for name, text in files.items():
        (folder / name).write_text(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}")
    (folder / "checksums.sha256").write_text("\n".join(sums) + "\n")
    return folder
