# RF Sentinel Docker image

Containerizes the RF Sentinel dashboard (`ui/backend/app.py`) plus all
protocol plugins, so it can be deployed to a station (e.g. PASSIVE-SHIELD
station1, `10.139.1.160`) without depending on whatever's installed
natively on that host's Python.

The image runs the project's own `install.sh` at build time (creates
`.venv`, `pip install -e` for the root package and every
`rf_platform/plugins/*`, compiles the Bluetooth Classic C++ plugin via
CMake) rather than hand-rolling a subset — this is a full parity build,
not a stripped-down one.

## Build

From the repo root (context matters — Dockerfile references paths
relative to root):

```
docker build -f docker/Dockerfile -t rf-sentinel:latest .
```

## Deploy (agent missions: built-in IQ service)

The agent (missions, discovery, `/agent`, `/fleet`) uses RF Sentinel's own IQ
service, which owns the receivers outright. Stop anything else holding them
first (`rfiq-daemon@radioN`, a PASSIVE-SHIELD detector container): a bladeRF
opened by two processes can wedge until it is replugged.

```
mkdir -p ~/.config/rf-sentinel ~/rf-sentinel-data
head -c 24 /dev/urandom | base64 | tr -d '/+=' > ~/.config/rf-sentinel/iq_token
T=$(cat ~/.config/rf-sentinel/iq_token)

docker run -d --name rf-sentinel-iq --restart unless-stopped --network host \
  --privileged -v /dev/bus/usb:/dev/bus/usb \
  -e SDR_GATEWAY_API_TOKEN=$T \
  rf-sentinel:latest python3 -m rf_platform.soapy_gateway.server --host 127.0.0.1 --port 8790

docker run -d --name rf-sentinel --restart unless-stopped --network host \
  -v ~/rf-sentinel-data:/var/log/rf_sentinel \
  -e RF_SENTINEL_TEXTUAL_CONSOLE=0 -e SDR_BACKEND=soapy \
  -e SDR_GATEWAY_BASE_URL=http://127.0.0.1:8790 -e SDR_GATEWAY_API_TOKEN=$T \
  rf-sentinel:latest

# Sign-in (the dashboard requires it when bound to 0.0.0.0); the token is shown once.
docker exec rf-sentinel rf-sentinel agent operators add NAME --role approver --log-dir /var/log/rf_sentinel
```

Cell surveys use the Cellular Survey service on `127.0.0.1:5098` (its own
container, holding the modem). The image runs Python 3.10 (Ubuntu 22.04), so
code must not use 3.11-only names such as `datetime.UTC`.

## Deploy (older production config: `rfiq`)

This is what's actually running on station1 and dev-desktop today —
shares the radio with sdr-shark and the shared `bt-detector` sidecar
through `rfiq_daemon` (see `rf-iq-gateway`) instead of opening SoapySDR
directly:

```
docker run -d \
  --name rf-sentinel \
  --restart unless-stopped \
  --network host \
  -v /tmp:/tmp \
  -e RF_SENTINEL_HOST=0.0.0.0 \
  -e RF_SENTINEL_PORT=5050 \
  -e RF_SENTINEL_TEXTUAL_CONSOLE=0 \
  -e SDR_BACKEND=rfiq \
  -e SDR_RFIQ_SOCKET=/tmp/rfiq0.sock \
  -e SDR_RFIQ_CONTROL_SOCKET=/tmp/rfiq0-control.sock \
  -e SDR_GATEWAY_BASE_URL=http://127.0.0.1:8080 \
  rf-sentinel:latest
```

`SDR_GATEWAY_BASE_URL` is still set out of habit/parity with the
original design, but **see "Known gap" below** - `sdr-gateway` isn't
actually running on either host right now, and most of the app doesn't
need it.

## Deploy (older: direct SoapySDR via `sdr-gateway`)

The original design point, before `rfiq_daemon` existed - the dashboard
talks to a separate `sdr-gateway` HTTP service (not part of this repo)
for device access instead of a direct SoapySDR caller. Not what's
actually deployed anywhere currently; kept here for reference in case
`sdr-gateway` comes back into the picture:

```
docker run -d \
  --name rf-sentinel \
  --restart unless-stopped \
  --network host \
  -v /dev/bus/usb:/dev/bus/usb \
  --device-cgroup-rule="c 189:* rmw" \
  --group-add plugdev \
  -v rf-sentinel-data:/opt/rf-sentinel/ui/backend/data \
  -e SDR_GATEWAY_BASE_URL=http://127.0.0.1:8080 \
  rf-sentinel:latest
```

