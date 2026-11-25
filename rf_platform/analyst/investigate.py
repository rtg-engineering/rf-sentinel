"""Run one read-only investigation: provider draft → mechanical grounding → investigation record."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from . import SCHEMA_VERSION
from .deterministic import DeterministicAnalyst
from .evidence import EvidenceStore, short_hash
from .grounding import check
from .tools import ToolGateway


_STATUS_ORDER = {"supported": 0, "open": 1, "weakened": 2}


def rank_hypotheses(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order each finding's hypotheses by evidence: supported, then open, then weakened; more support first.

    Rank orders candidate explanations; it never upgrades a hypothesis into a fact.
    """
    ranked = []
    for finding in findings:
        hyps = sorted(finding.get("hypotheses", []), key=lambda h: (
            _STATUS_ORDER.get(h.get("status"), 1), -len(h.get("supporting_refs", [])), len(h.get("weakening_refs", []))))
        ranked.append({**finding, "hypotheses": [{**h, "rank": i} for i, h in enumerate(hyps, 1)]})
    return ranked


class Provider(Protocol):
    name: str
    model: str | None

    def investigate(self, gateway: ToolGateway, scope: dict[str, Any]) -> dict[str, Any]: ...


def run_investigation(store: EvidenceStore, provider: Provider | None = None, question: str | None = None,
                      reference_snapshot_id: str | None = None, current_snapshot_id: str | None = None,
                      provenance: str = "synthetic_replay", max_calls: int = 16,
                      max_seconds: float = 300.0) -> dict[str, Any]:
    provider = provider or DeterministicAnalyst()
    scope = {"reference_snapshot_id": reference_snapshot_id or store.order[0],
             "current_snapshot_id": current_snapshot_id or store.order[-1],
             "question": question or "What changed between the reference and current snapshots, and what evidence supports it?"}
    gateway = ToolGateway(store, max_calls=max_calls, max_seconds=max_seconds)
    provider_info: dict[str, Any] = {"name": provider.name, "model": provider.model}
    try:
        draft = provider.investigate(gateway, scope)
        provider_info.update(getattr(provider, "last_run_info", {}) or {})
    except Exception as exc:  # Graceful degradation: deterministic analysis still runs without a model.
        if isinstance(provider, DeterministicAnalyst):
            raise
        provider_info.update(getattr(provider, "last_run_info", {}) or {})
        provider_info.update(failed=True, error=f"{type(exc).__name__}: {exc}"[:500],
                             fallback="deterministic")
        draft = DeterministicAnalyst().investigate(gateway, scope)
    if isinstance(draft, dict) and isinstance(draft.get("findings"), list):
        draft = {**draft, "findings": rank_hypotheses(draft["findings"])}
    executed_actions: list[Any] = []  # Phase 1 never executes actions; kept explicit for audit.
    grounding = check(draft, store, executed_actions)
    findings = [{**f, "grounded": grounding["findings"].get(f["finding_id"], False)} for f in draft.get("findings", [])]
    stable = {"scope": scope, "findings": findings, "recommendations": draft.get("recommendations", []),
              "provider": {"name": provider_info["name"], "model": provider_info.get("model")}}
    return {
        "schema_version": SCHEMA_VERSION,
        "investigation_id": f"INV-{short_hash(stable)}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "provider": provider_info,
        "evidence": {"provenance": provenance,
                     "snapshots": [store.snapshots[i].collection() for i in store.order]},
        "findings": findings,
        "recommendations": draft.get("recommendations", []),
        "limitations": draft.get("limitations", []),
        "executed_actions": executed_actions,
        "tool_audit": gateway.audit,
        "grounding": grounding,
        "computed_results": dict(store.computed),
    }
