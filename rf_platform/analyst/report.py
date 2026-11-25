"""Human-readable HTML and the investigation evidence bundle, both rendered from the same JSON."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore

_CSS = """
:root{--bg:#f6f8fa;--panel:#fff;--ink:#17212b;--muted:#5b6b78;--line:#d8e0e6;--obs:#0b6e4f;--cmp:#3157a8;
--hyp:#8a5a00;--rec:#7b2d8e;--bad:#b42318;--chip:#eef3f6}
@media (prefers-color-scheme: dark){:root{--bg:#0f1a22;--panel:#16232d;--ink:#e7eef3;--muted:#9fb1bd;--line:#2a3b47;
--obs:#56d4a8;--cmp:#8fb0ff;--hyp:#f0c060;--rec:#d79be6;--bad:#ff8a80;--chip:#1f2f3b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:4px 0 2px}h2{font-size:19px;margin:32px 0 10px}
.muted{color:var(--muted)}.banner{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:14px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
.card h3{margin:0 0 4px;font-size:17px}.tag{display:inline-block;font:600 11px/1 system-ui;letter-spacing:.06em;text-transform:uppercase;
padding:4px 7px;border-radius:5px;margin-right:6px;border:1px solid currentColor}
.obs{color:var(--obs)}.cmp{color:var(--cmp)}.hyp{color:var(--hyp)}.rec{color:var(--rec)}.bad{color:var(--bad)}
ul{margin:6px 0 10px;padding-left:20px}li{margin:3px 0}code{font:12.5px ui-monospace,monospace;background:var(--chip);padding:1px 4px;border-radius:4px;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.wrap{overflow-x:auto}td:nth-child(2) code{white-space:nowrap;overflow-wrap:normal}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _facts(items, css, label):
    if not items:
        return ""
    rows = "".join(f"<li>{_e(f['statement'])}: <strong>{_e(f['value'])}</strong> "
                   f"<span class='muted'>(<code>{_e(f['ref'])}</code> → <code>{_e(f['field'])}</code>)</span></li>" for f in items)
    return f"<div><span class='tag {css}'>{label}</span><ul>{rows}</ul></div>"


def render_html(inv: dict[str, Any]) -> str:
    g = inv["grounding"]
    status = "All findings grounded" if g["passed"] else f"Grounding FAILED: {len(g['issues'])} issue(s)"
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>RF Sentinel Analyst Report</title><style>{_CSS}</style></head><body><main>",
             "<div class='muted'>RF SENTINEL ANALYST · READ-ONLY INVESTIGATION</div>",
             f"<h1>{_e(inv['scope'].get('question') or 'What changed between snapshots?')}</h1>",
             f"<div class='muted'>{_e(inv['investigation_id'])} · provider {_e(inv['provider']['name'])}"
             f"{' / ' + _e(inv['provider']['model']) if inv['provider'].get('model') else ''} · {_e(inv['created_at'])}</div>",
             f"<div class='banner'><strong class='{'obs' if g['passed'] else 'bad'}'>{_e(status)}</strong> · "
             f"{g['citations_checked']} citations checked · {len(inv['executed_actions'])} actions executed · "
             f"{sum(1 for a in inv['tool_audit'] if a['status'] == 'ok')} read-only tool calls</div>",
             "<h2>Limitations</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in inv["limitations"]) + "</ul>",
             "<h2>Findings</h2>"]
    for f in inv["findings"]:
        badge = "" if f.get("grounded") else "<span class='tag bad'>Unsupported</span>"
        hyps = "".join(f"<li><strong>{_e(h['status'])}</strong> · {_e(h['kind'])}: {_e(h['statement'])}</li>" for h in f["hypotheses"])
        parts.append(f"<section class='card' id='{_e(f['finding_id'])}'><h3>{badge}{_e(f['title'])}</h3>"
                     f"<div class='muted'>{_e(f['finding_id'])} · {_e(f['category'])} · confidence {_e(f['confidence'])}</div>"
                     + _facts(f["observed_facts"], "obs", "Observed")
                     + _facts(f["computed_results"], "cmp", "Computed")
                     + (f"<div><span class='tag hyp'>Hypotheses</span><ul>{hyps}</ul></div>" if hyps else "")
                     + f"<p><strong>Assessment:</strong> {_e(f['assessment'])}</p></section>")
    parts.append("<h2>Recommendations</h2>")
    for r in inv["recommendations"]:
        parts.append(f"<section class='card'><span class='tag rec'>{_e(r['permission'].replace('_', ' '))}</span>"
                     f"<span class='tag muted'>{'executed' if r['executed'] else 'proposed, not executed'}</span>"
                     f"<p>{_e(r['action'])}</p><p class='muted'>{_e(r['rationale'])}</p></section>")
    if g["issues"] or g["warnings"]:
        parts.append("<h2>Grounding issues</h2><ul>" + "".join(
            f"<li class='bad'>{_e(i['finding_id'])} {_e(i['where'])}: {_e(i['problem'])}</li>" for i in g["issues"]) + "".join(
            f"<li class='muted'>warning · {_e(w['finding_id'])} {_e(w['where'])}: {_e(w['problem'])}</li>" for w in g["warnings"]) + "</ul>")
    parts.append("<h2>Tool audit</h2><div class='wrap'><table><tr><th>#</th><th>Tool</th><th>Status</th><th>Arguments</th><th>Result</th></tr>" + "".join(
        f"<tr><td>{a['sequence']}</td><td><code>{_e(a['tool'])}</code></td><td>{_e(a['status'])}</td>"
        f"<td><code>{_e(json.dumps(a['arguments'], sort_keys=True))}</code></td><td>{_e(a.get('result_id') or a.get('reason'))}</td></tr>"
        for a in inv["tool_audit"]) + "</table></div>")
    parts.append("<h2>Evidence snapshots</h2><ul>" + "".join(
        f"<li><code>{_e(s['snapshot_id'])}</code> · {s['entity_count']} entities · {s['detections_returned']} detections</li>"
        for s in inv["evidence"]["snapshots"]) + "</ul></main></body></html>")
    return "".join(parts)


def write_bundle(out_dir: Path, inv: dict[str, Any], store: EvidenceStore) -> Path:
    """Write the roadmap's investigation bundle layout with SHA-256 checksums."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    current = store.snapshot(inv["scope"]["current_snapshot_id"])
    computed = inv["computed_results"]
    comparison = next((v for v in computed.values() if v["tool"] == "compare_baselines"), {})
    baseline_summary = next((v for v in computed.values() if v["tool"] == "get_environment_summary"
                             and v["args"]["snapshot_id"] == inv["scope"]["reference_snapshot_id"]), {})

    def dumps(value):
        return json.dumps(value, indent=2, sort_keys=True) + "\n"

    files = {
        "investigation.json": dumps(inv),
        "scenario-or-mission.json": dumps({"scope": inv["scope"], "provenance": inv["evidence"]["provenance"]}),
        "observations.jsonl": "".join(json.dumps({"event_id": eid, **obs}, sort_keys=True) + "\n"
                                      for eid, obs in sorted(current.observations.items())),
        "entities.json": dumps(current.entities),
        "topology-changes.json": dumps(comparison.get("relationship_changes", [])),
        "baseline-summary.json": dumps(baseline_summary),
        "hypotheses.json": dumps({f["finding_id"]: f["hypotheses"] for f in inv["findings"]}),
        "agent-tool-audit.jsonl": "".join(json.dumps(a, sort_keys=True) + "\n" for a in inv["tool_audit"]),
        "approvals.json": dumps({"requested": [r for r in inv["recommendations"] if r["permission"] == "requires_approval"],
                                 "granted": [], "executed_actions": inv["executed_actions"]}),
        "validation-results.json": dumps(inv["grounding"]),
        "findings.json": dumps(inv["findings"]),
        "snapshots.json": dumps([store.snapshots[i].to_dict() for i in store.order]),
        "report.html": render_html(inv),
    }
    sums = []
    for name, text in sorted(files.items()):
        data = text.encode()
        (out_dir / name).write_bytes(data)
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    manifest = {"schema_version": inv["schema_version"], "investigation_id": inv["investigation_id"],
                "provider": inv["provider"], "grounding_passed": inv["grounding"]["passed"],
                "provenance": inv["evidence"]["provenance"], "files": sorted(files)}
    (out_dir / "manifest.json").write_text(dumps(manifest))
    sums.append(f"{hashlib.sha256((out_dir / 'manifest.json').read_bytes()).hexdigest()}  manifest.json")
    (out_dir / "checksums.sha256").write_text("\n".join(sorted(sums, key=lambda l: l.split('  ')[1])) + "\n")
    return out_dir
