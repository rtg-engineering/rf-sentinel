#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
if [[ "${VENV_DIR}" != /* ]]; then
  VENV_DIR="${ROOT_DIR}/${VENV_DIR}"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
BTC_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/bluetooth-classic"
BTC_BUILD_DIR="${BTC_PLUGIN_DIR}/build"
BLE_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/bluetooth-lowenergy"
ZIGBEE_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/zigbee-802154"
WIFI_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/wifi-80211"
SUBGHZ_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/subghz-stack"
FM_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/fm-broadcast"
AM_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/am-broadcast"
CELLULAR_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/cellular-awareness"
DRONE_PLUGIN_DIR="${ROOT_DIR}/rf_platform/plugins/drone-rf"

venv_is_stale() {
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    return 0
  fi

  local activate_file="${VENV_DIR}/bin/activate"
  if [[ -f "${activate_file}" ]] && ! grep -Fq "VIRTUAL_ENV=${VENV_DIR}" "${activate_file}"; then
    return 0
  fi

  local cfg_file="${VENV_DIR}/pyvenv.cfg"
  if [[ -f "${cfg_file}" ]] && grep -Eq "BluetoothExplorer|/[^ ]+/.venv" "${cfg_file}" && ! grep -Fq "${VENV_DIR}" "${cfg_file}"; then
    return 0
  fi

  return 1
}

echo "[RF Sentinel] root: ${ROOT_DIR}"
echo "[RF Sentinel] host arch: $(uname -m)"

if ! command -v cmake >/dev/null 2>&1; then
  echo "error: cmake is required to build rf_platform/plugins/bluetooth-classic" >&2
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "error: ${PYTHON_BIN} not found" >&2
  exit 1
fi

if [[ -d "${VENV_DIR}" ]] && venv_is_stale; then
  echo "[RF Sentinel] stale or moved venv detected; recreating: ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[RF Sentinel] creating venv: ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "[RF Sentinel] installing Python requirements"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"
"${VENV_DIR}/bin/python" -m pip install -e "${ROOT_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${BTC_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${BLE_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${ZIGBEE_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${WIFI_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${SUBGHZ_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${FM_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${AM_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${CELLULAR_PLUGIN_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${DRONE_PLUGIN_DIR}"

echo "[RF Sentinel] verifying CLI entry points"
for cli in rf_sentinel_scan rf_sentinel_pipeline rf_sentinel_ui bluetooth_classic ble_scanner zigbee_802154 wifi_80211 tpms_stack fm_broadcast lowfreq-scan cellular_scanner; do
  if [[ ! -x "${VENV_DIR}/bin/${cli}" ]]; then
    echo "error: expected CLI missing or not executable: ${VENV_DIR}/bin/${cli}" >&2
    exit 1
  fi
done

echo "[RF Sentinel] rebuilding Bluetooth Classic plugin for native arch"
rm -rf "${BTC_BUILD_DIR}"
cmake -S "${BTC_PLUGIN_DIR}" -B "${BTC_BUILD_DIR}"
cmake --build "${BTC_BUILD_DIR}" --parallel "${BTC_BUILD_JOBS:-$(nproc 2>/dev/null || echo 2)}"

BTC_BINARY="${BTC_BUILD_DIR}/btcexplorer-sniffer"
if [[ ! -x "${BTC_BINARY}" ]]; then
  echo "error: expected binary missing or not executable: ${BTC_BINARY}" >&2
  exit 1
fi

if command -v file >/dev/null 2>&1; then
  echo "[RF Sentinel] built binary: $(file -b "${BTC_BINARY}")"
fi

echo
echo "Install complete."
echo "Run:"
echo "  source \"${VENV_DIR}/bin/activate\""
echo "  rf_sentinel_scan"
echo "  rf_sentinel_ui"