`--network host` is what lets it reach `sdr-gateway` at
`127.0.0.1:8080`. The Bluetooth Classic C++ binary is the exception in
either mode — it does open SoapySDR directly (see below), not through
either the gateway or `rfiq_daemon`.

## Known gap: the device picker still needs `sdr-gateway`

`ui/backend/app.py`'s `_available_devices()` (feeds the UI's device
picker, and `/api/devices`) unconditionally calls `_fetch_gateway_devices()`
against `SDR_GATEWAY_BASE_URL` — there's no `rfiq_daemon`-native fallback.
With `sdr-gateway` not running (the normal state on both station1 and
dev-desktop today), the UI shows **"no SDRs are available from
sdr-gateway"** even though the actual detection pipeline (the shared
`bt-detector` sidecar, and this container's own `discovery_table`
polling of it - see the root README) works fine independent of any of
this, since that path talks to `rfiq_daemon` directly and never calls
`_available_devices()`.

Similarly, `rf_sentinel_scan` (the binary `_start_rf_sentinel_engine()`
launches when you click "start scan" in the UI) has no
`--iq-source`/`--rfiq-socket` flags at all — unlike the Bluetooth Classic
plugin's own scanner binary (`bluetooth_scanner`, used by the shared
`bt-detector`), which does support talking to `rfiq_daemon` directly.
Starting a scan from the UI still assumes direct BladeRF access via
SoapySDR, which will conflict with `rfiq_daemon` already holding the
radio in the `rfiq` deploy mode above.

**Net effect:** the always-on shared-detector view (BT/BLE cards fed by
`bt-detector`) works today without `sdr-gateway`. The UI's own
"start scan" button and device picker do not, and fixing that properly
needs either standing `sdr-gateway` back up, or adding real
`rfiq_daemon` socket support to `rf_sentinel_scan` and an
`rfiq`-native path in `_available_devices()` (bigger change, not yet
done as of this writing).

## Why `docker/soapy-build/`

Same finding as `passive-shield/station1-docker/`, reused here because
the Bluetooth Classic plugin (`rf_platform/plugins/bluetooth-classic`,
`CMakeLists.txt`) links directly against SoapySDR + a source-built
`libbladeRF.so.2`:

- apt has no SoapySDR dev package for this arch/repo; headers are
  custom-built on-device at `~/soapy/SoapySDR`.
- apt's `libbladerf2` (0.2021.10-2, Dec 2021) cannot properly drive
  station1's radios after their firmware/FPGA update (confirmed
  separately: `readStream()` capped at ~508 samples/call instead of
  65536, chronic RX FIFO overflow, zero real detections despite USB3
  SuperSpeed enumerating fine). The known-good build must overwrite the
  apt copy **at its own path**
  (`/usr/lib/aarch64-linux-gnu/libbladeRF.so.2`) — `ld.so.cache` kept
  preferring that directory over `/usr/local/lib` regardless of a
  higher-priority copy being placed there too.

## Container-specific build gotchas already worked through

- `python3-dev` is required (`Python.h`) — `zigbee-802154` has a C
  extension (`_cdecode.c`).
- `libboost-program-options-dev`, `libboost-system-dev`,
  `libboost-thread-dev`, `libfftw3-dev` — Bluetooth Classic's CMake
  build needs Boost ≥1.46 and `fftw3f`.
- `tshark` — `wifi-80211` depends on `pyshark`, which shells out to it.
  `wireshark-common`'s postinst prompts (via debconf) about non-root
  packet capture; preseeded to decline non-interactively since we run
  as root.
- Plain `pip install .` against the root `pyproject.toml` silently
  built an unnamed `UNKNOWN-0.0.0` package with no `console_scripts`
  (some metadata-parsing issue with the pip/setuptools version here,
  possibly the PEP 639 `license = "..."` string) — running the real
  `install.sh` (which creates a proper venv with an upgraded pip first)
  avoided this; a hand-written entry point isn't needed with the full
  build.

## Verified working

Deployed to station1 and run with all protocols enabled
(`mode: sentinel`) — confirmed real decoded output end-to-end (FM
broadcast stations with plausible pilot-tone/RDS metrics; both
BladeRFs actively in use, one dedicated to BTC/BLE, the other
hopping Zigbee/TPMS/FM).
