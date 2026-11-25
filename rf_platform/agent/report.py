"""Verifiable agent run reports: run.json + report.html + checksums.sha256 per run."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
from typing import Any

LIMITATIONS = [
    "Receive-only: no radio transmitted during this run.",
    "Detections are what the receiver plugin decoded in this window, at the recorded gains. "
    "Devices that did not transmit, were out of range, or were not decoded are not listed.",
    "Peak levels and clipping are measured from received samples. Front-end overload is inferred from them, not measured directly.",
]


def runs_dir() -> Path:
    return Path(os.getenv("RF_SENTINEL_AGENT_RUNS_DIR") or ".demo/agent-runs")


def _render(result: dict[str, Any]) -> str:
    e = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    rows = "".join(
        f"<tr><td><code>{e(d['key'])}</code></td><td>{d['count']}</td>"
        f"<td>{e(round(d['best_rssi_dbfs'], 1)) if 'best_rssi_dbfs' in d else ''}</td>"
        f"<td>{e(' · '.join(dict.fromkeys(str(d[k]) for k in ('identity', 'name', 'manufacturer', 'device_type_detail', 'pdu_type') if d.get(k))))}</td>"
        f"<td>{e(d.get('gain_db'))}</td></tr>" for d in result["devices"])
    steps = "".join(f"<li><span class='t'>{s['t']:.1f} s</span> {e(s['message'])}</li>" for s in result["steps"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RF Sentinel Agent Run</title><style>
:root{{--bg:#f6f8fa;--ink:#17212b;--muted:#5b6b78;--line:#d8e0e6;--panel:#fff}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1a22;--ink:#e7eef3;--muted:#9fb1bd;--line:#2a3b47;--panel:#16232d}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:24px;margin:4px 0}}.muted,.t{{color:var(--muted)}}table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}code{{font:13px ui-monospace,monospace}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;overflow-x:auto}}</style></head>
<body><main><div class="muted">RF SENTINEL AGENT · RECEIVE-ONLY RUN</div><h1>{e(result['command'])}</h1>
<div class="muted">Run {e(result['run_id'])} · {e(result['started_at'])} · receiver {e(result['receiver'])} · {e(result['duration_s'])} s ·
gain {e(', '.join(f'{g:.0f}' for g in result['gains_db']))} dB ({e(result['adjustments'])} automatic adjustment(s))</div>
<div class="card"><strong>{len(result['devices'])} device(s), {sum(d['count'] for d in result['devices'])} detection(s)</strong>
<table><tr><th>Device</th><th>Seen</th><th>Best RSSI (dBFS)</th><th>Details</th><th>Gain (dB)</th></tr>{rows}</table></div>
<h2>What the agent did</h2><ol>{steps}</ol>
<h2>Limitations</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in result['limitations'])}</ul></main></body></html>"""


def save(result: dict[str, Any], directory: Path | None = None) -> Path:
    """Write the run's JSON, HTML, and checksums; returns the run folder."""
    folder = (directory or runs_dir()) / result["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    files = {"run.json": json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", "report.html": _render(result)}
    sums = []
    for name, text in files.items():
        (folder / name).write_text(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}")
    (folder / "checksums.sha256").write_text("\n".join(sums) + "\n")
    return folder


def finalize(result: dict[str, Any], started: float) -> dict[str, Any]:
    result.setdefault("started_at", datetime.fromtimestamp(started, timezone.utc).isoformat())
    result["run_id"] = "RUN-" + hashlib.sha256(
        f"{result['started_at']}|{result['command']}|{result['receiver']}".encode()).hexdigest()[:12]
    result["limitations"] = LIMITATIONS
    return result


def load(run_id: str, directory: Path | None = None) -> dict[str, Any]:
    path = (directory or runs_dir()) / run_id / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"No run {run_id} under {directory or runs_dir()}")
    return json.loads(path.read_text())


def list_runs(directory: Path | None = None) -> list[dict[str, Any]]:
    base = directory or runs_dir()
    runs = []
    for path in sorted(base.glob("RUN-*/run.json")):
        data = json.loads(path.read_text())
        runs.append({k: data.get(k) for k in ("run_id", "started_at", "command", "receiver", "gains_db")}
                    | {"devices": len(data.get("devices", []))})
    return sorted(runs, key=lambda r: r["started_at"] or "")
