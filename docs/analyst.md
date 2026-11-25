# RF Sentinel Analyst (read-only investigation)

The Analyst answers *"what changed in this wireless environment, and what evidence supports it?"* It works from captured `/api/status` snapshots. It cannot change ingestion, detections, receivers, or scan schedules. Every factual claim in a report is re-read from the cited evidence by code, not by a model.

This is Phase 1 of the autonomous-investigation roadmap: read-only, evidence-backed reports over the deterministic Docker scenario. It does not run missions, keep persistent memory across sessions, or execute any collection action.

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
| `compare_baselines(reference, current)` | New, re-observed, and not re-observed entities; WiFi relationship changes; signal changes |

The gateway refuses unknown tools and the roadmap's restricted tools (`start_passive_capture`, `modify_scan_schedule`, `share_report`, `export_evidence_bundle`). It also enforces the call budget. Every call is audited, including refusals. Phase 1 executes no restricted action. A recommendation that would need one is recorded as `requires_approval`, with `executed: false`.

### Report structure

Each finding separates:

- **Observed facts**: `(ref, field, value)` read from snapshot, entity, event, or sensor evidence;
- **Computed results**: `(ref, field, value)` from a `computed:` tool result;
- **Hypotheses**: candidate explanations, always including benign and sensor/collection alternatives;
- **Assessment** and **confidence**.

Every report states its limitations. HTML and JSON are rendered from the same investigation record. Device names and SSIDs are HTML-escaped and treated as data.

### Grounding

`grounding.check` fails a finding when a cited reference does not exist, when the cited field is missing, or when the stated value differs from the evidence. It also fails when an observed fact cites a computed result (or the reverse), or when a hypothesis cites a missing reference. Recommendations are checked too: a restricted tool not marked `requires_approval`, or any executed action, fails the check. Numbers in titles and assessments that match no cited value produce warnings for review. A finding that fails is shown as **Unsupported**.

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
