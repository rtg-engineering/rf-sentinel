# Operator agent

Ask RF Sentinel in plain language, and it runs the collection itself. It picks a receiver, sets and corrects the gain, runs the right decoder, and reports what it found, with a verifiable run report. It **only receives**.

```bash
python3 -m rf_platform.agent "find bluetooth devices"
python3 -m rf_platform.agent "find bluetooth devices for 2 minutes on bladerf:1"
python3 -m rf_platform.agent "look for zigbee on channel 25" --json
python3 -m rf_platform.agent "find tire sensors" --gain 30        # fixed gain, no automatic gain
python3 -m rf_platform.agent list                                 # past runs
python3 -m rf_platform.agent report RUN-xxxxxxxxxxxx              # where a run's report is, and how to verify it
```

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

## Why two stages of gain

On two bladeRF 2.0 micro units at 2.4 GHz, the strongest signals were WiFi. Level-based control alone kept the WiFi peaks below clipping at about 33 dB, but BLE bursts were then weak (median peak around -25 dBFS) and decoded rarely. The decoder feedback raised the gain to 46–51 dB, where BLE advertisements decoded. That matches the best gain found by manual testing (about 50 dB).

## Safety and limits

- Receive-only, one task at a time, bounded duration. The receiver stream is released when a run ends (verified: no streams left on the service after runs).
- Bluetooth Classic needs the native sniffer binary and is not run by the agent.
- The dashboard agent is disabled in demo mode, since there are no radios.
- Decode yield depends on the RF environment and on decoder sensitivity. In a 45 s run here, it found 2–3 devices.
