#!/usr/bin/env bash
set -euo pipefail

export PYTHON_SERVICE_PORT="${PYTHON_SERVICE_PORT:-8080}"
export DEBUG="${DEBUG:-false}"

python main.py &
PYTHON_PID=$!

cleanup() {
  kill "$PYTHON_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

node kapso-bridge/server.mjs &
NODE_PID=$!

wait -n "$PYTHON_PID" "$NODE_PID"
EXIT_CODE=$?

kill "$PYTHON_PID" "$NODE_PID" 2>/dev/null || true
wait "$PYTHON_PID" 2>/dev/null || true
wait "$NODE_PID" 2>/dev/null || true

exit "$EXIT_CODE"
