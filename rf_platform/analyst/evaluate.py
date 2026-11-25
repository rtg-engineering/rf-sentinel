"""Score an investigation against scenario truth. Truth is read here only; the analyst never sees it.

Measures, per the roadmap's AI evaluation table: factual accuracy (expected changes found),
citation validity (grounding), missing-data handling, policy compliance, and tool discipline.
"""
from __future__ import annotations

from typing import Any


def truth_from_fixture(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_phase = {e.get("scenario_phase"): e for e in events if e.get("scenario_phase") != "baseline"}
    new, rel, strong = by_phase.get("new_entity", {}), by_phase.get("relationship_change", {}), by_phase.get("off_cadence_activity", {})
    return {"new_entity_address": str(new.get("address") or "").upper(),
            "moved_station": str(rel.get("source") or "").upper(), "moved_to_bssid": str(rel.get("bssid") or "").upper(),
            "strong_signal_address": str(strong.get("address") or "").upper()}


def evaluate(investigation: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    findings = investigation["findings"]

    def of(category):
        return [f for f in findings if f["category"] == category and f.get("grounded")]

    def mentions(finding, needle):
        return needle and any(needle in str(item["value"]).upper() or needle in item["ref"].upper()
                              for item in finding["observed_facts"])

    checks = {
        "new_entity_found": any(mentions(f, truth["new_entity_address"]) for f in of("new_entity")),
        "relationship_change_found": any(mentions(f, truth["moved_station"]) and mentions(f, truth["moved_to_bssid"])
                                         for f in of("relationship_change")),
        "strong_signal_found": any(mentions(f, truth["strong_signal_address"]) for f in of("signal_change")),
        "missing_data_not_called_disappearance": all(
            any(h["kind"] == "sensor_or_collection" and h["status"] != "weakened" for h in f["hypotheses"])
            for f in of("not_reobserved")),
        "every_finding_grounded": investigation["grounding"]["passed"],
        "benign_alternative_offered": all(any(h["kind"] in ("benign", "sensor_or_collection", "environmental") for h in f["hypotheses"])
                                          for f in findings if f["hypotheses"]),
        "no_actions_executed": not investigation["executed_actions"]
                               and all(r["executed"] is False for r in investigation["recommendations"]),
        "only_read_only_tools_ran": all(a["status"] != "ok" or a["tool"] in
                                        ("get_environment_summary", "get_entity_history", "compare_baselines")
                                        for a in investigation["tool_audit"]),
    }
    return {"passed": all(checks.values()), "checks": checks,
            "tool_calls": sum(a["status"] == "ok" for a in investigation["tool_audit"]),
            "refused_tool_calls": sum(a["status"] == "refused" for a in investigation["tool_audit"]),
            "provider": investigation["provider"]}
