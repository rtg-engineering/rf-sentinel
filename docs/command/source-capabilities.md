# RF Sentinel Command: source capabilities (FOC-001)

What the current repository already does that the Command layer can reuse, with the source path and the test that
covers it. Audited at `main` after PR #24 (2026-11-27). "Tested" means an automated test in `tests/`. "Hardware"
means it was also run on real receivers (two bladeRF 2.0, one cellular modem without a SIM).

## Station-side: collection

| Capability | Source | Test | Hardware |
|---|---|---|---|
| Built-in IQ service: devices, streams, retune, power sweeps, IQ sweeps; one owner per receiver (`409` when busy) | `rf_platform/soapy_gateway/backend.py`, `server.py` | `tests/test_soapy_gateway.py` | yes |
| Tune-and-capture block for hopping clients (`POST /streams/<id>/capture`) | `rf_platform/soapy_gateway/backend.py` (`RxWorker.capture`) | `tests/test_soapy_gateway.py::CaptureTest` | yes |
| Idle-stream reaper: a crashed client's stream is released after 30 s | `rf_platform/soapy_gateway/server.py` (`reap_idle_streams`) | `tests/test_soapy_gateway.py` | yes |
| Protocol plugins: BLE, Bluetooth Classic (receive-only `listen`), Zigbee, TPMS/sub-GHz, cellular survey adapter, drone RF adapter | `rf_platform/plugins/*`, `rf_platform/agent/core.py` (`plugin_command`) | `tests/test_agent.py::test_plugin_commands_are_receive_only_and_bounded`, `tests/test_drone_rf.py` | yes (BLE, Classic, cellular, drone link cues) |
| Level-based gain control, then decoder burst feedback | `rf_platform/agent/radio.py` (`auto_gain`), `core.py` | `tests/test_agent.py` | yes |
| Spectrum discovery 70 MHz–6 GHz with a band plan and a proposed follow-up | `rf_platform/agent/discover.py` | `tests/test_agent_discover.py` | yes |
| Simulated receivers and scripted scenarios (always marked simulated) | `rf_platform/agent/simulate.py` | `tests/test_agent*.py` | n/a |

## Station-side: agent, policy and operators

| Capability | Source | Test |
|---|---|---|
| Typed tool list; transmit-class operations named and always refused | `rf_platform/agent/gateway.py` (`TOOLS`, `NEVER`, `decide`) | `tests/test_agent_gateway.py::test_permission_matrix`, `test_observe_and_never_are_denied_without_waiting` |
| Server mode ceiling (`observe` < `approval-required` < `passive-auto`); unknown setting fails closed | `gateway.py` (`server_ceiling`, `effective_mode`) | `test_agent_gateway.py::test_ceiling_caps_requested_mode` |
| Approvals bound to the exact operation (content hash), single-use, named operator, expiry, cancel on stop | `gateway.py` (`ApprovalBroker`) | `test_agent_gateway.py::test_approve_deny_expire_cancel`, `test_named_operator_single_use` |
| Per-run audit trail (who asked, every tool decision) | `gateway.py` (`AgentGateway.audit`), written into `run.json` | `test_agent_gateway.py`, `tests/test_auth.py::test_approvals_are_recorded_under_the_signed_in_identity` |
| Operator identity: hashed tokens, roles viewer/operator/approver, immediate revocation, browser session plus CSRF header | `rf_platform/auth.py`, `ui/backend/app.py` (`_authenticate`) | `tests/test_auth.py` |
| Optional separation of duties (requester cannot approve own run) | `ui/backend/app.py` (`_approval_separation`) | `tests/test_auth.py::test_separation_of_duties` |
| Atomic admission: one agent run at a time per dashboard | `ui/backend/app.py` (`agent_admission_lock`) | `tests/test_auth.py::AdmissionTest::test_concurrent_runs_admit_one` |
| Restart history for standing watches (interrupted and missed cycles recorded) | `ui/backend/app.py` (`resume_agent_watches`) | `tests/test_auth.py::RestartHistoryTest` |
| Plain-language planning: deterministic grammar plus optional model planner with validation and fallback | `rf_platform/agent/workflow.py`, `playbook.py` | `tests/test_agent_workflow.py`, `test_agent_playbook.py`, `test_workflow_corpus.py` |
| Readiness checks before a run (receivers, decoders, modem, disk, alert delivery) | `rf_platform/agent/readiness.py` | `tests/test_agent_readiness.py` |

## Evidence and reporting

| Capability | Source | Test |
|---|---|---|
| Verifiable run report: `run.json` + `report.html` + `checksums.sha256` | `rf_platform/agent/report.py` | `tests/test_agent.py` |
| Public redaction of a run (addresses, names, cell IDs, serials, hostname) with source hash | `rf_platform/agent/publish.py` | `tests/test_agent_publish.py` |
| Ask about a run: numbers must cite a tool-call field | `rf_platform/agent/ask.py` | `tests/test_agent_ask.py` |
| Analyst: evidence store, grounding check, policy engine, investigation bundle with SHA-256 manifest | `rf_platform/analyst/` (`evidence.py`, `grounding.py`, `policy.py`, `report.py`) | `tests/test_analyst*.py`, `tests/test_missions.py` |
| Validation harness, result card, scorecards | `docs/validation/`, harness scripts | `tests/test_validation_harness.py` |

## Multi-node (simulated today)

| Capability | Source | Test |
|---|---|---|
| Coordinator: cue nearest nodes, locate (WCL), "outside the fleet" refusal, rogue-cell tasking, follow, coverage when a node drops | `rf_platform/fleet/coordinator.py` | `tests/test_fleet.py` |
| Scripted five-node scenario with ground truth and scoring | `rf_platform/fleet/scenario.py` | `tests/test_fleet.py::test_fixes_are_scored_against_truth` |
| Fleet map with playback and decision reasons | `ui/frontend/fleet.html`, `/api/fleet/scenario` | manual |

## Event shape

`rf_platform/events.py` defines `RFEvent` (protocol, subtype, timestamp, frequency, channel, RSSI, confidence,
device/partial ID, metadata, `source_sensor`). It has no event ID, schema version, receipt timestamp or clock
uncertainty. Those are needed before any event crosses a network (FOC-002).

## Not present

- No MQTT, broker client or station-to-hub transport anywhere in the repository.
- No hub process or persistent cross-station store. The coordinator keeps state in memory, inside one process.
- No node identity, receiver leases across stations, or command IDs with expiry or idempotency.
- No floor-plan view. `/fleet` uses a geographic basemap with online tiles.
