"""Refine a plan in plain words: "also alert me if a 2G cell shows up", "make it 5 minutes", "drop the signal
filter", "then survey cell towers for 1 minute", "repeat every 10 minutes for an hour".

The rules path edits the current plan directly (or re-plans the original request with the change appended when
the change adds steps or a schedule). The optional model path gets the current plan and the change, and returns
the whole updated plan. Either way the result is validated like any plan, and the change list says what moved.
"""
from __future__ import annotations

import re
from typing import Any

from .playbook import (PLAYBOOK_INSTRUCTIONS, PlaybookSpec, SEQUENCER, Step, _what_phrase, plan_playbook_deterministic,
                       strip_repeat)
from .workflow import (CELL_FIELDS, PLANNER_INSTRUCTIONS, UNIT_S, WorkflowSpec, _short_reason, default_planner_model,
                       plan_deterministic)

REMOVE = r"\b(?:remove|drop|delete|forget|lose|get rid of|without|no longer|don'?t|do not|stop)\b"
SYNONYMS = {"strength": "signal", "rssi": "signal", "stronger": "signal", "weaker": "signal", "airtags": "airtag",
            "trackers": "tracker", "vendor": "manufacturer", "apple": "apple", "2g": "2g", "new": "new",
            "unknown": "unknown", "iq": "iq", "recording": "record", "record": "record", "alerts": "alert",
            "tags": "tag", "tagging": "tag", "classic": "classic", "serving": "serving", "lingering": "seen for",
            "dwell": "seen for", "filters": "filter", "rules": "rule"}


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Plain-language differences between two plan summaries."""
    def lines(summary: dict[str, Any]) -> list[str]:
        if "steps" in summary:
            out = [f"repeat: {summary['repeat']}"] if summary.get("repeat") else []
            for i, step in enumerate(summary["steps"]):
                out += [f"step {i + 1}: find {step['find']}"] + [f"step {i + 1}: keep {f}" for f in step["filters"]] \
                    + [f"step {i + 1}: {t}" for t in step["triggers"]]
                out += [f"step {i + 1}: {step[k]}" for k in ("gate", "focus") if step.get(k)]
            return out
        return [f"find {summary['find']}"] + [f"keep {f}" for f in summary["filters"]] + list(summary["triggers"])
    old, new = lines(before), lines(after)
    if "steps" in after and "steps" not in before:
        old = [f"step 1: {x}" for x in old]  # a workflow that became a playbook is its step 1
    return [f"+ {x}" for x in new if x not in old] + [f"− {x}" for x in old if x not in new]


def _target_step(book: PlaybookSpec, instruction: str, conditions_fields: set[str]) -> int:
    m = re.search(r"\bstep (\d)\b", instruction)
    if m and 1 <= int(m.group(1)) <= len(book.steps):
        return int(m.group(1)) - 1
    wants_cell = bool(conditions_fields & CELL_FIELDS) or bool(re.search(r"\b(?:cell|cells|cellular|towers?|lte|5g|2g|carriers?|networks?|operators?|plmn)\b", instruction))
    for index in range(len(book.steps) - 1, -1, -1):
        if ("cellular" in book.steps[index].workflow.collect) == wants_cell:
            return index
    return len(book.steps) - 1


def _edit_workflow(spec: WorkflowSpec, instruction: str) -> WorkflowSpec:
    """Apply one plain-language change to a single workflow."""
    text = instruction.lower().strip(" .")
    updated = spec.model_copy(deep=True)
    if re.search(REMOVE, text):
        words = {SYNONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", re.sub(REMOVE, " ", text)) if len(w) > 1}
        words -= {"the", "a", "an", "me", "it", "them", "about", "any", "rule", "filter", "and", "of", "on", "for",
                  "alerting", "anymore", "please", "that", "this", "when", "if"}
        wants_filter = bool(re.search(r"\bfilters?\b|\bonly\b", text))
        wants_rule = bool(re.search(r"\brules?\b|\btriggers?\b|\balert", text))

        def hit(description: str) -> bool:
            low = description.lower()
            return any(w in low for w in words) or (not words and True)
        before = (len(updated.filters), len(updated.triggers))
        if not wants_rule or wants_filter:
            updated.filters = [c for c in updated.filters if not hit(c.describe())]
        if not wants_filter or wants_rule:
            kept = []
            for trigger in updated.triggers:
                if hit(trigger.describe()):
                    # "don't record IQ": drop just that action when the rule has others
                    actions = [a for a in trigger.do if not hit(a.describe())]
                    if actions and len(actions) < len(trigger.do) and not any(hit(c.describe()) for c in trigger.when):
                        kept.append(trigger.model_copy(update={"do": actions}))
                    continue
                kept.append(trigger)
            updated.triggers = kept
        if (len(updated.filters), len(updated.triggers)) == before and updated.triggers == spec.triggers:
            raise ValueError(f"nothing in the plan matches '{instruction}'")
        return WorkflowSpec.model_validate(updated.model_dump())
    # Additions and changes: plan the instruction against this workflow's own protocols, then merge.
    change = re.sub(r"^(?:and|also|plus|then|please)[, ]+", "", text.strip())
    duration = re.search(rf"\b(?:make it|for|to|change (?:it|the duration) to|run for)\s+(\d+(?:\.\d+)?\s*{UNIT_S})\b", change)
    # A sentence boundary keeps the change's own action ("alert me if X") apart from the collection clause.
    probe = plan_deterministic(f"{_what_phrase(spec.collect)} for {duration.group(1) if duration else '30 seconds'}. {change}",
                               standing_actions=False)
    if duration or re.fullmatch(rf"(?:make (?:it |this )?)?\d+(?:\.\d+)?\s*{UNIT_S}(?: long)?", change):
        updated.duration_s = probe.duration_s if duration else plan_deterministic(f"{_what_phrase(spec.collect)} for {change}").duration_s
    if probe.channel and "zigbee" in spec.collect:
        updated.channel = probe.channel
    if re.search(r"\b(?:only|just|keep|filter)\b", text) or not probe.triggers:
        for condition in probe.filters:
            if condition not in updated.filters:
                updated.filters.append(condition)
    for trigger in probe.triggers:
        if trigger not in updated.triggers:
            updated.triggers.append(trigger)
    if updated == spec:
        raise ValueError(f"I could not tell what to change from '{instruction}'")
    return WorkflowSpec.model_validate(updated.model_dump())


def refine_rules(current: dict[str, Any], instruction: str, original: str = "") -> dict[str, Any]:
    """Rules path. `current` is a plan result ({"spec"} or {"playbook"}); returns a new plan result."""
    text = " ".join(str(instruction).split())
    if not text:
        raise ValueError("Say what to change, e.g. 'also alert me if a 2G cell shows up'")
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", text.lower()):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    adds_steps = re.match(rf"(?:and\s+)?(?:{SEQUENCER})\b", text.lower()) or strip_repeat(text)[1] is not None
    if adds_steps:
        # New steps or a schedule are added to the plan as it stands now (earlier refinements are kept). The new
        # steps are planned in the context of the current ones, so "watch them" / "if you find any" refer back.
        steps = ([Step.model_validate(x) for x in current["playbook"]["steps"]] if current.get("playbook")
                 else [Step(workflow=WorkflowSpec.model_validate(current["spec"]))])
        base_goal = (current.get("playbook") or current.get("spec") or {}).get("goal") or original
        rest, repeat, note = strip_repeat(text)
        repeat = repeat or (PlaybookSpec.model_validate(current["playbook"]).repeat if current.get("playbook") else None)
        if rest.strip(" .,;"):
            context = ". then ".join(f"{_what_phrase(s.workflow.collect)} for {s.workflow.duration_s} seconds" for s in steps)
            probe = plan_playbook_deterministic(f"{context}. {rest}")
            steps += probe.steps[len(steps):]
        book = PlaybookSpec.model_validate({"goal": f"{base_goal.rstrip(' .')}. {text}", "steps": [s.model_dump() for s in steps],
                                            "repeat": repeat.model_dump() if repeat else None,
                                            "notes": [note] if note else []})
        return {"playbook": book.model_dump(), "summary": book.summary(), "goal": book.goal}
    if current.get("playbook"):
        book = PlaybookSpec.model_validate(current["playbook"])
        probe_fields = set()
        try:
            probe = plan_deterministic("find ble devices for 30 seconds. " + re.sub(r"^(?:and|also|plus|please)[, ]+", "", text.lower()))
            probe_fields = {c.field for c in probe.filters} | {c.field for t in probe.triggers for c in t.when}
        except ValueError:
            pass
        index = _target_step(book, text.lower(), probe_fields)
        book.steps[index].workflow = _edit_workflow(book.steps[index].workflow, " ".join(re.sub(r"\b(?:(?:in|for|on|of) )?step \d\b", "", text).split()))
        book = PlaybookSpec.model_validate(book.model_dump())
        return {"playbook": book.model_dump(), "summary": book.summary(), "goal": book.goal}
    spec = _edit_workflow(WorkflowSpec.model_validate(current["spec"]), text)
    return {"spec": spec.model_dump(), "summary": spec.summary(), "goal": spec.goal}


REFINE_INSTRUCTIONS = """\
You are given an RF Sentinel plan (JSON) and an operator's change. Return the WHOLE updated plan in the same
shape. Change only what the operator asked for; keep every other value exactly as it is. If the change adds a step
or a schedule, return a playbook. The rules for plans follow.

