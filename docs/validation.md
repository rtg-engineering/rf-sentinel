# Measured Validation

RF Sentinel's validation harness runs a finite, deterministic synthetic fixture through the normal Docker demo replay, normalization, discovery table, and status API. It pauses the replay after baseline activity, verifies the interruption, resumes it, and compares three fresh runs. A headless browser evaluates the **shipped dashboard's** Pattern-of-Life functions against the captured API rows. No separate classifier or ingestion implementation supplies the results.

This measures platform behavior under synthetic replay. It does **not** measure live RF decoder accuracy, receiver sensitivity, uncontrolled protocol coverage, production performance, or browser paint latency.

## Run

From the repository root on a Linux host with Python 3.10+, Git, and a working Docker Engine using Linux containers:

```bash
./scripts/validate_demo.sh
```

No host Python packages, SDR hardware, Compose installation, or host browser are required. The command builds `Dockerfile.demo`, builds `Dockerfile.validation` with Chromium and Playwright, runs the regression tests inside the validation image, then performs three independent replays. Initial builds need network access and disk space for Chromium. Subsequent builds reuse Docker layers.

Each backend receives a unique container name and an ephemeral loopback-only host port. The harness uses its own containers and does not stop an existing demo. Containers are removed after success, failure, SIGINT, or SIGTERM. Images and local evidence remain. SIGKILL or host/daemon failure cannot be cleaned up by the interrupted process; see troubleshooting.

A zero exit status requires all scenario, accounting, recovery, repeatability, and rendering checks to pass. A nonzero exit status indicates failure; inspect `checks` in the JSON and the logs. An exclusive file lock prevents two validations from overwriting the same evidence directory.

```bash
RF_SENTINEL_VALIDATION_DURATION_S=300 \
RF_SENTINEL_VALIDATION_RATE=10 \
RF_SENTINEL_VALIDATION_RUNS=3 \
./scripts/validate_demo.sh
```

| Variable | Default | Meaning |
|---|---:|---|
| `RF_SENTINEL_VALIDATION_DURATION_S` | 30 | Nominal replay duration **per run**, excluding setup, pause, analytics, and teardown. |
| `RF_SENTINEL_VALIDATION_RATE` | 10 | Target events per second; ingestion and scheduling overhead may reduce achieved throughput. |
| `RF_SENTINEL_VALIDATION_RUNS` | 3 | Fresh runs; values below three are rejected. |

Duration and rate must be finite and positive. The fixture contains `max(25, floor(duration × rate))` events, ensuring a complete 21-event baseline plus four changes even for short configurations. Very high rates can exceed the observer's ability to capture the baseline or all receipts; that is a failed measurement, not an assumed pass. `--skip-build` is for development with current local images only; omit it for reproducible evidence.

## Scenario and test truth

The validation fixture contains five phases, each with `scenario_phase`, `scenario_truth_id`, and `scenario_expected_outcome` metadata:

1. `baseline`: the existing nine protocol families, recurring entities, and five-station DemoMesh topology.
2. `new_entity`: a new synthetic BLE beacon.
3. `relationship_change`: the Telemetry station moves to the FieldLab AP; the five mesh stations remain associated with DemoMesh.
4. `off_cadence_activity`: the new beacon receives a strong-signal observation, which the normal UI reports as proximity-review anomaly context. This tests the strong-signal alternative, not a new off-cadence detector.
5. `operator_review`: a final mesh observation preserves 231-day weekly recurrence.

For validation only, `RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800` anchors synthetic observation dates to Sunday, 2026-11-22 at 12:00 UTC. This makes weekend classifications reproducible on any execution day. The historical dates are synthetic input, **not** 231 days of runtime. Emission, acceptance, API observation, and recovery timestamps are captured during actual execution and are not backdated.

Test truth is not displayed as inferred live intelligence. Ordinary demo generation also has deterministic source event IDs; production RF ingestion is unchanged.

## Event accounting and lifecycle

Demo `/api/status` retains its existing fields and adds `validation_metrics`:

- `events_attempted`: source records submitted to the normal demo ingestion transaction.
- `events_accepted`: records normalized and represented in the normal API-visible detection state.
- `events_rejected`: records not accepted, with stable reason counts: `malformed_event`, `normalizer_filtered`, or `not_api_visible`.
- `events_dropped`: zero for this synchronous replay path, which has no intervening event queue. This makes no claim about live receiver or pipeline queue loss.
- `replay_cycle`, `last_event_sequence`, `last_event_attempted_at`, and `last_event_accepted_at`: source progress and actual wall-clock times.
- `receipts`: at most 10,000 recent accepted event identities, each with a monotonically increasing `accepted_index`, source sequence, replay cycle, actual emission time, and state-visibility time. `GET /api/status?receipts_after=N` returns only receipts with `accepted_index > N`; without the parameter the full buffer is returned. The harness polls with its highest seen index so response size stays bounded. Before this cursor, the full receipt list made each poll grow from ~125 KB to ~511 KB over a 3,000-event replay, inflating observed latency over the run.
- `complete`: the finite fixture has finished.

Every attempted event must reconcile as accepted, rejected, or dropped. The validation fixture is entirely valid, so every event must be accepted and observed, with zero rejections/drops. Malformed JSON records are retained as invalid source records and counted as rejections. Unit tests exercise rejection paths separately from the valid performance fixture.

The receipt is recorded under the state lock after the normal ingestion state update. It observes that transaction; it does not replace detection, entity, topology, or Pattern-of-Life processing. If the receipt buffer overwrites records before the observer sees them, identity completeness fails.

Pause retains detections, counters, and the current source position. Resume with `preserve_detections=true` continues from that position without skipping the pending event. `/api/clear` resets detection state and metrics together; it does not rewind the fixture. Start without preservation also resets state. For a complete new validation, start a fresh container, as the harness does. Demo metrics and receipts are not exposed in live mode.

## Latency

The backend captures `validation_emitted_at` immediately before normalizing a replayed event. The observer polls the ordinary `/api/status` endpoint, correlates each receipt by deterministic event ID, and records the first response receipt time. Their difference in milliseconds is the **observed event-to-API visibility latency**. Linux host and containers share the wall clock. Negative or non-finite samples fail validation; clock adjustments can invalidate a run.

The measurement includes request/response and polling delay, JSON processing, and observer resource-sampling overhead. It is an upper-bound observation of availability through the UI's API, not pure backend compute time or pixel-display latency. Polling is nominally every 10 ms; each request and periodic Docker process sampling add overhead. No latency comes from configured replay offsets.

Percentiles use the nearest-rank method: sort N observations and select index `ceil(p × N) - 1`. Minimum, P50, P95, P99, maximum, and sample count are reported. Empty samples are `null`, not zero. All observations from all runs contribute to top-level latency statistics; each run also retains its samples.

## Recovery

After at least 21 accepted baseline events, the harness calls the existing `/api/scan/stop` endpoint. It verifies `running=false`, `worker_alive=false`, and unchanged acceptance counts across a 300 ms pause. It then calls `/api/scan/start` with state preservation and requires new accepted events.

This is a real **replay-worker pause/resume** test with retained in-memory state. Backend crash and container restart are covered separately below; durable state retention is not claimed. The HTTP service remains reachable so interruption is detected through normal health fields and stalled acceptance. Recovery time runs from the resume request to the first observed post-resume acceptance. Each run records interruption, restoration, recovery timestamps, and subsequent accepted-event count. No interruption or no post-resume acceptance causes failure.

## Crash restart and state rebuild

After the three normal runs, a fourth container (`run-restart`) replays the same fixture. Once at least half the fixture has been accepted, the harness sends `docker kill --signal KILL` to the backend, waits for exit, requires exit code 137 and an unreachable API, then runs `docker start` on the same container and waits for `/api/status`. The ephemeral host port is re-read because Docker may reassign it.

RF Sentinel keeps detection state **in memory only**. The harness therefore checks the honest outcome, not durable retention:

- `post_restart_state_clean`: the restarted process reports zero attempted/accepted events and an empty discovery table (no partial or corrupt state);
- `recovered`: after resume, the replay is accepted again from the source fixture;
- `rebuilt_matches_uninterrupted`: the rebuilt run's stable result digest equals the uninterrupted runs' digest.

