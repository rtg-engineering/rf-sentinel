"""Persistent, append-only environment memory for the Analyst (roadmap Phase 3).

One SQLite file holds:
  snapshots    every captured, truth-stripped snapshot (content-hashed IDs)
  baselines    named baselines with numbered versions; a new version supersedes, never overwrites
  assessments  every investigation, with the snapshots it used and its full record
  annotations  operator reviews and labels, which never modify evidence or findings

Triggers reject UPDATE and DELETE on every table, so history is auditable. Any stored assessment
can be recomputed from its stored snapshots and checked against the recorded result (verify_assessment).
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .evidence import EvidenceStore, Snapshot, canonical, short_hash

TABLES = ("snapshots", "baselines", "assessments", "annotations", "missions", "mission_events")
ANNOTATION_KINDS = ("confirm", "dispute", "note", "known_device", "unknown_device")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT UNIQUE NOT NULL, label TEXT NOT NULL, captured_at TEXT NOT NULL,
  stored_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baselines (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  baseline_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), supersedes TEXT,
  author TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(name, version));
CREATE TABLE IF NOT EXISTS assessments (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  investigation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  reference_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  current_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  baseline_id TEXT, grounding_passed INTEGER NOT NULL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  annotation_id TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, author TEXT NOT NULL,
  kind TEXT NOT NULL, entity_key TEXT, investigation_id TEXT, finding_id TEXT, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS missions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mission_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id), at REAL NOT NULL, recorded_at TEXT NOT NULL,
  type TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL);
""" + "".join(f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
""" for t in TABLES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStoreError(ValueError):
    pass


class EnvironmentMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    # Snapshots -------------------------------------------------------------------------------
    def add_snapshot(self, snapshot: Snapshot) -> str:
        """Store a snapshot once; storing identical content again is a no-op."""
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO snapshots (snapshot_id, label, captured_at, stored_at, payload) "
                       "VALUES (?, ?, ?, ?, ?)", (snapshot.snapshot_id, snapshot.label, snapshot.captured_at, _now(),
                                                  canonical(snapshot.to_dict())))
        return snapshot.snapshot_id

    def snapshot(self, snapshot_id: str) -> Snapshot:
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise MemoryStoreError(f"Unknown snapshot: {snapshot_id}")
        return Snapshot.from_dict(json.loads(row["payload"]))  # re-verifies the content hash

    def snapshots(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("SELECT seq, snapshot_id, label, captured_at, stored_at FROM snapshots ORDER BY seq")]

    # Baselines -------------------------------------------------------------------------------
    def set_baseline(self, snapshot: Snapshot, name: str = "default", author: str = "operator",
                     reason: str = "Baseline set") -> dict[str, Any]:
        """Record a new baseline version; earlier versions remain and are marked superseded by this one."""
        self.add_snapshot(snapshot)
        current = self.active_baseline(name)
        version = (current["version"] + 1) if current else 1
        record = {"baseline_id": f"baseline:{name}:v{version}", "name": name, "version": version,
                  "snapshot_id": snapshot.snapshot_id, "supersedes": current["baseline_id"] if current else None,
                  "author": author, "reason": reason, "created_at": _now()}
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO baselines (baseline_id, name, version, snapshot_id, supersedes, author, reason, created_at) "
                       "VALUES (:baseline_id, :name, :version, :snapshot_id, :supersedes, :author, :reason, :created_at)", record)
        return record

    def active_baseline(self, name: str = "default") -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM baselines WHERE name = ? ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        return dict(row) if row else None

    def baseline_history(self, name: str = "default") -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM baselines WHERE name = ? ORDER BY version", (name,))]

    # Assessments -----------------------------------------------------------------------------
    def record_assessment(self, investigation: dict[str, Any], baseline_id: str | None = None) -> dict[str, Any]:
        scope = investigation["scope"]
        for sid in (scope["reference_snapshot_id"], scope["current_snapshot_id"]):
            self.snapshot(sid)  # both snapshots must already be stored
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO assessments (investigation_id, created_at, reference_snapshot_id, current_snapshot_id, "
                       "baseline_id, grounding_passed, record) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (investigation["investigation_id"], investigation["created_at"], scope["reference_snapshot_id"],
                        scope["current_snapshot_id"], baseline_id, int(investigation["grounding"]["passed"]),
                        canonical(investigation)))
            seq = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"assessment_seq": seq, "investigation_id": investigation["investigation_id"]}

    def assessments(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT seq, investigation_id, created_at, reference_snapshot_id, current_snapshot_id, baseline_id, "
                "grounding_passed FROM assessments ORDER BY seq")]

    def assessment(self, seq: int | None = None) -> dict[str, Any]:
        """A stored investigation record; the latest when seq is None."""
        with closing(self._connect()) as db:
            row = (db.execute("SELECT record FROM assessments ORDER BY seq DESC LIMIT 1").fetchone() if seq is None else
                   db.execute("SELECT record FROM assessments WHERE seq = ?", (seq,)).fetchone())
        if row is None:
            raise MemoryStoreError("No assessment has been recorded" if seq is None else f"Unknown assessment: {seq}")
        return json.loads(row["record"])

    def last_assessed_snapshot(self) -> Snapshot | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT current_snapshot_id FROM assessments ORDER BY seq DESC LIMIT 1").fetchone()
        return self.snapshot(row["current_snapshot_id"]) if row else None

    def verify_assessment(self, seq: int, provider=None) -> dict[str, Any]:
        """Recompute a stored assessment from its stored snapshots and compare the stable result.

        Only deterministic investigations are expected to reproduce exactly; an LLM-authored assessment
        is re-checked for grounding against the same stored evidence instead.
        """
        from .grounding import check
        from .investigate import run_investigation
        stored = self.assessment(seq)
        scope = stored["scope"]
        store = EvidenceStore([self.snapshot(scope["reference_snapshot_id"]), self.snapshot(scope["current_snapshot_id"])])
        if stored["provider"].get("name") == "deterministic" or stored["provider"].get("fallback"):
            again = run_investigation(store, provider, question=scope["question"], provenance=stored["evidence"]["provenance"])
            same = again["investigation_id"] == stored["investigation_id"] and again["findings"] == stored["findings"]
            return {"seq": seq, "method": "recomputed", "reproduced": same,
                    "stored_investigation_id": stored["investigation_id"], "recomputed_investigation_id": again["investigation_id"]}
        # Re-run the read-only tools the record cites, then re-check every citation against stored evidence.
        from .tools import READ_ONLY_TOOLS
        for result in stored["computed_results"].values():
            store.register_computed(result["tool"], result["args"],
                                    READ_ONLY_TOOLS[result["tool"]]["fn"](store, **result["args"]))
        draft = {k: stored[k] for k in ("findings", "recommendations", "limitations")}
        draft["findings"] = [{k: v for k, v in f.items() if k != "grounded"} for f in draft["findings"]]
        recheck = check(draft, store)
        return {"seq": seq, "method": "regrounded", "reproduced": recheck["passed"] == stored["grounding"]["passed"]
                and set(store.computed) >= set(stored["computed_results"]),
                "stored_investigation_id": stored["investigation_id"], "grounding_passed": recheck["passed"]}

    # Annotations -----------------------------------------------------------------------------
    def annotate(self, kind: str, text: str, author: str = "operator", entity_key: str | None = None,
                 investigation_id: str | None = None, finding_id: str | None = None) -> dict[str, Any]:
        if kind not in ANNOTATION_KINDS:
            raise MemoryStoreError(f"kind must be one of {', '.join(ANNOTATION_KINDS)}")
        if not (entity_key or finding_id):
            raise MemoryStoreError("An annotation must target an entity_key or a finding_id")
        if not str(text).strip():
            raise MemoryStoreError("Annotation text is required")
        record = {"created_at": _now(), "author": str(author)[:80], "kind": kind, "entity_key": entity_key,
                  "investigation_id": investigation_id, "finding_id": finding_id, "text": str(text)[:2000]}
        record["annotation_id"] = f"note:{short_hash(record)}"
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO annotations (annotation_id, created_at, author, kind, entity_key, investigation_id, "
                       "finding_id, text) VALUES (:annotation_id, :created_at, :author, :kind, :entity_key, "
                       ":investigation_id, :finding_id, :text)", record)
        return record

    def annotations(self, entity_keys: list[str] | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM annotations ORDER BY seq")]
        if entity_keys is None:
            return rows
        wanted = set(entity_keys)
        return [r for r in rows if r["entity_key"] in wanted]

    # Missions (event-sourced; state is derived by replaying events) --------------------------------
    def add_mission(self, mission_id: str, spec: dict[str, Any], created_by: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO missions (mission_id, created_at, created_by, spec) VALUES (?, ?, ?, ?)",
                       (mission_id, _now(), created_by, canonical(spec)))

    def mission_spec(self, mission_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT spec FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
        if row is None:
            raise MemoryStoreError(f"Unknown mission: {mission_id}")
        return json.loads(row["spec"])

    def missions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("SELECT seq, mission_id, created_at, created_by FROM missions ORDER BY seq")]

    def append_mission_event(self, mission_id: str, at: float, type_: str, actor: str, data: dict[str, Any]) -> int:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO mission_events (mission_id, at, recorded_at, type, actor, data) VALUES (?, ?, ?, ?, ?, ?)",
                       (mission_id, float(at), _now(), type_, actor, canonical(data)))
            return db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def mission_events(self, mission_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT seq, at, recorded_at, type, actor, data FROM mission_events WHERE mission_id = ? "
                              "ORDER BY seq", (mission_id,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # Historical queries ----------------------------------------------------------------------
    def entity_timeline(self, entity_key: str) -> list[dict[str, Any]]:
        """Presence, detection count, signal, and association of one entity in every stored snapshot."""
        timeline = []
        for meta in self.snapshots():
            row = self.snapshot(meta["snapshot_id"]).entities.get(entity_key)
            timeline.append({"snapshot_id": meta["snapshot_id"], "captured_at": meta["captured_at"],
                             "present": row is not None,
                             **({"detections": row.get("detections"), "last_rssi_dbfs": row.get("last_rssi_dbfs"),
                                 "bssid": row.get("bssid")} if row else {})})
        return timeline

    def audit_log(self) -> list[dict[str, Any]]:
        """Every stored record in order of creation, for review."""
        entries = []
        for table, cols in (("snapshots", "snapshot_id AS id, stored_at AS at, label AS detail"),
                            ("baselines", "baseline_id AS id, created_at AS at, reason AS detail"),
                            ("assessments", "investigation_id AS id, created_at AS at, current_snapshot_id AS detail"),
                            ("annotations", "annotation_id AS id, created_at AS at, kind || ': ' || text AS detail")):
            with closing(self._connect()) as db:
                entries += [{"table": table, **dict(r)} for r in db.execute(f"SELECT {cols} FROM {table}")]
        return sorted(entries, key=lambda e: e["at"])


def operator_context(memory: EnvironmentMemory, investigation: dict[str, Any]) -> list[dict[str, Any]]:
    """Annotations about entities a finding cites. Shown beside findings; never changes them."""
    keys = set()
    for finding in investigation.get("findings", []):
        for fact in finding.get("observed_facts", []):
            ref = str(fact.get("ref", ""))
            if ref.startswith("entity:"):
                keys.add(ref[7:ref.rindex("@snapshot:")] if "@snapshot:" in ref else ref[7:])
    return memory.annotations(sorted(keys)) if keys else []


def assess(memory: EnvironmentMemory, current: Snapshot, against: str = "last", baseline_name: str = "default",
           provider=None, provenance: str = "synthetic_replay", question: str | None = None) -> dict[str, Any]:
    """Investigate `current` against the last assessed state ("last") or the active baseline ("baseline"),
    then record the assessment. "last" falls back to the active baseline when nothing has been assessed yet.
    """
    from .investigate import run_investigation
    if against not in ("last", "baseline"):
        raise MemoryStoreError("against must be 'last' or 'baseline'")
    memory.add_snapshot(current)
    baseline = memory.active_baseline(baseline_name)
    reference, reason = None, None
    if against == "last":
        reference, reason = memory.last_assessed_snapshot(), "last assessment"
    if reference is None and baseline:
        reference, reason = memory.snapshot(baseline["snapshot_id"]), f"baseline {baseline['baseline_id']}"
    if reference is None:
        raise MemoryStoreError("Set a baseline first.")
    if reference.snapshot_id == current.snapshot_id:
        raise MemoryStoreError(f"Nothing new has been captured since the {reason}.")
    question = question or (f"What changed since the {reason}, and what evidence supports it?")
    investigation = run_investigation(EvidenceStore([reference, current]), provider, question=question,
                                      provenance=provenance)
    investigation["memory"] = {"compared_against": reason,
                               "baseline_id": baseline["baseline_id"] if baseline else None,
                               "prior_assessments": len(memory.assessments())}
    investigation["operator_context"] = operator_context(memory, investigation)
    recorded = memory.record_assessment(investigation, baseline["baseline_id"] if baseline else None)
    investigation["memory"]["assessment_seq"] = recorded["assessment_seq"]
    return investigation
