#!/usr/bin/env bash
set -euo pipefail

# VPS/Docker generic entrypoint for FastAPI + Node Bridge
# Usage: docker run -p 3001:3001 -p 8000:8000 -e KAPSO_INTERNAL_TOKEN=xxx <image>

export PYTHON_SERVICE_PORT="${PYTHON_SERVICE_PORT:-8000}"
export NODE_BRIDGE_PORT="${NODE_BRIDGE_PORT:-3001}"
export INTERNAL_AGENT_API_URL="${INTERNAL_AGENT_API_URL:-http://127.0.0.1:${PYTHON_SERVICE_PORT}/api/v1/kapso/inbound}"
export DEBUG="${DEBUG:-false}"

# Health check function for Python service
wait_for_python() {
    local max_attempts=30
    local attempt=1
    while [ $attempt -le $max_attempts ]; do
        if curl -sf "http://127.0.0.1:${PYTHON_SERVICE_PORT}/health" > /dev/null 2>&1 || \
           curl -sf "http://127.0.0.1:${PYTHON_SERVICE_PORT}/openapi.json" > /dev/null 2>&1; then
            echo "[Entrypoint] Python service ready on port ${PYTHON_SERVICE_PORT}"
            return 0
        fi
        echo "[Entrypoint] Waiting for Python service... ($attempt/$max_attempts)"
        sleep 1
        attempt=$((attempt + 1))
    done
    echo "[Entrypoint] WARNING: Python service may not be ready"
    return 1
}

echo "[Entrypoint] Starting services..."
echo "[Entrypoint] Python service will listen on: 0.0.0.0:${PYTHON_SERVICE_PORT}"
echo "[Entrypoint] Node bridge will listen on: 0.0.0.0:${NODE_BRIDGE_PORT}"
echo "[Entrypoint] Internal API: ${INTERNAL_AGENT_API_URL}"

# Start Python FastAPI in background
python main.py &
PYTHON_PID=$!

# Optional: wait for Python to be healthy before starting Node
if [ "${WAIT_FOR_PYTHON:-true}" = "true" ]; then
    wait_for_python || true
fi

# Start Node Bridge in background
PORT="${NODE_BRIDGE_PORT}" node kapso-bridge/server.mjs &
NODE_PID=$!

cleanup() {
    echo "[Entrypoint] Shutting down services..."
    kill "$PYTHON_PID" 2>/dev/null || true
    kill "$NODE_PID" 2>/dev/null || true
    wait "$PYTHON_PID" 2>/dev/null || true
    wait "$NODE_PID" 2>/dev/null || true
    echo "[Entrypoint] Services stopped"
}

trap cleanup EXIT INT TERM

echo "[Entrypoint] Both services running. Waiting for termination..."

# Wait for any process to exit
wait -n "$PYTHON_PID" "$NODE_PID"
EXIT_CODE=$?

echo "[Entrypoint] One process exited with code $EXIT_CODE, shutting down..."
exit "$EXIT_CODE"
