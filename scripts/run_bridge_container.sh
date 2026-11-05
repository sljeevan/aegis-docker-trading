#!/bin/bash
# ==============================================================================
# Aegis Alpha — Container MT5 Bridge Runner
# Executes inside container under Wine 64-bit and connects to local MT5 terminal
# ==============================================================================

set -e

TERMINAL_PATH="${MT5_TERMINAL_PATH:-/app/data/terminal/terminal64.exe}"
ACCOUNT_PREFIX="${ACCOUNT_PREFIX:-XM_DEMO}"
BRIDGE_PORT="${BRIDGE_PORT:-8888}"

echo "================================================================="
echo " Starting Aegis MT5 Bridge Server in Container..."
echo " Profile Prefix : ${ACCOUNT_PREFIX}"
echo " Bridge Port    : ${BRIDGE_PORT}"
echo " Terminal Path  : ${TERMINAL_PATH}"
echo "================================================================="

# Wait for Xvfb display :99 to become ready
echo "Waiting for Xvfb display :99..."
for i in {1..30}; do
    if [ -S /tmp/.X11-unix/X99 ]; then
        echo "✅ Xvfb display :99 is online."
        break
    fi
    sleep 1
done

# Check if MT5 terminal executable exists in volume
if [ ! -f "${TERMINAL_PATH}" ]; then
    echo "⚠️  MT5 executable not found at: ${TERMINAL_PATH}"
    echo "ℹ️  Please place or mount your MT5 terminal files (terminal64.exe) in the terminal volume."
    echo "   Sleeping 15s before checking again..."
    sleep 15
    exit 1
fi

# Convert Linux path to Wine Windows path
WINE_TERMINAL_PATH="Z:${TERMINAL_PATH//\//\\}"

echo "Launching MT5 Bridge Server under Wine..."
cd /app/src
exec wine C:\\python64\\python.exe -u -m gold_trader.bridge_server \
    --host 127.0.0.1 \
    --port "${BRIDGE_PORT}" \
    --path "${WINE_TERMINAL_PATH}" \
    --prefix "${ACCOUNT_PREFIX}"
