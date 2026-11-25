# RF Sentinel Analyst (read-only investigation)

The Analyst answers *"what changed in this wireless environment, and what evidence supports it?"* It works from captured `/api/status` snapshots. It cannot change ingestion, detections, receivers, or scan schedules. Every factual claim in a report is re-read from the cited evidence by code, not by a model.

This covers Phases 1–3 of the autonomous-investigation roadmap:
- read-only, evidence-backed reports;
- bounded multi-step analysis with sensor-health reasoning;
- ranked hypotheses, evaluation on held-out scenarios, and dashboard drill-down;
- persistent, append-only environment memory: versioned baselines, "what changed since the last assessment", reproducible history, and operator notes that never rewrite evidence.

It does not run missions, keep persistent memory across sessions, or execute any collection action.

## In the dashboard

Click **Investigate**, then **Set baseline** to snapshot what the dashboard currently shows. Let activity accumulate, then click **Investigate changes**. The overlay lists grounded findings, each with its leading explanation and the alternatives. Each cited entity is a chip: click it to open that entity's row in the detections table. **Full report** opens the HTML report.

The dashboard runs only the deterministic analyst, never a model. The investigation reads the same `/api/status` payload the dashboard uses and does not change ingestion state. The endpoints are `POST /api/analyst/baseline`, `POST /api/analyst/investigate`, `GET /api/analyst/latest` and `GET /api/analyst/report`.

`python3 scripts/record_walkthrough.py --analyst --build` records this flow as `docs/media/rf-sentinel-analyst-walkthrough.{mp4,gif}`.

## Run it

End to end through the Docker demo backend (no hardware, no model):

```bash
python3 scripts/analyst_demo.py --build
```

This replays the validation scenario's 25 events (21 baseline events, then a new BLE beacon, a WiFi station moving to another access point, and a strong-signal observation). It pauses after the baseline, captures snapshots over HTTP, investigates, and writes `.demo/analyst/demo-<UTC>/`:

```text
bundle/                  investigation evidence bundle (below)
evaluation.json          score against scenario truth
events.jsonl             fixture, including truth fields the analyst never receives
```

Against any running backend:

```bash
python3 -m rf_platform.analyst capture --base-url http://127.0.0.1:8080 --label baseline --out before.json
# ...later...
python3 -m rf_platform.analyst capture --base-url http://127.0.0.1:8080 --label current --out after.json
python3 -m rf_platform.analyst investigate before.json after.json --out .demo/analyst/run
```

The command exits nonzero if grounding fails. Use `--provenance live` or `controlled_test` for non-synthetic evidence.

## Environment memory

Baselines, assessments, and operator notes are kept in one append-only SQLite file. The dashboard uses `RF_SENTINEL_ANALYST_DB`, which defaults to `<log dir>/analyst-memory.sqlite`. The CLI defaults to `.demo/analyst/memory.sqlite`.

```bash
python3 -m rf_platform.analyst memory baseline day1.json --reason "Initial survey"
python3 -m rf_platform.analyst memory assess day2.json                    # against the baseline (first time)
python3 -m rf_platform.analyst memory assess day3.json --out run/        # against the last assessment
python3 -m rf_platform.analyst memory assess day3.json --against baseline
python3 -m rf_platform.analyst memory history
python3 -m rf_platform.analyst memory timeline "wifi:ap:02:10:7A:00:30:11:Telemetry-GW"
python3 -m rf_platform.analyst memory verify 2                           # recompute assessment 2 from stored snapshots
python3 -m rf_platform.analyst memory annotate --entity <key> --kind known_device --text "Facilities beacon"
python3 -m rf_platform.analyst memory audit
```

