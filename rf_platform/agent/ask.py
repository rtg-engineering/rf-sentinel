"""Ask about a finished run: "which trackers came back every cycle?", "why did step 2 not run?", "what was strongest?"

With a model configured, the model answers; it can only read the run through the tools below. Without one (or
when its answer fails the check), common questions are answered by code from the run's own records. Its answer is checked mechanically: every device
it cites must be one the run recorded, and every number in it must appear in what the tools returned. An answer
that fails the check is not shown; the code answer is shown instead, with the reason.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from .workflow import _short_reason, default_planner_model, device_class


def device_table(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every device in a workflow or playbook result, with what the run knows about it."""
    records = result.get("steps_run") if result.get("kind") == "playbook" else [{"index": 0, "cycle": 0, "result": result}]
    alerted = {(a.get("device") or {}).get("key") for a in (result.get("alerts") if result.get("kind") == "playbook"
                                                             else (result.get("workflow") or {}).get("alerts")) or []}
    table: dict[str, dict[str, Any]] = {}
    for record in records or []:
        wf = (record.get("result") or {}).get("workflow") or {}
        for d in wf.get("all_devices") or []:
            item = table.setdefault(d["key"], {"key": d["key"], "label": d.get("label") or d["key"], "protocol": d.get("protocol"),
                                               "kind": d.get("device_class") or device_class(d), "manufacturer": d.get("manufacturer") or "",
                                               "device_type": d.get("device_type") or "", "count": 0, "best_signal": None,
                                               "new": bool(d.get("new")), "tags": [], "steps": [], "cycles": [],
                                               "alerted": d["key"] in alerted})
            item["count"] += d.get("count") or 0
            signal = d.get("best_rssi_dbfs")
            if isinstance(signal, (int, float)) and (item["best_signal"] is None or signal > item["best_signal"]):
                item["best_signal"] = round(float(signal), 1)
            item["tags"] = list(dict.fromkeys(item["tags"] + list(d.get("tags") or [])))
            for field, value in (("steps", record.get("index", 0) + 1), ("cycles", record.get("cycle", 0) + 1)):
                if value not in item[field]:
                    item[field].append(value)
    for item in table.values():
        item["unit"] = "dBm" if item["protocol"] == "cellular" else "dBFS"
    return sorted(table.values(), key=lambda d: -d["count"])


def _line(d: dict[str, Any]) -> str:
    bits = [d["protocol"] or "", f"seen {d['count']}×"]
    if d["best_signal"] is not None:
        bits.append(f"best {d['best_signal']:g} {d['unit']}")
    if len(d["steps"]) > 1:
        bits.append(f"steps {', '.join(map(str, d['steps']))}")
    if len(d["cycles"]) > 1:
        bits.append(f"cycles {', '.join(map(str, d['cycles']))}")
    if d["tags"]:
        bits.append("tagged " + ", ".join(d["tags"]))
    if d["alerted"]:
        bits.append("alerted")
    return f"{d['label']} ({d['key']}): " + ", ".join(b for b in bits if b)


