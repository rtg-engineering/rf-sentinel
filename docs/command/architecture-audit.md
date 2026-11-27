# RF Sentinel Command: architecture audit (FOC-001)

What exists, what Command adds, and where the seams are. Audited at `main` after PR #24 (2026-11-27). Details and
tests: [source-capabilities.md](source-capabilities.md). Task status: [`PROGRESS.md`](../../PROGRESS.md). The security
review is kept privately (not published).

## Today: one station

```
browser / CLI ──HTTP──> dashboard (ui/backend/app.py)
                          ├─ auth: operators, roles, sessions (rf_platform/auth.py)
                          ├─ agent: plan → readiness → run (rf_platform/agent/*)
                          │    └─ AgentGateway: typed tools, mode ceiling, approvals, audit
                          ├─ scanner control (/api/scan/*, /api/config)
                          ├─ Analyst (rf_platform/analyst/*)
                          └─ /fleet: coordinator + scripted scenario, in-process (rf_platform/fleet/*)
plugins (subprocess, JSON lines) ──HTTP/WS──> IQ service (rf_platform/soapy_gateway) ──> receivers
cellular adapter ──HTTP──> Cellular Survey service (modem, no SIM)
```

The station is a sound unit to reuse: collection is typed and bounded, the agent cannot reach transmit operations,
runs are admitted one at a time, and every run ends in a checksummed report with its audit trail.

## Command: what is added

| Component (plan §2) | Reuse | Gap |
|---|---|---|
| Station bridge | `RFEvent` shape, agent `on_detection` callbacks, run reports | Versioned envelope with event ID, source/receipt time, clock state; MQTT client; local queue for disconnects |
| Hub | None (new process) | Identity checks, envelope validation, dedup, health, per-site policy |
| Site model | `fleet.Node` (position, base protocols, online) | Zones, floor plan, capabilities, coverage caveats |
| Correlator | `Coordinator` entity tracking, `geo.wcl`, "outside the fleet" test | Correlation across stations by event ID and time window; observation identity kept separate from physical identity |
| Mission broker | `AgentGateway` (typed tools, approvals, audit), admission lock, readiness | Node leases, command ID/expiry/idempotency, revalidation on the station, remote stop |
| Global Analyst | `rf_platform/analyst` (evidence store, grounding, bundles) | Inputs from several stations, citing source event IDs |
| UI / CLI | `/fleet` map and timeline, `/agent` mission page | Station health, baseline vs changed, report export per incident |
| Demo simulator | `fleet/scenario.py` (ground truth, scoring), `agent/simulate.py` | Run the scenario through the real hub pipeline, not in-process |

## Seams

1. **Station → hub:** the agent's `on_detection` stream and run completion are the natural publish points. Envelopes
   carry metadata only. IQ stays on the station, referenced by hash (as `record_iq` evidence already is).
2. **Hub → station:** a mission arrives as the same typed request the dashboard accepts today
   (`/api/agent/run` with a validated plan), so the station's gateway, admission and readiness still decide. The
   hub never gets a path around them.
3. **Coordinator:** `Coordinator.tick(t, sightings)` takes sightings as plain dicts, so it can run on the hub fed by
   envelopes instead of the scripted scenario. Its decisions already carry a reason string.

## Before networking

A private security review lists the items to close before any network-accessible demo. In summary: transmit-capable
operations must require operator authorization, anything crossing the network needs TLS and station identity, and
remote missions use only the typed agent path.

## Not decided here

- Broker choice and deployment (Mosquitto is the likely default; it is not yet in this repository).
- Whether the hub is a separate service or a dashboard mode.
- Floor plan input format.

These are FOC-002 to FOC-004 decisions for owner review.
