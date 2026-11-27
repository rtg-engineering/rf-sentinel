# RF Sentinel Command: progress

Tracks the focused plan's tasks (FOC-001..013). Status: `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE` (with commit and
test evidence), `VERIFIED` (after review). "Existing" says what is already in the repository: present / partial /
absent. Details: [docs/command/](docs/command/).

| Task | Status | Existing | Notes |
|---|---|---|---|
| FOC-001 Source audit, contract, risk baseline | IN_PROGRESS (awaiting owner review) | n/a | `docs/command/architecture-audit.md`, `source-capabilities.md`; security review kept private. No code changed. |
| FOC-002 Versioned MQTT transport, identities | IN_PROGRESS | partial | Envelope v1 done: `rf_platform/command/envelope.py`, `docs/command/schemas/envelope-v1.schema.json`, `tests/test_command_envelope.py` (station identity `<hostname>/<arch>`, deterministic ids, clock state, TTL and idempotency for missions, no IQ). MQTT client, broker, TLS identities and ACLs not started. |
| FOC-003 Hub store, dedup, sensor health | TODO | partial | Run persistence and watch restart history are per station (`agent-runs/`). The coordinator state is in memory. |
| FOC-004 Floor plan and baseline view | TODO | partial | `/fleet` map, playback and decision reasons (kept as is by owner decision). Needs station health, baseline vs changed, and an offline basemap or floor plan (the current map needs online tiles). |
| FOC-005 Cross-station corroboration | TODO | partial | `Coordinator` cues neighbours and reasons about outages, simulated in-process (`tests/test_fleet.py`). Needs source event IDs and non-corroboration reporting. |
| FOC-006 Typed mission broker, approvals | TODO | partial | Station side present: `AgentGateway` typed tools, mode ceiling, hashed single-use approvals, roles, admission lock, audit. Absent: node leases, command expiry and idempotency, remote revalidation, durable approvals. |
| FOC-007 Four security scenarios | TODO | partial | Fleet scenario covers a new emitter with cueing and a node outage. Absent: approved maintenance device, rotating BLE identity, seeded variations, truth kept from the Analyst. Validation harness and result card exist. |
| FOC-008 Public demo and evidence kit | TODO | partial | `docker-compose.demo.yml`, demo mode, `/agent` recording mode, fleet video, `agent publish`. No three-station Command demo yet. |
| FOC-009 Two-station real-world test | TODO | partial | One station with two bladeRF 2.0 and a modem runs real missions. No second station on the network yet. |
| FOC-010 Evaluation landing page | TODO | absent | Website lives in a separate repository. |
| FOC-011 Distribution | TODO | absent | |
| FOC-012 Buyer feedback | TODO | absent | |
| FOC-013 Decision review | TODO | absent | |

## Owner decisions (2026-11-27)

- Drone RF coverage is in scope (overrides the plan's deferral).
- `/fleet` is kept as it is.
- The cellular modem has no SIM: cell surveys stay receive-only.
- Transmitting is allowed when a named operator authorizes it: agent transmit tools always ask; dashboard settings that
  transmit need a recorded authorization.
- Station identity in envelopes is the hostname and CPU architecture.
