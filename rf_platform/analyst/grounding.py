"""Mechanical grounding checks. Non-LLM code decides whether a finding is supported by evidence."""
from __future__ import annotations

import math
import re
from typing import Any

from .evidence import MISSING, EvidenceStore, field_value
from .schemas import DRAFT, validate
from .tools import RESTRICTED_TOOLS

OBSERVATION_PREFIXES = ("snapshot:", "entity:", "event:", "sensor:")
# Standalone numbers only: digits inside MACs, IDs, or names (72:94:..., FM 101.7) are not claims.
_NUMBER = re.compile(r"(?<![\w:.\-])-?\d+(?:\.\d+)?(?![\w:])")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def _numbers(text: str) -> list[str]:
    return re.findall(r"-?\d+(?:\.\d+)?", text)


FLAGGED = (("new_entities", "new_entity"), ("relationship_changes", "relationship_change"),
           ("signal_changes", "signal_change"))


def uncovered_changes(draft: dict[str, Any], store: EvidenceStore) -> list[dict[str, str]]:
    """Changes flagged by any compare_baselines result that no finding of the matching category cites."""
    missing = []
    for result in store.computed.values():
        if result.get("tool") != "compare_baselines":
            continue
        for list_name, category in FLAGGED:
            for item in result.get(list_name, []):
                key = str(item.get("key"))
                covered = any(
                    f.get("category") == category and any(
                        key in str(fact.get("ref")) or str(fact.get("value")) == key
                        for fact in [*f.get("observed_facts", []), *f.get("computed_results", [])])
                    for f in draft.get("findings", []))
                if not covered:
                    missing.append({"category": category, "key": key})
    return missing


def check(draft: dict[str, Any], store: EvidenceStore, executed_actions: list[Any] | None = None) -> dict[str, Any]:
    """Return a grounding report. A finding is grounded only if every fact and reference verifies."""
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    schema_errors = validate(draft, DRAFT)
    if schema_errors:
        return {"passed": False, "schema_errors": schema_errors, "issues": [], "warnings": [],
                "findings": {}, "citations_checked": 0}

    def issue(fid, where, problem):
        issues.append({"finding_id": fid, "where": where, "problem": problem})

    per_finding: dict[str, bool] = {}
    citations = 0
    seen_ids = set()
    for f_index, finding in enumerate(draft["findings"]):
        fid = finding["finding_id"]
        before = len(issues)
        if fid in seen_ids:
            issue(fid, "finding_id", "duplicate finding_id")
        seen_ids.add(fid)
        values = []
        for group, allowed in (("observed_facts", OBSERVATION_PREFIXES), ("computed_results", ("computed:",))):
            for i, item in enumerate(finding[group]):
                where = f"{group}[{i}]"
                citations += 1
                if not item["ref"].startswith(allowed):
                    issue(fid, where, f"{group} must cite {' or '.join(allowed)} references, got {item['ref']!r}")
                    continue
                record = store.resolve(item["ref"])
                if record is None:
                    issue(fid, where, f"reference does not exist: {item['ref']}")
                    continue
                actual = field_value(record, item["field"])
                if actual is MISSING:
                    issue(fid, where, f"field {item['field']!r} not present in {item['ref']}")
                elif not _same(actual, item["value"]):
                    issue(fid, where, f"stated value {item['value']!r} does not match evidence value {actual!r}")
                values.append(str(item["value"]))
        for h in finding["hypotheses"]:
            for side in ("supporting_refs", "weakening_refs"):
                for ref in h[side]:
                    citations += 1
                    if store.resolve(ref) is None:
                        issue(fid, f"hypotheses.{h['hypothesis_id']}.{side}", f"reference does not exist: {ref}")
        # Prose numbers that appear in no cited value are flagged for review (not failed: units/labels vary).
        cited = " ".join(values)
        for text_field in ("title", "assessment"):
            for number in _NUMBER.findall(finding[text_field]):
                if not any(_same(float(number), float(v)) for v in _numbers(cited)):
                    warnings.append({"finding_id": fid, "where": text_field,
                                     "problem": f"number {number} in prose is not among this finding's cited values"})
        per_finding[fid] = len(issues) == before

    for rec in draft["recommendations"]:
        tool = rec.get("tool")
        if tool in RESTRICTED_TOOLS and rec["permission"] != "requires_approval":
            issues.append({"finding_id": "-", "where": f"recommendations.{rec['recommendation_id']}",
                           "problem": f"'{tool}' is restricted and must be marked requires_approval"})
    for gap in uncovered_changes(draft, store):
        issues.append({"finding_id": "-", "where": "coverage",
                       "problem": f"flagged {gap['category']} for {gap['key']} has no {gap['category']} finding citing it"})
    if executed_actions:
        issues.append({"finding_id": "-", "where": "executed_actions",
                       "problem": f"{len(executed_actions)} action(s) executed; Phase 1 permits none"})
    return {"passed": not issues, "schema_errors": [], "issues": issues, "warnings": warnings,
            "findings": per_finding, "citations_checked": citations}