`restart_recovery` in `results.json` records `pre_kill_events_accepted` (lost with the process, then re-ingested), `kill_to_health_ms` (SIGKILL to first healthy status, including container start), `recovery_time_ms` (resume request to first accepted event), and `rebuild_complete_ms` (resume to fixture completion, dominated by the replay rate). Latency samples are taken from the rebuild pass only. Docker's `RestartCount` does not count a manual `docker start`, so it stays zero. This tests a crash-then-restart from the source replay, not durable state, live receiver reconnection, or orchestration restart policies.

## Soak drift

Each run reports `latency_trend` (P50/P95 for the first and last thirds of accepted events and their P95 delta) and `resources.rss_growth_bytes` (last minus first sampled RSS). `soak` in `results.json` and the result card show the maximum of each across runs. These are **reported observations, not pass criteria**; no drift threshold has been approved. Use the 300-second configuration for a meaningful soak, and do not run other Docker workloads or another validation concurrently, since contention distorts latency and CPU. If you run a soak in a separate `--output` directory, the shared `rf-sentinel:*` image tags are still rebuilt by any other validation started without `--skip-build`.

## Evidence bundle

At the end of every run, success or failure, the harness writes `bundle/rf-sentinel-validation-<commit>[-dirty]-<UTC>.tar.gz` and a matching `.sha256`. The archive contains the fixture, `results.json`, every per-run and rows file, the card HTML/PNG, logs with repository and home paths redacted, a `README.txt` with the replay command, and `MANIFEST.json` listing the SHA-256 of every file plus version, source hash, and configuration.

Anyone can check a bundle without Docker or the harness:

```bash
python3 scripts/verify_evidence_bundle.py .demo/validation/bundle/<archive>.tar.gz
```

The verifier checks every manifest checksum and that no unlisted files are present. It also checks the fixture hash and event count, and that each run's accounting reconciles. It recomputes each run's stable digest from the per-run files and compares it to `results.json`. Last, it confirms the restart rebuild digest equals the uninterrupted digest. It exits nonzero on any mismatch. Checksums show integrity, not authorship. Distribute the `.sha256` through a separate channel if that matters.

## Repeatability and resources

Three independent containers use identical fixture bytes and configuration. Stable comparisons include protocol entity counts, entity count, WiFi AP/station counts and station-to-AP relationships, per-entity weekly/weekend/span/anomaly classifications, rejection/drop accounting, and scenario outcomes. SHA-256 covers canonical JSON with sorted keys and compact separators. Timestamps, timings, resources, process/container IDs, and ports are excluded. Fixture and configuration hashes are recorded separately.

Pattern classifications come from `patternScore` and `analyticsForRow` in the normal frontend, evaluated in Chromium against captured `/api/status` discovery rows. This does not duplicate their logic in the benchmark. `conclusions.protocol_counts` counts **entities** by normalized protocol; `fixture.protocols` lists source protocol families.

Linux `/proc/1/stat` inside the backend container supplies process RSS and CPU time. Starting RSS is measured before replay. Peak RSS and CPU percentage are **sampled peaks**, approximately once per second, not guaranteed instantaneous maxima. CPU is process CPU seconds divided by elapsed sample time, multiplied by 100, and may exceed 100 on multiple cores. Resource sampling competes with API polling. Docker supplies restart counts. Completion entity counts and output log bytes are measured. Unavailable RSS/CPU measurements are `null` with explanatory notes; they are never estimated.

## Evidence and schema

All generated files stay under ignored `.demo/validation/`:

```text
results.json             aggregate schema-versioned report
run-1.json               per-run accounting, checks, samples, conclusions
run-2.json
run-3.json
rows-1.json              normal API discovery rows used for UI analytics
rows-2.json
rows-3.json
run-restart.json         SIGKILL + restart + rebuild run
rows-restart.json
events.jsonl             exact synthetic validation fixture
result-card.html         generated from results.json
result-card.png          Chromium rendering of that HTML
logs/                    image builds, tests, backend logs
bundle/                  checksummed .tar.gz evidence bundle and .sha256
```

`results.json` schema version `1.0` has these stable fields:

| Field | Meaning |
|---|---|
| `schema_version`, `application`, `application_version` | Schema, product, and Git commit; `-dirty` marks any tracked change or untracked file outside `docs/*.md`. |
| `source_sha256` | Hash of key application/build/harness/test source files, distinguishing local changes. |
| `started_at`, `completed_at`, `duration_s` | UTC ISO timestamps and total elapsed validation time, including builds/tests and replay setup/teardown, before card rendering. |
| `public_safe`, `synthetic_replay` | This harness uses generated synthetic observations only. |
| `fixture` | Relative path, SHA-256, event count per run, source protocol families. |
| `configuration`, `configuration_sha256` | Actual requested configuration and its hash. |
| `event_accounting` | Summed attempted/accepted/rejected/dropped counts and rejection reasons across runs. |
| `latency_ms` | Aggregate sample count, minimum, P50/P95/P99, maximum. |
| `recovery` | All-run boolean success, maximum recovery time, summed post-recovery accepted count; timestamps describe the first run. Per-run files contain each interruption. |
| `repeatability` | Run count, comparison booleans, normalized digest, per-run digests. |
| `resources` | First-run starting RSS, maximum sampled peaks, total restart count and log bytes, completion entity count, unavailable-measurement notes. |
| `conclusions` | First run's stable entity/protocol/topology and UI Pattern-of-Life conclusions. |
| `restart_recovery` | SIGKILL/restart outcome, clean-state check, timings, rebuild accounting/latency and digest comparison. |
| `soak` | Maximum per-run P95 drift and RSS growth; observations only. |
| `checks`, `overall_status` | Required checks and `pass`/`fail`. |
| `limitations` | Claim boundaries included in the result card. |

Per-run JSON contains detailed scenario checks. Setup or execution failures produce a failed report and available logs; a failed per-run file preserves partial accounting when a status response was available. Top-level aggregate defaults on an aborted run are not a completed throughput measurement. Read the failure record before interpreting them.

The HTML and PNG show fixture size, aggregate accounting, P50/P95, recovery, repeatability, total duration, source version, and limitations. Values are read from JSON and formatted for display. PNG failure makes the command fail. No performance numbers are entered manually.

The result card and JSON remain local for review. After a successful **reviewed** run, the reviewed summary may be copied to `docs/validation/latest-public-result.json` and the card to `docs/media/rf-sentinel-validation-card.png`. The harness does not perform that promotion or publish a website. Resolve the `-dirty` source state and rerun before publishing commit-specific claims.

## Operator walkthrough recording

`python3 scripts/record_walkthrough.py [--build]` records about 50 seconds of the shipped dashboard while the demo backend replays the validation scenario's minimum fixture: 21 baseline events, then the four changes, paced roughly 12 seconds apart. Each scene (baseline, new entity, relationship change, operator review) starts only after the dashboard's own status shows that state, so a missed change fails the recording instead of producing a misleading video. The script adds only a caption bar, a synthetic-replay badge, and outlines on the relevant row. It requires host ffmpeg and writes `docs/media/rf-sentinel-operator-walkthrough.mp4` and `.gif`. It is a demonstration, not a measurement, and is not part of `validate_demo.sh`.

## Tests and troubleshooting

Run tests locally with the project's Python dependencies installed:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The one-command Docker runner also executes these tests. They cover stable IDs, schema fields, percentiles, accounting, malformed-event rejection, digest comparisons, card values, missing resources, health timeout, API compatibility, reset, and pause/resume scheduling.

- **Build failure:** inspect `logs/Dockerfile.demo.log` or `logs/Dockerfile.validation.log`. Check daemon access, disk space, and package-network connectivity.
- **Timeout or no acceptance:** inspect the failed `run-<n>.json` and backend log. A finite replay must finish; timeout never counts as recovery.
- **Scenario or identity mismatch:** inspect per-run checks and rows. Use a lower rate if sampling cannot observe the short phase boundaries. Do not remove checks to claim a pass.
- **Browser failure:** inspect the error in `results.json`. Chromium and Playwright belong to the validation image; rebuild without `--skip-build` after source/dependency changes.
- **Unclean external termination:** list `docker ps -a --filter name=rf-validation-`, identify the interrupted run's unique names, and remove only those containers with `docker rm -f <name>`.
- **Concurrent run:** wait for the existing process holding `.demo/validation/.lock`; do not delete its evidence while it is active.

This is a reproducible synthetic platform evaluation, not certification, a sustained live-RF soak, or evidence of classification accuracy in an uncontrolled environment.
