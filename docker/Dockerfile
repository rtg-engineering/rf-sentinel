# Build from the repo root: docker build -f docker/Dockerfile .
# BASE=ubuntu:24.04 (Python 3.12) for the dashboard: the model planner (pydantic-ai) needs Python 3.11+.
# The IQ service image can stay on 22.04; the dashboard reaches receivers only over HTTP.
ARG BASE=ubuntu:22.04
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# build-essential+cmake: bluetooth-classic plugin is a CMake/C++ build.
# tshark: wifi-80211 plugin depends on pyshark, which shells out to tshark.
# wireshark-common's postinst asks (via debconf) whether non-root users can
# capture packets; we run as root, so decline non-interactively.
RUN echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections \
    && apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python3-dev \
      cmake build-essential pkg-config \
      libboost-program-options-dev libboost-system-dev libboost-thread-dev \
      libfftw3-dev \
      libbladerf2 bladerf libusb-1.0-0 \
      python3-soapysdr soapysdr-module-bladerf \
      tshark \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# python3-soapysdr: device discovery and non-bladeRF receivers for the built-in IQ service
# (python -m rf_platform.soapy_gateway.server); bladeRF streaming goes through libbladeRF directly.
# SoapySDR C++ headers/lib + a source-built libbladeRF.so.2: same story as
# the passive-shield station1 container. apt has no SoapySDR dev package for
# this arch/repo (headers are custom-built on-device), and apt's libbladerf2
# (0.2021.10-2, Dec 2021) cannot properly drive these radios after the
# firmware/FPGA update already applied to the station1 hardware - confirmed
# separately (readStream() capped at ~508 samples instead of 65536, chronic
# RX FIFO overflow). Overwrite it at its own apt-owned path so there's no
# ld.so.cache ambiguity between /usr/local/lib and /usr/lib/aarch64-linux-gnu.
COPY docker/soapy-build/include/SoapySDR /usr/local/include/SoapySDR
COPY docker/soapy-build/SoapySDR.pc /usr/local/lib/pkgconfig/SoapySDR.pc
COPY docker/soapy-build/libSoapySDR.so.0.8-3 /usr/local/lib/libSoapySDR.so.0.8-3
COPY docker/soapy-build/libbladeRFSupport.so /usr/local/lib/SoapySDR/modules0.8-3/libbladeRFSupport.so
COPY docker/soapy-build/libbladeRF.so.2 /usr/lib/aarch64-linux-gnu/libbladeRF.so.2
RUN ln -sf /usr/local/lib/libSoapySDR.so.0.8-3 /usr/local/lib/libSoapySDR.so.0.8 \
    && ln -sf /usr/local/lib/libSoapySDR.so.0.8-3 /usr/local/lib/libSoapySDR.so \
    && ldconfig

COPY rf_platform/ /opt/rf-sentinel/rf_platform/
COPY ui/ /opt/rf-sentinel/ui/
COPY requirements.txt pyproject.toml install.sh /opt/rf-sentinel/

WORKDIR /opt/rf-sentinel
RUN chmod +x install.sh && ./install.sh
# Model planner/analyst (pydantic-ai): installed on Python 3.11+, skipped by its own marker on 3.10.
RUN /opt/rf-sentinel/.venv/bin/python -m pip install --no-cache-dir -e "/opt/rf-sentinel[analyst-llm]"
RUN mkdir -p /opt/rf-sentinel/ui/backend/data /var/log/rf_sentinel

ENV RF_SENTINEL_HOST=0.0.0.0 \
    RF_SENTINEL_PORT=5050 \
    RF_SENTINEL_TEXTUAL_CONSOLE=0 \
    SDR_GATEWAY_BASE_URL=http://127.0.0.1:8080 \
    PATH=/opt/rf-sentinel/.venv/bin:$PATH \
    # Default the BTC/BLE combined scanner to rfiq_daemon instead of
    # sdr-gateway - talks to the same daemon sdr-shark uses by default now,
    # via unix sockets under /tmp (needs -v /tmp:/tmp at deploy time, not
    # USB passthrough). Other protocol plugins (zigbee/tpms/fm/wifi) still
    # go through sdr-gateway regardless of this setting - see docker/README.md.
    SDR_BACKEND=rfiq \
    SDR_RFIQ_SOCKET=/tmp/rfiq0.sock \
    SDR_RFIQ_CONTROL_SOCKET=/tmp/rfiq0-control.sock

EXPOSE 5050

CMD ["/opt/rf-sentinel/.venv/bin/python3", "-m", "rf_platform.ui"]
