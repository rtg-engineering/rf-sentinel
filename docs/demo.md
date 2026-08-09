# RF Sentinel Public Demo

RF Sentinel has a hardware-free public demo mode that replays deterministic, synthetic RF detection events into the normal dashboard. It is designed for portfolio and reviewer use without SDR hardware, local captures, or real RF environment data.

## Run

```bash
docker compose -f docker-compose.demo.yml up --build
```

Open:

```text
http://127.0.0.1:8080
```

To use another host port:

```bash
RF_SENTINEL_DEMO_PORT=8081 docker compose -f docker-compose.demo.yml up --build
```

Stop the demo:

```bash
docker compose -f docker-compose.demo.yml down
```

The wrapper scripts do the same thing:

```bash
./scripts/demo.sh
./scripts/demo_stop.sh
```

## What You Should See

- The dashboard starts in live demo mode with synthetic SDR assignments.
- RF Health counters climb as events replay.
- Detections populate for Bluetooth Classic, BLE, Zigbee / 802.15.4, WiFi APs and stations, TPMS, walkie-style sub-GHz, FM broadcast, VLF/LF/MF, and passive cellular awareness.
- The 2.4 GHz chart lights up across Bluetooth, BLE, Zigbee, and WiFi channels.
- The WiFi tab can show AP/station topology inferred from synthetic 802.11 frames.
- The Start button resumes replay after Stop; Clear wipes the table and the replay repopulates it.

## Dataset

The generated file lives under:

```text
.demo/events/public-demo-events.jsonl
```

Each JSON line contains a synthetic event with an `offset_s` replay timestamp. The demo intentionally uses notional device names, MAC-like identifiers, RF signal strengths, and protocol metadata. The `.demo/` directory is gitignored and should not be committed.

Regenerate it manually:

```bash
python3 scripts/generate_demo_events.py --output .demo/events/public-demo-events.jsonl
```

## Smoke Checks

```bash
curl http://127.0.0.1:8080/api/devices
curl http://127.0.0.1:8080/api/status
```

A healthy demo reports `running: true`, `decoder_stats.demo_mode: true`, synthetic demo devices, and a non-empty `discovery_table` after a few seconds.

## Troubleshooting

- If port 8080 is already in use, set `RF_SENTINEL_DEMO_PORT=8081`.
- If the page opens but no detections appear, wait 10 seconds and refresh `/api/status`.
- If Docker was already running an older image, run `docker compose -f docker-compose.demo.yml down` and start again.
