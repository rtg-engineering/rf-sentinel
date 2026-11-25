# Evaluating RF Sentinel

This guide walks through a hands-on evaluation: what you see in five minutes, how to run an investigation, how to check what it tells you, and what the demo does and does not prove.

**Want a guided evaluation, or an evaluation license for your hardware? [Contact RTG](https://rtgspectrum.com/contact/).**

## 1. The first five minutes (no hardware)

```bash
git clone https://github.com/rtg-engineering/rf-sentinel.git
cd rf-sentinel
docker compose -f docker-compose.demo.yml up --build
```

Open `http://127.0.0.1:8080`. Use `RF_SENTINEL_DEMO_PORT=8081` if port 8080 is busy. A deterministic synthetic replay drives the real dashboard:

- the detection table fills across protocol families, with RF health metrics and 2.4 GHz activity;
- the **WiFi** tab, set to **WiFi Map**, shows access points and their stations;
- **Analytics** opens pattern-of-life review: what recurs, on which days, and what is out of pattern.

## 2. Run an investigation

1. Click **Investigate**, then **Set baseline**. This snapshots exactly what the dashboard shows.
2. Let activity continue, then click **Investigate changes**.
3. Read the findings. Each one shows its leading explanation, the alternatives, and the entities it cites.
4. Click an entity chip to open its row in the detections table.
5. Click **Full report** for the complete HTML report.

The scripted version runs the same flow end to end through the backend's HTTP API, and scores the result against the scenario's ground truth:

```bash
python3 scripts/analyst_demo.py --build
```

## 3. Check what it tells you

RF Sentinel is built so you do not have to take its conclusions on trust.

- **Every claim cites evidence.** Each report fact is a `(reference, field, value)` triple. The grounding check re-reads every value from the evidence and marks any finding it cannot verify as **Unsupported**.
- **Bundles are checksummed.** In any investigation bundle, `sha256sum -c checksums.sha256` verifies every file.
- **Platform measurements are reproducible.** `./scripts/validate_demo.sh` reruns the published validation. `python3 scripts/verify_evidence_bundle.py <bundle>.tar.gz` checks the published evidence bundle without Docker.
- **Analyst accuracy is measured.** `python3 scripts/evaluate_analyst.py` scores the Analyst on held-out scenarios: a new device, an access-point outage, a receiver outage, and a quiet control.

Sample investigation reports: [`samples/analyst/`](samples/analyst/).

## 4. What the demo proves, and what it does not

| Demonstrated by the hardware-free demo | Not demonstrated by the demo |
|---|---|
| The normalized event pipeline, dashboard, entity tracking, and pattern-of-life analytics | Live decoder accuracy or receiver sensitivity |
| Event accounting, API latency, crash recovery, and repeatability (published measurements) | Performance in an uncontrolled RF environment |
| Analyst investigation, grounding, and drill-down on known scenarios | Coverage of devices your hardware cannot receive |
| Read-only operation: nothing is executed | Production support, scale, or multi-site operation |

Live performance depends on the radios, antennas, placement, and environment. It is measured during a pilot, not assumed.

## 5. Hardware and protocol status

| Protocol family | Receiver in this repository | Live status | In the public demo |
|---|---|---|---|
| Bluetooth Classic (LAP/UAP) | `plugins/bluetooth-classic` | Documented live: bladeRF, 60 MHz bank capture | Yes |
| BLE advertising | `plugins/bluetooth-lowenergy` | Documented live: HackRF via `sdr-gateway` | Yes |
| Zigbee / IEEE 802.15.4 | `plugins/zigbee-802154` | Documented live, time-sliced on HackRF | Yes |
| Sub-GHz / TPMS | `plugins/subghz-stack` | Documented live: 315 / 433.92 MHz auto-hop | Yes |
| WiFi 802.11 (APs, stations) | `plugins/wifi-80211` | Plugin present; characterized per deployment | Yes |
| Walkie-style sub-GHz voice | `plugins/walkie-talkie` | Plugin present; characterized per deployment | Yes |
| FM / AM broadcast | `plugins/fm-broadcast`, `plugins/am-broadcast` | Plugin present; characterized per deployment | FM |
| VLF/LF/MF | dashboard mode (SDRplay RSP2 only) | Characterized per deployment | Yes |
| Passive cellular awareness | `plugins/cellular-awareness` | Plugin present; characterized per deployment | Yes |

**Hosts:** Linux on `x86_64` or `aarch64`. `install.sh` rebuilds the native Bluetooth Classic receiver for the target architecture.

**Radios used in RTG's own testing:** bladeRF and HackRF. Other SoapySDR-supported front ends are assessed per evaluation.

**Known integration gap:** the dashboard's own device picker and "start scan" button depend on `sdr-gateway`. The shared passive BT/BLE detection view works without it. See [`docker/README.md`](../docker/README.md).

## 6. Licensing and next steps

RF Sentinel is proprietary commercial software; see [`LICENSE`](../LICENSE). Some plugins are distributed under separate open-source licenses; see [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md). Evaluation licenses, commercial licensing, and support terms are agreed directly with RTG.

- **Evaluation license:** run RF Sentinel on your own hardware or recorded data.
- **[RF Environment Assessment Pilot](pilot.md):** a fixed-scope engagement with a verifiable evidence report as the deliverable.
- **Integration:** connect your sensors or existing event sources.

**[Contact RTG](https://rtgspectrum.com/contact/)**
