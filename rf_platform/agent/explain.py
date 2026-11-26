"""One paragraph saying what a plan will do, written by code from the validated plan (never by a model), so the
operator can confirm intent at a glance before pressing Run."""
from __future__ import annotations

from typing import Any

from .workflow import PROTOCOL_TITLES, Action, Condition, Trigger, WorkflowSpec


def _join(items: list[str], last: str = "and") -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {last} {items[-1]}" if items else ""


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return "1 minute" if seconds == 60 else "1 second" if seconds == 1 else f"{seconds} seconds"


def _condition(c: Condition, cell: bool) -> str:
    unit = "dBm" if cell else "dBFS"
    v = c.value
    if c.field == "device_class":
        return f"{'is' if c.op == 'eq' else 'is not'} a {v}"
    if c.field == "new":
        return "has not been seen before" if (v is True) == (c.op == "eq") else "has been seen before"
    if c.field == "rssi_dbfs":
        return f"is {'stronger' if c.op == 'ge' else 'weaker'} than {v:g} {unit}"
    if c.field == "dwell_s":
        return f"stays around for {_duration(v)} or more"
    if c.field == "count":
        return "shows up" if v == 1 else f"is seen {v:g} times"
    if c.field == "generation":
        return f"is a {v} cell"
    if c.field == "operator":
        return "is on an unknown network" if v == "unknown operator" else f"{'is' if c.op in ('eq', 'contains') else 'is not'} on {v}"
    if c.field == "role":
        return "becomes the serving cell"
    if c.field == "label" and c.op == "contains":
        return f"is {'an' if str(v)[:1].lower() in 'aeiou' else 'a'} {v}"
    word = {"manufacturer": "is made by", "device_type": "is a", "name": "is named"}.get(c.field, f"{c.field} {c.op}")
    return f"{word} {v}" if c.op in ("eq", "contains") else f"is not {v}"


def _action(a: Action) -> str:
    return {"alert": lambda: ("send you an urgent alert" if a.severity == "critical" else "alert you")
                              + (f" (“{a.message}”)" if a.message else ""),
            "tag": lambda: f"tag it “{a.label}”",
            "record_iq": lambda: f"record {_duration(a.seconds)} of IQ as evidence",
            "start_scan": lambda: f"scan {PROTOCOL_TITLES[a.protocol]} for {_duration(a.seconds)}",
            "extend": lambda: f"keep watching {_duration(a.seconds)} longer",
            "stop": lambda: "stop"}[a.kind]()


def _plural(text: str) -> str:
    for one, many in (("is ", "are "), ("has ", "have "), ("stays ", "stay "), ("becomes ", "become "), ("shows ", "show ")):
        if text.startswith(one):
            return many + text[len(one):]
    return text


def _rule(t: Trigger, cell: bool) -> str:
    conditions = [c for c in t.when if not (c.field == "count" and c.value == 1 and len(t.when) > 1)]
    anything = len(conditions) == 1 and conditions[0].field == "count" and conditions[0].value == 1
    noun = "cell" if cell else "device"
    if anything:
        passive = all(a.kind in ("tag", "record_iq", "alert") for a in t.do) and not any(a.kind == "alert" for a in t.do)
        subject = f"for every {noun} it keeps" if passive else f"{'every time' if t.repeat else 'as soon as'} anything shows up"
    else:
        subject = f"{'whenever' if t.repeat else 'if'} a {noun} {_join([_condition(c, cell) for c in conditions])}"
    return f"{subject}, I'll {_join([_action(a) for a in t.do])}"


def explain_workflow(spec: WorkflowSpec, lead: str = "I'll") -> str:
    cell = spec.collect == ["cellular"]
    what = {"ble": "Bluetooth LE", "classic": "Bluetooth Classic", "zigbee": "Zigbee", "tpms": "tire sensors",
            "cellular": "cell towers (with the modem)"}
    kinds = [c for c in spec.filters if c.field in ("device_class", "label", "manufacturer", "device_type")]
    noun = ("trackers" if any(c.field == "device_class" and c.value == "tracker" and c.op == "eq" for c in kinds) else "")
    sentence = f"{lead} listen to {_join([what[p] for p in spec.collect])} for {_duration(spec.duration_s)}"
    if spec.channel:
        sentence += f" on channel {spec.channel}"
    keep = [c for c in spec.filters if not (noun and c.field == "device_class")]
    if noun or keep:
        sentence += (f", keeping only {noun or ('cells' if cell else 'devices')}"
                     + (f" that {_join([_plural(_condition(c, cell)) for c in keep])}" if keep else ""))
    sentence += "."
    rules = [_rule(t, cell) for t in spec.triggers]
    if rules:
        sentence += " " + " ".join(r[0].upper() + r[1:] + "." for r in rules)
    return sentence


def explain(plan: dict[str, Any]) -> str:
    """plan: {"spec": ...} or {"playbook": ...} (validated dumps)."""
    if plan.get("spec"):
        return explain_workflow(WorkflowSpec.model_validate(plan["spec"]))
    from .playbook import PlaybookSpec, _span
    book = PlaybookSpec.model_validate(plan["playbook"])
    parts: list[str] = []
    if book.start is not None:
        parts.append(("Starting " + book.start.describe().removeprefix("starts ").replace(", the next time that comes round", "")) + ",")
    if book.repeat is not None:
        total = sum(s.workflow.duration_s for s in book.steps)
        parts.append(f"{'e' if parts else 'E'}very {_span(book.repeat.every_s)} for {book.repeat.times} cycles "
                     f"(about {_span(book.repeat.every_s * (book.repeat.times - 1) + total)}):")
    for index, step in enumerate(book.steps):
        lead = "First I'll" if index == 0 and len(book.steps) > 1 else "I'll" if index == 0 else "Then I'll"
        if step.gate.when != "always":
            ref = book.ref(index, step.gate.step) + 1
            what = _join([_condition(c, False) for c in step.gate.conditions]) if step.gate.conditions else ""
            cond = {"found": f"If step {ref} found " + (f"a device that {what}" if what else "anything"),
                    "none_found": f"If step {ref} found " + (f"no device that {what}" if what else "nothing"),
                    "alerted": f"If step {ref} raised an alert", "no_alerts": f"If step {ref} raised no alert"}[step.gate.when]
            lead = f"{cond}, I'll"
        text = explain_workflow(step.workflow, lead)
        if step.focus != "all":
            ref = book.ref(index, step.focus_step) + 1
            whose = {"found": f"the devices step {ref} found", "alerted": f"the devices that alerted in step {ref}",
                     "tagged": f"the devices tagged in step {ref}"}[step.focus]
            text = text.replace(" listen to ", f" follow {whose} on ", 1)
        if index == 0 and parts and parts[-1].endswith(":") and text.startswith("First"):
            text = "f" + text[1:]
        parts.append(text)
    return " ".join(parts)