def answer_rules(result: dict[str, Any], question: str) -> dict[str, Any] | None:
    """Code answers for the questions operators ask most. None when the question is not one of them."""
    q = question.lower()
    devices = device_table(result)
    alerts = (result.get("alerts") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("alerts")) or []

    def listing(title: str, items: list[dict[str, Any]], empty: str) -> dict[str, Any]:
        if not items:
            return {"answer": empty, "devices": []}
        shown = items[:8]
        return {"answer": f"{title}\n" + "\n".join(f"• {_line(d)}" for d in shown)
                + (f"\n…and {len(items) - len(shown)} more." if len(items) > len(shown) else ""),
                "devices": [d["key"] for d in shown]}
    if re.search(r"\bwhy\b.*\b(?:skip|not run|didn'?t run|did not run)|\bskipped\b", q):
        skipped = [r for r in result.get("steps_run") or [] if r["status"] in ("skipped", "not_run", "error")]
        return {"answer": "\n".join(f"• Step {r['index'] + 1}" + (f" (cycle {r['cycle'] + 1})" if (result.get('cycles') or {}).get('planned', 1) > 1 else "")
                                    + f" {r['status'].replace('_', ' ')}: {r['reason']}" for r in skipped) or "Every step ran.",
                "devices": []}
    if re.search(r"\b(?:alerts?|alerted|warnings?|critical)\b", q):
        if not alerts:
            return {"answer": "No alerts were raised.", "devices": []}
        return {"answer": f"{len(alerts)} alert(s):\n" + "\n".join(
            f"• {a['severity']}: {a['message']}" + (f" (step {a['step'] + 1})" if "step" in a else "") for a in alerts[:10]),
                "devices": list(dict.fromkeys((a.get("device") or {}).get("key") for a in alerts[:10] if a.get("device")))}
    if re.search(r"\b(?:recurr|came back|keep coming|every cycle|more than one cycle|each cycle)", q):
        items = sorted([d for d in devices if len(d["cycles"]) > 1], key=lambda d: -len(d["cycles"]))
        return listing("Devices decoded in more than one cycle:", items, "No device was decoded in more than one cycle.")
    if re.search(r"\b(?:persist|stayed|still there|more than one step|every step|both steps|followed|following)", q):
        items = sorted([d for d in devices if len(d["steps"]) > 1], key=lambda d: (not d["alerted"], -len(d["steps"])))
        return listing("Devices decoded in more than one step:", items, "No device was decoded in more than one step.")
    if re.search(r"\b(?:strongest|closest|loudest|nearest|best signal)\b", q):
        items = sorted([d for d in devices if d["best_signal"] is not None], key=lambda d: -d["best_signal"])[:5]
        return listing("Strongest signals (SDR in dBFS, cells in dBm; not comparable across the two):", items, "No signal levels were recorded.")
    if re.search(r"\b(?:trackers?|airtags?|tiles?)\b", q):
        items = [d for d in devices if d["kind"] == "tracker" or "airtag" in d["label"].lower()]
        return listing(f"{len(items)} tracker(s):", items, "No trackers were decoded.")
    if re.search(r"\bnew\b|never seen|first time", q):
        items = [d for d in devices if d["new"]]
        return listing(f"{len(items)} device(s) not seen in any earlier run:", items, "Every device had been seen in an earlier run.")
    if re.search(r"\b(?:evidence|iq|recordings?|captures?)\b", q):
        evidence = result.get("evidence") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("evidence")
        if not evidence:
            return {"answer": "No IQ evidence was recorded.", "devices": []}
        return {"answer": "\n".join(f"• {x['file']}: {x['seconds']} s at {round(x['center_freq_hz'] / 1e6)} MHz, sha256 {x['sha256'][:16]}…"
                                    for x in evidence), "devices": list(dict.fromkeys(x["device"] for x in evidence))}
    if re.search(r"\bcells?\b|\btowers?\b|\boperators?\b|\bcarriers?\b", q):
        items = [d for d in devices if d["protocol"] == "cellular"]
        carriers = {d["manufacturer"] for d in items if d["manufacturer"] and d["manufacturer"].lower() in q}
        if carriers:
            items = [d for d in items if d["manufacturer"] in carriers]
        return listing(f"{len(items)} cell(s)" + (f" on {', '.join(sorted(carriers))}" if carriers else "") + ":", items,
                       "No cells were recorded.")
    if re.search(r"\bhow many\b|\bcount\b|\bsummar|\bwhat happened\b|\bwhat did you find\b", q):
        story = result.get("narrative") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("narrative")
        return {"answer": "\n".join(f"• {line}" for line in story or []) or f"{len(devices)} device(s).", "devices": []}
    return None


class Answer(BaseModel):
    answer: str = Field(description="Plain-language answer, using only facts returned by the tools")
    devices: list[str] = Field(default_factory=list, description="Keys of every device the answer mentions")


_NUMBER = re.compile(r"(?<![\w:.\-])-?\d+(?:\.\d+)?(?![\w:])")


