# Receivers: built-in SoapySDR IQ service

By default, RF Sentinel reads its radios through **SoapySDR**, so any SoapySDR-supported receiver works without driver-specific code or a separate gateway. Examples include bladeRF, HackRF, RTL-SDR, Airspy, SDRplay, LimeSDR, USRP and PlutoSDR.

When the dashboard starts outside demo mode, it launches a small built-in IQ service on `127.0.0.1:8790` and points the receiver plugins at it. The service speaks the subset of the `sdr-gateway` HTTP and websocket API that the plugins already use, so every plugin works unchanged:
- devices, IQ streams with retuning, power sweeps, and IQ sweeps;
- interleaved signed 8-bit I/Q, the framing every plugin decodes.

## Choosing the backend

| `SDR_BACKEND` | Behaviour |
|---|---|
| `soapy` (default) | Start the built-in SoapySDR service and use it. If `SDR_GATEWAY_BASE_URL` is set explicitly, that gateway is used instead. |
| `gateway` | Use an external `sdr-gateway` at `SDR_GATEWAY_BASE_URL`. |
| `rfiq` | Station deployments: the BT/BLE combined scanner reads `rfiq_daemon` sockets. |

`/api/status` reports the active backend and the receivers it found under `sdr_backend`. If SoapySDR cannot be loaded, the dashboard still starts and reports why.

## Requirements

SoapySDR with Python bindings (`python3-soapysdr`), plus the module for your radio (for example `soapysdr-module-bladerf` or `soapysdr-module-hackrf`). `SoapySDRUtil --find` should list the radio. Inside a virtualenv, RF Sentinel falls back to the system's SoapySDR bindings.

## Behaviour

- **Devices** are listed as `<driver>:<index>` (`bladerf:0`, `hackrf:0`). Tuning and sample-rate ranges are queried from the driver. Sound cards exposed by SoapyAudio are excluded.
- **One owner per device.** A second stream on a busy device is refused (`409`) unless the request sets `replace_existing`.
- **Gain.** Automatic gain control in the radio is switched off, so requested gains are applied exactly. LNA/VGA map onto named stages where they exist (HackRF, Airspy, SDRplay); elsewhere, the sum sets the aggregate gain (bladeRF 2.0 `full`, from -15 to 60 dB).
- **Scaling.** The driver's full scale maps to ±127. Choose the gain so peaks stay just below clipping.
- **Open devices are reused** between streams. Opening a bladeRF takes several seconds; after the first open, a new stream delivers samples within about a second.

- **Receiver health is reported per stream.** `GET /streams` includes `receiver.driver_path` (`libbladerf` or `soapysdr`), `receiver.samples_received`, and `receiver.overflows`. Overflows are driver-reported overruns, so they count samples that were lost.
- **A failed receiver releases its device.** If a stream stops delivering samples for 5 s (a USB error or an unplugged radio), it fails with the driver error, its handle is closed, and it no longer owns the device. The next stream reopens the radio without needing `replace_existing`.

## bladeRF: native receive path

bladeRF radios are read through **libbladeRF directly**, using its plain (non-metadata) sync interface, instead of through SoapyBladeRF. Every other radio uses SoapySDR.

**Why.** SoapyBladeRF always streams in timestamp-metadata mode. With bladeRF 2.0 FPGA/firmware newer than the installed libbladeRF knows about, that mode reports an overrun after every 508-sample message. At 4 MS/s that is a gap every ~127 µs, and the stream silently loses samples.

The combination measured here was:

| Component | Version |
|---|---|
| FPGA | v0.16.0 |
| Firmware | v2.6.0 |
| libbladeRF | 2.5.0 |
| SoapyBladeRF | 0.4.1 |

libbladeRF itself warns at open that the FPGA and firmware are "newer than entries in libbladeRF's compatibility table". Changing buffer settings made no difference. The plain sync interface delivers a continuous stream on the same hardware.

**What it changed.** Most BLE packets (~250–380 µs) crossed a gap. On the gapped stream, about 1% of synchronised packets passed CRC. On the native stream, 77–93% did.

**Requirements.**
- `libbladeRF.so.2`, which is installed with the `bladerf` packages that SoapyBladeRF already needs.
- Gain semantics are unchanged: the same requested gain gave the same measured noise floor on both paths.

**Switching back.** Set `RF_SENTINEL_BLADERF_NATIVE=0` to use SoapyBladeRF instead, for example with a libbladeRF build that matches your FPGA. Compare `receiver.overflows` before relying on it.

## Measuring decoder quality

Decoder changes are evaluated on recorded IQ, so before and after see identical samples:

```bash
# 20 s of channel 37 at a fixed gain -> .cs8 + .json (sha256, levels)
python3 scripts/record_iq.py --device bladerf:0 --freq-hz 2402000000 --rate 4000000 --gain 48 \
    --seconds 20 --out .demo/iq/ble-ch37.cs8
# A/B a git ref against the working tree on the same files
python3 scripts/eval_ble_decoder.py .demo/iq/ble-ch3*.cs8 --baseline-ref origin/main
# Negative controls: every recording against all 40 whitening sequences
python3 scripts/eval_ble_false_positives.py .demo/iq/*.cs8
```

Both evaluators run the decoder on the recording's own timeline. The false-positive evaluator reports a 95% upper bound on the false-positive rate (rule of three), not a claim of zero.

## Measured on two bladeRF 2.0 micro units

- Sustained 4.0 of 4 MS/s and 19.9 of 20 MS/s from the receive loop, and 8.0 of 8 MS/s over the websocket.
- A planned power sweep of 2400–2490 MHz in 6 hops, and an IQ sweep hopping BLE channels 37, 38 and 39.
- BLE advertising, 20 s per channel on the same recordings:
  - the previous decoder found 194 CRC-valid detections and 34 devices;
  - the current decoder found 558 detections and 40 devices;
  - nothing was lost and there were no false positives.
- A live 60 s agent run found 12 devices and 272 detections.
- **False positives.** Each recording was decoded under all 40 BLE whitening sequences: 11 recordings and 157,641 sync candidates that could only be false. **None passed CRC**, so the 95% upper bound is 1.9 × 10⁻⁵ per candidate. The recordings were:
  - channels 37/38/39;
  - channel 37 at 30, 40, 50 and 60 dB;
  - 915 MHz ISM, 1575 MHz, 5745 MHz WiFi, and 2437 MHz in-band WiFi.

  In the non-BLE bands, sync alone matched 80 times at 1575 MHz and 40 times at 915 MHz. The CRC rejected all of them.
- Channel 37 over 10 s gave 71, 92, 98 and 91 detections at 30, 40, 50 and 60 dB.
- Gain matters. At 2.4 GHz WiFi dominates the peaks, and BLE decoded best around 46–50 dB. The agent's automatic gain reaches that range from decoder feedback.

## Standalone

```bash
rf-sentinel-soapy-gateway --host 127.0.0.1 --port 8790
```

When `SDR_GATEWAY_API_TOKEN` is set, requests need a Bearer token, `X-API-Key`, or `?token=`.
