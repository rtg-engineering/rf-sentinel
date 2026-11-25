# Operator agent

Ask RF Sentinel in plain language, and it runs the collection itself. It picks a receiver, sets and corrects the gain, runs the right decoder, and reports what it found, with a verifiable run report. It **only receives**.

```bash
rf-sentinel agent run "find bluetooth devices"                    # streams steps; Ctrl+C stops the run
rf-sentinel agent run "find bluetooth devices for 2 minutes on bladerf:1"
rf-sentinel agent run "look for zigbee on channel 25" --json
rf-sentinel agent run "find tire sensors" --gain 30               # fixed gain, no automatic gain
rf-sentinel agent run "find bluetooth devices" --mode approval-required
rf-sentinel agent run "find bluetooth devices" --detach           # print the run id and return
rf-sentinel agent status <run-id>
rf-sentinel agent stop <run-id>
rf-sentinel agent list
rf-sentinel agent approve <run-id> <action-id> --operator NAME   # or: deny ... --reason "..."
rf-sentinel agent report <run-id>
rf-sentinel agent policy
rf-sentinel agent run "find bluetooth devices" --local            # no dashboard: run in this process
```

Runs are managed by the dashboard's run service, which is the single source of truth. A run started from the dashboard can be inspected and stopped from the CLI, and the reverse. The CLI talks to `--server`, else `RF_SENTINEL_URL`, else `http://127.0.0.1:5050`. Stopping a run ends its scan, or its gain measurement or approval wait, within about 1–2 s, and releases only that run's receiver stream.

In the dashboard, type the same request into the **Ask RF Sentinel** bar. Its steps (including every gain change) appear as they happen. Decoded devices appear in the normal detections table, and a link opens the verifiable report. The dashboard and CLI use the same agent code and the same IQ service.

## What it does

1. **Understands the request.** Supported: Bluetooth Low Energy ("bluetooth", "BLE", "beacons", "AirTag"), Zigbee / 802.15.4, and TPMS tire sensors. It understands durations ("for 2 minutes", up to 600 s), a receiver ("on bladerf:1"), and a Zigbee channel. Requests to transmit, jam, spoof, or replay are refused. WiFi needs a monitor-mode adapter, not an SDR, so the agent says so.
2. **Chooses a receiver.** A free receiver whose tuning range and sample rate cover the band, from the IQ service. It uses the built-in SoapySDR service by default ([sdr.md](sdr.md)).
3. **Level-based gain.** It measures received samples on every center frequency, then steps the gain to the highest value where the 99.9th-percentile magnitude is within 3 dB of -6 dBFS and under 0.1% of samples clip. The loudest channel decides, so the gain is safe everywhere.
4. **Decoder-feedback gain.** While the decoder runs, the agent checks the decoder's own burst peaks every 8 s:
   - if more than 20% of bursts reach full scale, it lowers the gain 6 dB;
   - if bursts are weak (median peak below -18 dBFS), it raises the gain 6 dB.

   Each change restarts the receiver and is reported, with up to four changes per run.
5. **Reports.** Detections are grouped per device: count, best RSSI, identity, manufacturer, and frame type. Each run writes `run.json`, `report.html`, and `checksums.sha256` under `.demo/agent-runs/<run>` (dashboard: `<log dir>/agent-runs`). Check the files with `sha256sum -c checksums.sha256`.

Gain control is deterministic code; no model guesses gain values. Clipping is **measured** from received samples. Front-end overload is **inferred** from it.

## Operating modes

Every agent operation is authorized by the tool gateway (`rf_platform/agent/gateway.py`) in the backend. The same rules apply whether a request comes from the dashboard, the CLI, or the API.

| Mode | Read tools (receivers, runs, reports, detections) | Gain optimization, starting a scan, changing gain | Stopping its own scan |
|---|---|---|---|
| `observe` | allowed | **denied** | allowed |
| `approval-required` | allowed | **each operation waits for a named operator's approval** | allowed |
| `passive-auto` | allowed | allowed, within bounds | allowed |

- **Ceiling.** The server sets `RF_SENTINEL_AGENT_MODE` (default `passive-auto`; an unrecognized value fails closed to `observe`). A run may request the same mode or a more restrictive one. A request for a more privileged mode is capped.
- **Approvals.** Each approval is bound to one pending operation of one run, by action ID and content hash. It needs a named operator, can be used once, and expires (default 300 s). Stopping the run cancels it. A denied gain change leaves the scan running at its current gain.
- **Never available.** Transmit-capable operations (`transmit`, `page_stimulus`, `replay`, `jam`, `spoof`) and raw access (`shell`, `raw_driver`, `write_register`) are not tools. Requests for them are refused in every mode.
- **Direct API.** `POST /api/agent/tools/<tool>` runs read tools. Collection tools return `403` where the mode denies them, and `409` otherwise, because collection must go through a run so that it is bounded, owned, and audited.
- **Audit.** Every decision (allowed, denied, approval requested, approved or denied by whom, expired, or cancelled) is recorded in the run.

## Why two stages of gain

On two bladeRF 2.0 micro units at 2.4 GHz, the strongest signals were WiFi. Level-based control alone kept the WiFi peaks below clipping at about 33 dB, but BLE bursts were then weak (median peak around -25 dBFS) and decoded rarely. The decoder feedback raised the gain to 46–51 dB, where BLE advertisements decoded. That matches the best gain found by manual testing (about 50 dB).

## Safety and limits

- Receive-only, one run at a time, bounded duration. The receiver stream is released when a run ends (verified: no streams left on the service after runs).
- Bluetooth Classic needs the native sniffer binary and is not run by the agent.
- The dashboard agent is disabled in demo mode, since there are no radios.
- Decode yield depends on the RF environment and on decoder sensitivity. In a 45 s run here, it found 2–3 devices.