- **Append-only.** Database triggers reject `UPDATE` and `DELETE` on snapshots, baselines, assessments, and annotations. Snapshot IDs are content hashes and are re-verified on every read, so an edited payload is rejected.
- **Versioned baselines.** Setting a baseline again creates version *n + 1*, records who set it and why, and points to the version it supersedes. Earlier versions remain.
- **Since the last assessment.** `--against last` (the default, and **Since last assessment** in the dashboard) compares with the state at the previous assessment. Assessments therefore chain over days instead of always going back to the baseline. If nothing has been assessed yet, the comparison uses the active baseline.
- **Reproducible history.** `verify` recomputes a stored deterministic assessment from its stored snapshots and confirms the same investigation ID and findings. For an LLM-authored assessment, it re-runs the cited tools and re-checks every citation against the stored evidence.
- **Operator review.** `confirm`, `dispute`, `note`, `known_device`, and `unknown_device` annotations are separate records. Reports show them beside the findings that cite those entities, but they never change evidence, findings, or investigation IDs. In the dashboard, each cited entity has **Mark known** and **Note** actions.
- **Identity uncertainty.** `find_identity_candidates` lists earlier entities that share a new entity's advertising fingerprint (protocol, manufacturer ID, service UUIDs, appearance) and went quiet. A new BLE device can then be reported as a *possible* rotated address of a known one. This is an open hypothesis citing both entities, never a merge.

## How it works

```text
/api/status ──capture──▶ Snapshot (trimmed, test truth stripped, content-hashed ID)
                               │
                        EvidenceStore ◀── citable references
                               │
       provider ──calls──▶ ToolGateway ──▶ 3 read-only tools ──▶ computed:<id> results
   (deterministic or LLM)      │  allowlist · argument validation · call budget · audit
                               ▼
                         draft findings
                               │
                     grounding check (code) ──▶ investigation.json + report.html + bundle
```

### Evidence references

| Reference | Points to |
|---|---|
| `snapshot:<label>:<hash>` | Collection state of one capture: accepted/rejected/dropped counts, detections returned |
| `entity:<key>@<snapshot>` | One discovery-table row in that snapshot |
| `event:<event id>` | One normalized detection (`validation_event_id` in demo data; a content hash otherwise) |
| `sensor:<device>@<snapshot>` | One scanner assignment |
| `computed:<hash>` | One read-only tool result from this investigation |

A snapshot ID is a hash of its content, so a modified snapshot file is rejected on load. Validation fixtures carry `scenario_*` truth fields. These are removed before anything reaches the analyst, and a test enforces it.

### Tools

| Tool | Returns |
|---|---|
| `get_environment_summary(snapshot_id)` | Entities by protocol, WiFi roles, collection health, sensors, data-quality notes |
| `get_entity_history(entity_key)` | Presence, counts, signal, association, recurrence across snapshots; linked observations |
| `compare_baselines(reference, current)` | New, re-observed, and not re-observed entities; entities absent despite an expected detection rate; silent sensors; WiFi relationship changes; signal changes |
| `get_sensor_health(reference, current)` | Per sensor: protocols covered, new versus expected detections, status (`producing`, `silent_unexpectedly`, `insufficient_window`) |
| `get_topology_changes(reference, current)` | WiFi stations per access point in both snapshots, stations that moved, new APs |
| `get_evidence(event_ids)` | Up to 20 citable normalized detections |
| `find_identity_candidates(reference, current, entity_key)` | Earlier entities with the same advertising fingerprint that went quiet: possible same device under a new identifier |

**Expected detections.** An entity's or sensor's share of reference detections, multiplied by all new detections between the two snapshots. An absence is flagged only when at least 2 new detections were expected for an entity, or 3 for a sensor. Shorter windows are reported as insufficient, not as absences. Sensor coverage is inferred from scanner assignments (`protocol` and any `--protocols` list), because demo detections carry no per-detection sensor.

**Signal changes.** A strong signal (above -42 dBFS) is a change only for a new entity, or for one that rose from at least 6 dB below the threshold. Jitter around the threshold is not reported.

The gateway enforces a call budget and a wall-clock budget. It refuses unknown tools and the roadmap's restricted tools (`start_passive_capture`, `modify_scan_schedule`, `share_report`, `export_evidence_bundle`). It also enforces the call budget. Every call is audited, including refusals. Phase 1 executes no restricted action. A recommendation that would need one is recorded as `requires_approval`, with `executed: false`.

### Report structure

Each finding separates:

