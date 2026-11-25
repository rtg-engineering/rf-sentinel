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

## Measured on two bladeRF 2.0 micro units

- Sustained 4.0 of 4 MS/s and 19.9 of 20 MS/s from the receive loop, and 8.0 of 8 MS/s over the websocket.
- A planned power sweep of 2400–2490 MHz in 6 hops, and an IQ sweep hopping BLE channels 37, 38 and 39.
- The unchanged BLE plugin decoded advertisements from nearby devices, including one with a verified CRC.
- Decoding depended strongly on gain. On channel 37 here, bursts were well placed around 50 dB, and saturated with no decodes at 60 dB.

## Standalone

```bash
rf-sentinel-soapy-gateway --host 127.0.0.1 --port 8790
```

When `SDR_GATEWAY_API_TOKEN` is set, requests need a Bearer token, `X-API-Key`, or `?token=`.
