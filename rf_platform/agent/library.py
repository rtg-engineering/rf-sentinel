"""Named plans: save a workflow or playbook under a name, then "run <name>" from the dashboard or the CLI.

Stored as JSON next to the run reports (<runs dir>/library.json). Every saved plan is validated when saved and
again when used, so a hand-edited file cannot widen bounds.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
from typing import Any

from .playbook import PlaybookSpec
from .workflow import WorkflowSpec, runs_dir

_LOCK = threading.Lock()
MAX_ENTRIES = 100
RUN_VERB = re.compile(r"^\s*(?:please\s+)?(?:run|start|launch|do|execute|kick off)\s+(?:the\s+|my\s+|our\s+)?", re.I)
TRAILING = re.compile(r"\s+(?:playbook|watch|workflow|plan|now|again|please)$")


def _path(base: Path | None) -> Path:
    return (base or runs_dir()) / "library.json"


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower().replace("'s", "s")).strip()


def load(base: Path | None = None) -> list[dict[str, Any]]:
    try:
        entries = json.loads(_path(base).read_text())
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("name") and isinstance(e.get("plan"), dict)]


def _validate(plan: dict[str, Any]) -> dict[str, Any]:
    if isinstance(plan.get("playbook"), dict):
        book = PlaybookSpec.model_validate(plan["playbook"])
        return {"playbook": book.model_dump(), "summary": book.summary()}
    if isinstance(plan.get("spec"), dict):
        spec = WorkflowSpec.model_validate(plan["spec"])
        return {"spec": spec.model_dump(), "summary": spec.summary()}
    raise ValueError("a saved plan needs a spec or a playbook")


def save(name: str, plan: dict[str, Any], text: str = "", base: Path | None = None) -> dict[str, Any]:
    name = " ".join(str(name).split())[:60]
    if not _key(name):
        raise ValueError("give the plan a name")
    entry = {"name": name, "text": str(text)[:2000], "plan": _validate(plan), "saved_at": time.time()}
    with _LOCK:
        entries = [e for e in load(base) if _key(e["name"]) != _key(name)]
        entries.insert(0, entry)
        path = _path(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries[:MAX_ENTRIES], indent=1) + "\n")
    return entry


def delete(name: str, base: Path | None = None) -> bool:
    with _LOCK:
        entries = load(base)
        kept = [e for e in entries if _key(e["name"]) != _key(name)]
        if len(kept) == len(entries):
            return False
        _path(base).write_text(json.dumps(kept, indent=1) + "\n")
        return True


def find(text: str, base: Path | None = None) -> dict[str, Any] | None:
    """The saved plan a request names ("run tonight's watch"), or None. The plan is re-validated."""
    rest = _key(RUN_VERB.sub("", str(text), count=1))
    wanted: set[str] = set()
    for candidate in (rest, _key(text)):
        while candidate:  # "tonight's watch playbook now": try with each trailing filler word removed in turn
            wanted.add(candidate)
            shorter = TRAILING.sub("", candidate)
            if shorter == candidate:
                break
            candidate = shorter
    for entry in load(base):
        if _key(entry["name"]) in wanted:
            return {**entry, "plan": _validate(entry["plan"])}
    return None
