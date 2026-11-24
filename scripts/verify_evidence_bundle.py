#!/usr/bin/env python3
"""Independently check an RF Sentinel validation evidence bundle without Docker or the harness.

Checks: every MANIFEST checksum, fixture hash and event count, per-run accounting reconciliation,
repeatability digests recomputed from run files, and the restart-rebuild digest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stable(run):
    return {key: run[key] for key in ("conclusions", "event_accounting", "scenario_checks")}


def reconciles(c):
    return (c["attempted"] == c["accepted"] + c["rejected"] + c["dropped"]
            and sum(c["rejection_reasons"].values()) == c["rejected"])


def verify(root: Path) -> list[tuple[str, bool, str]]:
    checks = []
    manifest = json.loads((root / "MANIFEST.json").read_text())
    for name, expected in manifest["files"].items():
        path = root / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        checks.append((f"checksum {name}", actual == expected, "" if actual == expected else "mismatch or missing"))
    listed = set(manifest["files"]) | {"MANIFEST.json"}
    extra = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and str(p.relative_to(root)) not in listed)
    checks.append(("no unlisted files", not extra, ", ".join(extra)))

    result = json.loads((root / "results.json").read_text())
    fixture = (root / "events.jsonl").read_bytes()
    checks.append(("fixture sha256", hashlib.sha256(fixture).hexdigest() == result["fixture"]["sha256"], ""))
    count = sum(1 for line in fixture.splitlines() if line.strip())
    checks.append(("fixture event count", count == result["fixture"]["event_count"], f"{count} lines"))

    numbers = range(1, result["repeatability"]["run_count"] + 1)
    runs = [json.loads((root / f"run-{n}.json").read_text()) for n in numbers]
    for n, run in zip(numbers, runs):
        checks.append((f"run-{n} accounting reconciles", reconciles(run["event_accounting"]), ""))
    recomputed = [digest(stable(run)) for run in runs]
    checks.append(("run digests match results.json", recomputed == result["repeatability"]["run_digests"], ""))
    checks.append(("all runs identical", len(set(recomputed)) == 1 and recomputed[0] == result["repeatability"]["normalized_result_digest"], ""))

    restart_path = root / "run-restart.json"
    if restart_path.exists():
        restart = json.loads(restart_path.read_text())
        rebuilt = digest(stable(restart))
        checks.append(("restart digest matches results.json", rebuilt == result["restart_recovery"]["result_digest"], ""))
        checks.append(("restart rebuild equals uninterrupted", bool(recomputed) and rebuilt == recomputed[0], ""))
    else:
        checks.append(("restart run present", False, "run-restart.json missing"))
    checks.append(("reported overall status", result["overall_status"] == "pass", result["overall_status"]))
    return checks


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <bundle.tar.gz | extracted directory>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    with tempfile.TemporaryDirectory() as temp:
        if target.is_dir():
            root = target
        else:
            with tarfile.open(target) as tar:
                members = tar.getmembers()
                if any(m.issym() or m.islnk() or Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members):
                    print("FAIL  archive contains unsafe paths", file=sys.stderr)
                    return 1
                tar.extractall(temp, members=members)
            root = Path(temp) / "rf-sentinel-validation"
        try:
            checks = verify(root)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            checks = [("bundle structure", False, f"{type(exc).__name__}: {exc}")]
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name}{'  (' + detail + ')' if detail else ''}")
    ok = all(passed for _, passed, _ in checks)
    print("VERIFIED" if ok else "NOT VERIFIED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
