# Operator agent

Ask RF Sentinel in plain language, and it runs the collection itself. It picks a receiver, sets and corrects the gain, runs the right decoder, and reports what it found, with a verifiable run report. It **only receives**.

```bash
rf-sentinel agent run "find bluetooth devices"                    # streams steps; Ctrl+C stops the run
rf-sentinel agent run "find bluetooth devices for 2 minutes on bladerf:1"
rf-sentinel agent run "find bluetooth classic devices for 60 seconds"
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

1. **Understands the request.** Supported: Bluetooth Low Energy ("BLE", "beacons", "AirTag"), Bluetooth Classic ("bluetooth classic", "BR/EDR"), Zigbee / 802.15.4, and TPMS tire sensors. A plain "bluetooth" request covers both kinds of Bluetooth (see [Bluetooth Classic](#bluetooth-classic)). It understands durations ("for 2 minutes", up to 600 s), a receiver ("on bladerf:1"), and a Zigbee channel. Requests to transmit, jam, spoof, or replay are refused. WiFi needs a monitor-mode adapter, not an SDR, so the agent says so.
2. **Chooses a receiver.** A free receiver whose tuning range and sample rate cover the band, from the IQ service. It uses the built-in SoapySDR service by default ([sdr.md](sdr.md)).
3. **Level-based gain.** It measures received samples on every center frequency, then steps the gain to the highest value where the 99.9th-percentile magnitude is within 3 dB of -6 dBFS and under 0.1% of samples clip. The loudest channel decides, so the gain is safe everywhere.
4. **Decoder-feedback gain.** While the decoder runs, the agent checks the decoder's own burst peaks every 8 s:
   - if more than 20% of bursts reach full scale, it lowers the gain 6 dB;
   - if bursts are weak (median peak below -18 dBFS), it raises the gain 6 dB.

   Each change restarts the receiver and is reported, with up to four changes per run.
5. **Reports.** Detections are grouped per device: count, best RSSI, identity, manufacturer, and frame type. Each run writes `run.json`, `report.html`, and `checksums.sha256` under `.demo/agent-runs/<run>` (dashboard: `<log dir>/agent-runs`). Check the files with `sha256sum -c checksums.sha256`.

Gain control is deterministic code; no model guesses gain values. Clipping is **measured** from received samples. Front-end overload is **inferred** from it.

## Bluetooth Classic

Before running Classic, the agent checks that this installation can actually do it:
- the `bluetooth-classic` plugin is installed;
- its native decoder is built, or cmake is available to build it on first use;
- a free receiver covers 2.4 GHz at 20 MS/s or more.

The result is recorded in the run under `capabilities` and shown in the report.

- **"find bluetooth devices".** BLE runs on one receiver and Classic on a second one, in parallel, for the same duration. Steps from the Classic side are prefixed `[Classic]`, and devices from both are listed together, each with its `protocol`. If Classic can't run (for example, only one receiver is attached), the agent says why and runs BLE only.
- **"find bluetooth classic devices".** If Classic is unavailable, the agent reports the limitation and runs BLE instead.
- **What it captures.** 20 of the 79 Classic channels (2432–2452 MHz). Classic hops across all 79, so every active piconet appears, at about a quarter of its packet rate. Devices are keyed by LAP (the lower 24 bits of the address). A full address needs a passive FHS packet.
- **Receive-only.** The agent runs the plugin's `listen` mode, reading IQ from the IQ service. The plugin's `page-stimulus` mode transmits, and the agent never invokes it; `page_stimulus` is also on the gateway's never list.

**Measured on synthetic Classic packets** (`scripts/synth_classic_iq.py`): 300 access codes with known LAPs, at 15 dB SNR per channel, through the unchanged native decoder.

| Stream | Access codes decoded |
|---|---|
| Continuous | 58 (19%) |
| 1 sample lost every 508 | 25 (8%) |
| 4 samples lost every 508 | 8 (3%) |
| 20 samples lost every 508 | 0 |
| Continuous, 8 dB SNR | 0 |

The SoapyBladeRF overrun loses data after every 508-sample message (see [sdr.md](sdr.md)). The agent reads Classic through the IQ service's continuous native path instead.

**Decoder throughput.** At 20 MHz the native decoder runs at about 0.7× real time. That was measured both offline and live, where it processed 68% of a 30 s stream. The IQ service skips ahead when a reader falls behind, so about a third of the stream is not examined. Every run reports this as `realtime_coverage` under the Classic decoder stats, and a step says so when it is below 95%. Narrower bands use a slower filter-bank path, so they don't help.

The decoder is also the next limit on sensitivity: it samples each symbol once, at a fixed timing phase. A Python emulation of the same pipeline that tries every timing phase recovered about three quarters of the packets.

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
- Bluetooth Classic runs only when the capability check passes. Live Classic decoding has not yet been validated against known Classic traffic here; the numbers above are from synthetic packets.
- The dashboard agent is disabled in demo mode, since there are no radios.
- Decode yield depends on the RF environment and on decoder sensitivity. In a 45 s run here, it found 2–3 devices.
