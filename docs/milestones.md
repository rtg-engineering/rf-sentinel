# RF Sentinel Milestones

This roadmap is adapted for this repository.

## Milestone 1: Product Definition And Repo Restructure

Goal: Turn RF Sentinel from a Bluetooth-focused app into the front end and first implementation of a multi-protocol RF intelligence platform.

Deliverables:

- Product README and architecture docs.
- Shared `rf_platform` package for schemas and platform primitives.
- Protocol plugin directories under `rf_platform/plugins/`.
- Common RF event schema.
- Common entity model.

## Milestone 2: Passive Capture And Ingestion

Goal: Collect passive observations from multiple RF sources.

Initial sources:

- Existing BLE/Bluetooth Classic SDR stream ingestion.
- Existing `rf_platform/plugins/bluetooth-classic` plugin.
- Existing `rf_platform/plugins/zigbee-802154` plugin.
- Future WiFi monitor-mode ingestion.
- Future TPMS ingestion.
- Future drone/UAS RF capture hooks.

## Milestone 3: Unified Event Schema And Database

Goal: Store every protocol observation in a consistent shape.

Core event fields:

- `timestamp`
- `protocol`
- `subtype`
- `frequency_hz`
- `channel`
- `rssi_dbm`
- `confidence`
- `device_id`
- `partial_id`
- `metadata`
- `source_sensor`

Storage plan:

- SQLite first.
- JSONL and CSV import/export.
- Replay mode for saved sessions.
- Postgres later.

## Milestone 4: Protocol Decoding MVP

Goal: Extract useful passive metadata from known protocols.

- BLE advertisements.
- Bluetooth Classic LAP/UAP observations.
- WiFi probes, beacons, AP/client identifiers, OUI, RSSI.
- Zigbee / IEEE 802.15.4 channel, PAN ID, addresses, frame type.
- TPMS sensor ID and recurring observations.
- Drone/UAS Remote ID, WiFi APs, and RF signatures where available.

## Milestone 5: ML Signal Classifier

Goal: Classify bursts when full protocol decoding is not possible.

- SDR burst detection.
- Spectrogram and normalized I/Q tensor generation.
- Protocol classifier for BLE, BTC, WiFi, Zigbee, TPMS, drone/UAS, unknown, and noise.
- Confidence scoring and unknown rejection.
- Evaluation reports.

## Milestone 6: Entity Resolution

Goal: Turn RF observations into tracked things.

- BLE address entities.
- WiFi MAC/BSSID entities.
- BTC `UAP:LAP` partial identities.
- Zigbee address/PAN entities.
- TPMS sensor ID entities.
- Drone Remote ID, WiFi AP, and RF fingerprint entities.
- First seen / last seen, RSSI history, protocol associations, and confidence.

## Milestone 7: Pattern-Of-Life Analytics

Goal: Turn packets into intelligence.

- First-seen / last-seen summaries.
- Recurring presence detection.
- Time-of-day activity profiles.
- Co-occurrence and relationship graph.
- RSSI trend analysis.
- New-device, disappeared-device, and unusual-protocol alerts.
- Moving-emitter heuristics for drones and TPMS.

## Milestone 8: Web Dashboard MVP

Goal: Make the front end feel like a real product.

- Live events.
- Entities/devices.
- Protocol summary.
- Timeline.
- Alerts.
- Device detail page.
- Filters by protocol, time, RSSI, sensor, and confidence.
- WebSocket event updates.
- CSV/JSON export.

## Milestone 9: Drone / UAS Detection

Goal: Make drone RF awareness first-class.

- WiFi drone AP and controller/client detection.
- Remote ID ingestion.
- 2.4 GHz and 5.8 GHz RF activity classification.
- Drone/control-link candidate events.
- Possible drone activity alert.
- Drone timeline and association logic.

## Milestone 10: Multi-Sensor Support

Goal: Support multiple collection nodes.

- Sensor identity.
- Remote sensor agent.
- Central observation server.
- Sensor health and time sync.
- Per-sensor RSSI.
- Approximate location/floorplan support.
- Multi-node correlation.

## Milestone 11: Reporting And Evidence Exports

Goal: Support customers, audits, field work, and demos.

- PDF/HTML reports.
- Protocol summaries.
- Discovered entities.
- New and recurring devices.
- Drone events.
- Suspicious activity.
- Timelines and top talkers.
- Unknown RF bursts.
- Analyst notes and replayable session bundles.

## Milestone 12: Authorized Defense / Lab Effects Module

Goal: Add active capabilities only as a controlled optional module.

- Separate from passive core.
- Explicit enable flag.
- Role-based access.
- Audit logs.
- Lab-mode banner.
- Hardware allowlist.
- Documentation emphasizing authorized environments.

## Milestone 13: SDK / Licensing Package

Goal: Make the platform evaluable and licensable.

- Python SDK.
- `rfintel scan`
- `rfintel classify capture.iq`
- `rfintel replay session.jsonl`
- `rfintel export-report`
- Docker deployment.
- API docs.
- Model versioning.
- Config profiles for research, commercial, and defense/lab.

## Milestone 14: Validation And Benchmarking

Goal: Prove the platform outside the lab.

- Multiple SDRs, Bluetooth adapters, and WiFi adapters.
- Different locations and days.
- Noisy environments and low-SNR cases.
- Drone detection scenarios.
- Classification accuracy, false positive/negative rates, decoder success rate, entity tracking accuracy, and alert quality.

## Milestone 15: Commercial Demo

Goal: Build the wow demo.

- Live BLE, WiFi, BTC, TPMS, Zigbee, and drone/possible-UAS observations.
- Dashboard and entity timelines.
- Pattern-of-life detection.
- New-device and possible-drone alerts.
- Exported report.
- Replay mode.
- API integration.