"""


def refine(current: dict[str, Any], instruction: str, original: str = "", planner: str = "auto", model: Any = None) -> dict[str, Any]:
    """Refine with the model when available (falls back to the rules path with the reason)."""
    import json
    before = current.get("summary") or (PlaybookSpec.model_validate(current["playbook"]).summary() if current.get("playbook")
                                        else WorkflowSpec.model_validate(current["spec"]).summary())
    if re.search(r"\b(transmit|jam|spoof|replay|attack|inject|deauth|page)\b", str(instruction).lower()):
        raise ValueError("RF Sentinel only receives. It will not transmit, jam, spoof, replay, or page devices.")
    reason = None
    try:
        rules_out = refine_rules(current, instruction, original)
    except ValueError as exc:
        if re.search(r"at most|between 5 and|repeats every|cycles|ends within|shorter than one cycle|24 h|only receives", str(exc)):
            raise  # over a limit: refuse with the reason rather than let a model bend the change to fit
        rules_out = None
    model = model or (default_planner_model() if planner in ("auto", "model") else None)
    out = None
    if planner != "deterministic" and model is not None:
        try:
            import os
            os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
            from pydantic_ai import Agent, UsageLimits
            playbook = bool(current.get("playbook")) or bool(re.match(rf"(?:and\s+)?(?:{SEQUENCER})\b", instruction.lower())) \
                or strip_repeat(instruction)[1] is not None
            output = PlaybookSpec if playbook else WorkflowSpec
            agent = Agent(model, output_type=output, retries=2,
                          instructions=REFINE_INSTRUCTIONS + (PLAYBOOK_INSTRUCTIONS if playbook else PLANNER_INSTRUCTIONS))
            plan_json = current.get("playbook") or current.get("spec")
            if playbook and not current.get("playbook"):
                plan_json = {"goal": plan_json.get("goal", ""), "steps": [{"workflow": plan_json}]}
            spec = agent.run_sync(f"Current plan:\n{json.dumps(plan_json)}\n\nChange: {instruction}",
                                  usage_limits=UsageLimits(request_limit=4)).output
            out = ({"playbook": spec.model_dump(), "summary": spec.summary()} if playbook
                   else {"spec": spec.model_dump(), "summary": spec.summary()})
            out.update(planner="model", model=model if isinstance(model, str) else type(model).__name__, fallback_reason=None)
        except Exception as exc:
            reason = _short_reason(exc)
    elif planner == "model":
        reason = "no planner model configured (set RF_SENTINEL_PLANNER_MODEL)"
    if out is None:
        out = rules_out if rules_out is not None else refine_rules(current, instruction, original)
        out.update(planner="deterministic", model=None, fallback_reason=reason)
    from .explain import explain
    out["explain"] = explain(out)
    out["changes"] = _changes(before, out["summary"])
    out["refined"] = True
    if not out["changes"]:
        raise ValueError(f"That change left the plan as it was: '{instruction}'")
    return out
