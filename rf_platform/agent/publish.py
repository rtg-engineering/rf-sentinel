"""Make a public copy of a run report: the same run, without identifiers of other people's devices or the site.

  rf-sentinel agent publish <run dir> <out dir>

A live report records what was in the air: BLE and Bluetooth addresses of nearby phones and trackers, advertised
names ("someone's AirPods"), cell IDs and tracking-area codes that can locate the site, and receiver serial numbers.
The public copy:
- replaces every device identifier with a pseudonym (ble-01, cell-03, ...), consistent within the report, so
  recurrence, persistence and alerts still read correctly;
- removes advertised names, cell IDs, tracking-area codes, PCI numbers in labels, and receiver serials;
- keeps everything else: the request, plan, steps, cycles, counts, signal levels, gains, operators (carriers),
  alerts, audit, timings;
- re-renders the HTML report from the redacted data, writes fresh checksums, and records the SHA-256 of the private
  original run.json, so its owner can show the two correspond without publishing the original.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import socket
from typing import Any

MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
SERIAL = re.compile(r"\[[0-9a-f]{6,}\.\.[0-9a-f]{6,}\]|\b[0-9a-f]{32}\b")
PCI_LABEL = re.compile(r" · PCI \d+")
ID_FIELDS = ("key", "address", "lap", "device_key", "agent_key")
DROP_FIELDS = {"name": "", "cell_id": "", "tac": None, "serial": None, "uap": None, "nap": None, "full_mac": None}


def _kind(token: str) -> str:
    if MAC.fullmatch(token) or MAC.search(token):
        return "ble"
    if re.match(r"(?:LTE|5G NR|NR|GSM|2G GSM|UMTS|WCDMA|3G)\b", token, re.I) or "PCI" in token:
        return "cell"
    return "dev"


def _collect(value: Any, tokens: set[str], names: set[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ID_FIELDS and isinstance(v, str) and len(v) >= 4:
                tokens.add(v)
            if k == "name" and isinstance(v, str) and len(v) >= 3:
                names.add(v)
            _collect(v, tokens, names)
    elif isinstance(value, list):
        for v in value:
            _collect(v, tokens, names)
    elif isinstance(value, str):
        tokens.update(MAC.findall(value))


def _drop(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (DROP_FIELDS[k] if k in DROP_FIELDS and v not in (None, "") else _drop(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_drop(v) for v in value]
    return value


def redact(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (public result, redaction summary)."""
    tokens: set[str] = set()
    names: set[str] = set()
    _collect(result, tokens, names)
    # Devices in table order first, so pseudonyms follow the report's own ordering.
    ordered = [d["key"] for d in result.get("devices") or [] if isinstance(d.get("key"), str)]
    ordered += sorted(t for t in tokens if t not in ordered)
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}
    for token in ordered:
        if token in mapping:
            continue
        kind = _kind(token)
        counters[kind] = counters.get(kind, 0) + 1
        mapping[token] = f"{kind}-{counters[kind]:02d}"
    text = json.dumps(_drop(result), default=str, ensure_ascii=False)
    for token, alias in mapping.items():  # labels that tell same-named devices apart end in "(…EE:00)"
        text = text.replace(json.dumps(f"(…{token[-5:]})", ensure_ascii=False)[1:-1], f"({alias})")
    for token in sorted(mapping, key=len, reverse=True):
        text = text.replace(json.dumps(token, ensure_ascii=False)[1:-1], mapping[token])
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(json.dumps(name, ensure_ascii=False)[1:-1], "[name removed]")
    text = MAC.sub("[address removed]", text)
    text = SERIAL.sub("[serial removed]", text)
    text = PCI_LABEL.sub("", text)
    host = socket.gethostname()  # the recording machine's name (often a person's name) appears as the sensor unit
    if len(host) >= 3:
        text = text.replace(host, "sensor-1")
    public = json.loads(text)
    summary = {"pseudonyms": counters, "names_removed": len(names),
               "removed": ["device addresses (pseudonymised)", "advertised names", "cell IDs", "tracking-area codes",
                           "PCI numbers in labels", "receiver serial numbers",
                           "the recording host's name"]}
    return public, summary


def publish(run_dir: Path, out_dir: Path) -> Path:
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    original = (run_dir / "run.json").read_bytes()
    result = json.loads(original)
    public, summary = redact(result)
    public["redaction"] = {**summary, "source_run_json_sha256": hashlib.sha256(original).hexdigest()}
    note = ("Public copy: " + ", ".join(summary["removed"]) + " were removed or replaced with pseudonyms that are "
            "consistent within this report. SHA-256 of the private original run.json: "
            + public["redaction"]["source_run_json_sha256"] + ".")
    public["limitations"] = list(public.get("limitations") or []) + [note]
    if public.get("kind") == "playbook":
        from .playbook import render
        html = render(public)
    else:
        from .report import _render
        html = _render(public)
    folder = out_dir / f"{public['run_id']}-public"
    folder.mkdir(parents=True, exist_ok=True)
    files = {"run.json": json.dumps(public, indent=2, sort_keys=True, default=str) + "\n", "report.html": html}
    sums = []
    for name, text in files.items():
        (folder / name).write_text(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}")
    (folder / "checksums.sha256").write_text("\n".join(sums) + "\n")
    leftovers = MAC.findall(files["run.json"] + html) + SERIAL.findall(files["run.json"] + html)
    if leftovers:  # never write a "public" copy that still holds an identifier
        for name in files:
            (folder / name).unlink()
        raise RuntimeError(f"redaction left {len(leftovers)} identifier(s); nothing was published")
    return folder