- **Observed facts**: `(ref, field, value)` read from snapshot, entity, event, or sensor evidence;
- **Computed results**: `(ref, field, value)` from a `computed:` tool result;
- **Hypotheses**: candidate explanations, always including benign and sensor/collection alternatives. They are ranked by evidence: supported first, then open, then weakened; more cited support ranks higher. A rank orders explanations; it never turns one into a fact;
- **Assessment** and **confidence**.

Every report states its limitations. HTML and JSON are rendered from the same investigation record. Device names and SSIDs are HTML-escaped and treated as data.

### Grounding

`grounding.check` fails a finding when a cited reference does not exist, when the cited field is missing, or when the stated value differs from the evidence. It also fails when an observed fact cites a computed result (or the reverse), or when a hypothesis cites a missing reference. Recommendations are checked too: a restricted tool not marked `requires_approval`, or any executed action, fails the check. Numbers in titles and assessments that match no cited value produce warnings for review. A hypothesis marked `supported` must cite supporting evidence, and one marked `weakened` must cite what weakens it. Every change that `compare_baselines` flags must have its own finding of the matching category. That covers new entities, relationship changes, signal changes, silent sensors (`sensor_gap`), and absent entities whose sensor is still producing (`missing_entity`). A finding that fails is shown as **Unsupported**.

## Providers

`deterministic` (default) is rule-based, needs no model, and is the baseline against which any LLM provider is compared.

`pydantic-ai` is optional and needs Python 3.11+:

```bash
pip install -e '.[analyst-llm]'
python3 -m rf_platform.analyst investigate before.json after.json --out run --provider pydantic-ai
python3 -m rf_platform.analyst investigate before.json after.json --out run --provider pydantic-ai --model ollama:<model>
```

`--model openai-codex:<model>` uses an existing Codex CLI login (ChatGPT subscription). It reads `~/.codex/auth.json` read-only. Refresh tokens are single-use, so if the access token has expired, the refreshed token stays in memory only and the Codex CLI will need `codex login` again. Run it while the CLI's access token is still valid.

The default model is `anthropic:claude-opus-5-5`, with server-side refusal fallback enabled. The model only plans and writes. It sees the same three read-only tools through the same gateway. Its final answer must match the draft schema, and an output validator returns grounding failures for correction. Every change flagged by `compare_baselines` must have its own finding of the matching category; the grounding check enforces this for every provider. If the model is unavailable or cannot produce a grounded draft, the investigation falls back to deterministic analysis, and the report records the provider error. The tests use a scripted local model and make no API calls.

## Evaluation

Held-out scenarios in `tests/fixtures/analyst/` are generated from the real backend by `scripts/build_analyst_fixtures.py`:

| Scenario | Current window | Correct outcome |
|---|---|---|
| `new_entity` | New BLE beacon, a station moves AP, strong signal | Those three changes |
| `ap_outage` | One AP and its station go silent; the WiFi sensor keeps producing | Two missing entities; receiver fault weakened |
| `wifi_sensor_outage` | All WiFi traffic stops; other sensors produce | One sensor gap; no WiFi device reported missing |
| `quiet_control` | Normal traffic | No change findings |

Each scenario's `truth.json` is read only by the evaluator.

```bash
python3 scripts/evaluate_analyst.py                                    # deterministic
python3 scripts/evaluate_analyst.py --provider pydantic-ai --model openai-codex:<model> --runs 2
```

Results go to `.demo/analyst/eval-<UTC>/results.json`, with a bundle per run. The results record whether each draft was written by the provider or came from the deterministic fallback.


`rf_platform/analyst/evaluate.py` scores an investigation against scenario truth from the fixture. It checks:

- whether the expected changes were found (new entity, relationship change, strong signal);
- citation validity;
- missing-data handling: no absence presented as a departure;
- whether a benign alternative was offered;
- that no action was executed and only read-only tools ran.

## Limits

- `/api/status` returns at most 120 detections, so older events are not citable. The report says so when the limit is reached.
- Demo first- and last-seen times are pinned to a synthetic reference date. Changes are therefore detected by comparing snapshots, not by time windows.
- Sensor attribution comes from scanner assignments; detections in the demo path do not carry `source_sensor`.
- RF observations identify emitters and associations, not people or intent.
