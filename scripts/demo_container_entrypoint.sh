#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENT_FILE="${RF_SENTINEL_DEMO_EVENT_FILE:-${ROOT_DIR}/.demo/events/public-demo-events.jsonl}"

cd "${ROOT_DIR}"
if [[ ! -s "${EVENT_FILE}" ]]; then
  python3 scripts/generate_demo_events.py --output "${EVENT_FILE}"
fi

exec python3 -m rf_platform.ui
