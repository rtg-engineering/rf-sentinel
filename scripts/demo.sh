#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENT_FILE="${ROOT_DIR}/.demo/events/public-demo-events.jsonl"

cd "${ROOT_DIR}"
python3 scripts/generate_demo_events.py --output "${EVENT_FILE}" >/dev/null

echo "RF Sentinel public demo event file: ${EVENT_FILE}"
echo "Open http://127.0.0.1:${RF_SENTINEL_DEMO_PORT:-8080}"
docker compose -f docker-compose.demo.yml up --build
