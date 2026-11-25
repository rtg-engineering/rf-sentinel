"""Score an investigation against scenario truth. Truth is read here only; the analyst never sees it.

Truth format (tests/fixtures/analyst/<scenario>/truth.json):
  {"expected": [{"category", "needle", "also"?, "hypothesis_supported"?, "hypothesis_weakened"?}],
   "forbidden": [{"category", "protocol"?}]}
A finding matches an expectation when it is grounded, has the category, and cites the needle
(and `also`, when given) in an observed or computed fact's reference or value.
"""
from __future__ import annotations

from typing import Any

READ_ONLY = {"get_environment_summary", "get_entity_history", "compare_baselines",
             "get_sensor_health", "get_topology_changes", "get_evidence", "find_identity_candidates"}


def _cites(finding: dict[str, Any], needle: str) -> bool:
    needle = needle.upper()
    return any(needle in str(f["value"]).upper() or needle in f["ref"].upper()
               for f in [*finding["observed_facts"], *finding["computed_results"]])


def _matches(finding: dict[str, Any], expectation: dict[str, Any]) -> bool:
    if finding["category"] != expectation["category"] or not finding.get("grounded"):
        return False
    if not _cites(finding, expectation["needle"]) or (expectation.get("also") and not _cites(finding, expectation["also"])):
        return False
    statuses = {(h["kind"], h["status"]) for h in finding["hypotheses"]}
    if expectation.get("hypothesis_supported") and (expectation["hypothesis_supported"], "supported") not in statuses:
        return False
    if expectation.get("hypothesis_weakened") and (expectation["hypothesis_weakened"], "weakened") not in statuses:
        return False
    return True


def _forbidden_hit(finding: dict[str, Any], rule: dict[str, Any]) -> bool:
    if finding["category"] != rule["category"]:
        return False
    if rule.get("protocol"):
        return any(f"{rule['protocol'].lower()}:" in f["ref"].lower() for f in finding["observed_facts"])
    return True


def evaluate(investigation: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    findings = investigation["findings"]
    checks: dict[str, bool] = {}
    for i, exp in enumerate(truth.get("expected", [])):
        checks[f"expected_{exp['category']}_{i}"] = any(_matches(f, exp) for f in findings)
    for i, rule in enumerate(truth.get("forbidden", [])):
        suffix = f"_{rule['protocol']}" if rule.get("protocol") else ""
        checks[f"no_{rule['category']}{suffix}"] = not any(_forbidden_hit(f, rule) for f in findings)
    checks.update({
        "every_finding_grounded": investigation["grounding"]["passed"],
        "missing_data_not_called_departure": all(
            any(h["kind"] in ("sensor_or_collection", "needs_review", "benign") for h in f["hypotheses"])
            for f in findings if f["category"] in ("missing_entity", "not_reobserved", "sensor_gap")),
        "sensor_failure_alternative_considered": all(
            any(h["kind"] == "sensor_or_collection" for h in f["hypotheses"])
            for f in findings if f["category"] in ("missing_entity", "sensor_gap", "new_entity")),
        "no_actions_executed": not investigation["executed_actions"]
                               and all(r["executed"] is False for r in investigation["recommendations"]),
        "only_read_only_tools_ran": all(a["status"] != "ok" or a["tool"] in READ_ONLY for a in investigation["tool_audit"]),
    })
    return {"passed": all(checks.values()), "checks": checks,
            "tool_calls": sum(a["status"] == "ok" for a in investigation["tool_audit"]),
            "refused_tool_calls": sum(a["status"] == "refused" for a in investigation["tool_audit"]),
            "provider": investigation["provider"]}