def check(answer: Answer, devices: list[dict[str, Any]], seen_text: str) -> str | None:
    """Mechanical grounding: cited devices exist, and every number in the answer was in what the tools returned."""
    keys = {d["key"] for d in devices}
    unknown = [k for k in answer.devices if k not in keys]
    if unknown:
        return f"cites device(s) the run never recorded: {', '.join(unknown[:3])}"
    seen_numbers = {float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", seen_text)}
    for raw in _NUMBER.findall(answer.answer):
        if float(raw) not in seen_numbers and float(raw) not in (0.0, 1.0):
            return f"states {raw}, which is not in the run's records"
    return None


ASK_INSTRUCTIONS = """\
You answer an RF operator's question about one finished RF Sentinel run (receive-only RF monitoring). Use the
tools to read the run; use ONLY facts the tools return. Name devices by label and include their keys in `devices`.
Copy numbers exactly as the tools give them; do not compute new numbers (no sums, averages or percentages).
If the records do not answer the question, say so. Keep it short: a sentence or a few bullet lines.
"""


def answer_model(result: dict[str, Any], question: str, model: Any) -> tuple[Answer, str]:
    import os
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    from pydantic_ai import Agent, UsageLimits
    devices = device_table(result)
    seen: list[str] = []

    def record(value: Any) -> str:
        text = json.dumps(value, default=str)
        seen.append(text)
        return text
    agent = Agent(model, output_type=Answer, instructions=ASK_INSTRUCTIONS, retries=1)

    @agent.tool_plain
    def overview() -> str:
        """The run's request, steps or cycles, totals, and plain-language summary."""
        story = result.get("narrative") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("narrative")
        return record({"request": result.get("command"), "simulated": result.get("simulated"), "mode": result.get("mode"),
                       "elapsed_s": result.get("elapsed_s"), "devices": len(devices), "summary": story,
                       "steps": [{"step": r["index"] + 1, **({"cycle": r.get("cycle", 0) + 1} if result.get("cycles") else {}), "title": r.get("title"),
                                  "status": r["status"], "reason": r.get("reason")} for r in result.get("steps_run") or []]})

    @agent.tool_plain
    def list_devices(protocol: str | None = None, kind: str | None = None, alerted_only: bool = False,
                     min_steps: int = 0, min_cycles: int = 0, sort: str = "count", limit: int = 20) -> str:
        """Devices the run recorded. protocol: ble/classic/zigbee/tpms/cellular; kind: tracker/beacon; sort: count or signal."""
        items = [d for d in devices if (not protocol or d["protocol"] == protocol) and (not kind or d["kind"] == kind)
                 and (not alerted_only or d["alerted"]) and len(d["steps"]) >= min_steps and len(d["cycles"]) >= min_cycles]
        if sort == "signal":
            items = sorted([d for d in items if d["best_signal"] is not None], key=lambda d: -d["best_signal"])
        return record({"total": len(items), "devices": items[:max(1, min(limit, 50))]})

    @agent.tool_plain
    def list_alerts() -> str:
        """Every alert raised, with severity, message, device and step."""
        alerts = result.get("alerts") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("alerts")
        repeating = bool(result.get("cycles"))
        return record([{"severity": a["severity"], "message": a["message"], "device": (a.get("device") or {}).get("key"),
                        "step": a.get("step", 0) + 1, **({"cycle": a.get("cycle", 0) + 1} if repeating else {})}
                       for a in alerts or []])

    out = agent.run_sync(question, usage_limits=UsageLimits(request_limit=6)).output
    return out, "\n".join(seen)


def ask(result: dict[str, Any], question: str, planner: str = "auto", model: Any = None) -> dict[str, Any]:
    question = " ".join(str(question).split())[:500]
    if not question:
        raise ValueError("Ask a question about this run")
    coded = answer_rules(result, question)
    model = model or (default_planner_model() if planner in ("auto", "model") else None)
    reason = None
    # With a model available it answers (its answer is checked); the code answer is the fallback. Code answers cover
    # common questions but cannot follow every qualifier ("which Verizon cell was strongest after 10pm?").
    if model is not None and planner != "deterministic":
        try:
            answer, seen = answer_model(result, question, model)
            problem = check(answer, device_table(result), seen)
            if problem is None:
                return {"answer": answer.answer, "devices": answer.devices, "by": "model",
                        "model": model if isinstance(model, str) else type(model).__name__, "checked": True}
            reason = f"model answer rejected: {problem}"
        except Exception as exc:
            reason = _short_reason(exc)
    if coded is None:
        story = result.get("narrative") if result.get("kind") == "playbook" else (result.get("workflow") or {}).get("narrative")
        coded = {"answer": "I can answer questions about alerts, trackers, new devices, the strongest signals, devices "
                           "seen in more than one step or cycle, cells, evidence, and skipped steps. Summary of this run:\n"
                           + "\n".join(f"• {line}" for line in story or []), "devices": []}
    return {**coded, "by": "rules", "model": None, "fallback_reason": reason}
