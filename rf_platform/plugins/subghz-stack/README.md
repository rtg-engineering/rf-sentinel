# SubGHz Stack

CLI-first Sub-GHz protocol analyzer built on `sdr-gateway`.

## What it does

- Starts an SDR IQ stream from `sdr-gateway`
- Defaults to the first HackRF device when available
- Monitors TPMS-style OOK activity and wideband Sub-GHz bins
- Prints decoded bits, hex, burst timing, and confidence
- Can save raw burst captures for later analysis
- Exposes rolling JSON stats/events for integration with other apps

## Requirements

- Python 3.10+
- `sdr-gateway` running locally or on a reachable host
- `hackrf-tools` on the SDR host if you want the default HackRF path

## Setup

```bash
cd rf-sentinel  # the repository root
source .venv/bin/activate
pip install -e rf_platform/plugins/subghz-stack
```

If your gateway uses auth:

```bash
export SDR_GATEWAY_API_TOKEN="<token>"
```

If your gateway is not local:

```bash
export SDR_GATEWAY_BASE_URL="http://127.0.0.1:8080"
```

## Usage

List SDR devices:

```bash
subghz-stack devices
```

Start live decode on the default HackRF path:

```bash
subghz-stack listen
```

Tune a different band:

```bash
subghz-stack listen --center-freq-hz 433920000
```

Save burst captures:

```bash
subghz-stack listen --save-dir captures/tpms
```

Emit JSON lines:

```bash
subghz-stack listen --json
```

Try the initial LoRa detector on a fixed center:

```bash
subghz-stack monitor --protocol lora --center-freq-hz 915000000
```

Wideband adaptive hunt across `315` and `433`:

```bash
subghz-stack wideband-monitor --auto-hunt-known-bands --sample-rate-sps 4000000 --bin-width-hz 200000 --channel-rate-sps 1000000
```

Wideband LoRa hunt across `902-928 MHz` with automatic sub-window retunes:

```bash
subghz-stack wideband-monitor --protocol lora --auto-hunt-known-bands --sample-rate-sps 4000000 --bin-width-hz 250000 --channel-rate-sps 500000
```

Mixed Sub-GHz hunt across `315 TPMS`, `433 TPMS`, and `915 LoRa`:

```bash
subghz-stack wideband-monitor --auto-hunt-all-known-bands --band-dwell-s 2 --sample-rate-sps 4000000 --bin-width-hz 250000 --channel-rate-sps 500000
```

Custom multi-band plan:

```bash
subghz-stack wideband-monitor \
  --band-spec 315:314800000:315300000:tpms \
  --band-spec 433:433700000:434400000:tpms \
  --band-spec 915:902000000:928000000:lora
```

## Notes

- `tpms-stack`, `tpms_stack`, `subghz-stack`, and `subghz_stack` are CLI aliases for the same plugin.
- TPMS is the most mature protocol path today.
- TPMS family parsing has started with initial Schrader-style decoders (`schrader-gg4`, `schrader-eg53ma4`) layered on top of the generic burst detector.
- LoRa support is started as an initial chirp detector / fingerprinting path and is not yet a full payload demodulator.
