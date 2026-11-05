#!/bin/bash
# ==============================================================================
# Aegis Alpha — Container Auto-Trader Runner
# Launches the Python AutoTrader daemon according to injected environment variables
# ==============================================================================

set -e

PYTHON_BIN="/opt/aegis_venv/bin/python"
BRIDGE_PORT="${BRIDGE_PORT:-8888}"
DB_PATH="${DB_PATH:-/app/data/trading_data/auto_trader.db}"

echo "================================================================="
echo " Starting Aegis Auto-Trader Daemon in Container..."
echo " Symbol         : ${SYMBOL:-XAUUSD}"
echo " Mode           : ${STRATEGY_MODE:-FLAT_RISK_TOUCH}"
echo " Database       : ${DB_PATH}"
echo "================================================================="

# Wait for MT5 bridge socket to come online
echo "Waiting for MT5 bridge server on 127.0.0.1:${BRIDGE_PORT}..."
for i in {1..60}; do
    if python3 -c "import socket; s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', int('${BRIDGE_PORT}'))); s.close()" 2>/dev/null; then
        echo "✅ MT5 bridge server is accepting socket connections!"
        break
    fi
    sleep 2
done

if ! python3 -c "import socket; s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', int('${BRIDGE_PORT}'))); s.close()" 2>/dev/null; then
    echo "❌ Timeout waiting for MT5 bridge server on 127.0.0.1:${BRIDGE_PORT}."
    exit 1
fi

# Build arguments array
ARGS=(
    "-u"
    "-m" "gold_trader.scripts.run_auto_trader"
    "--bridge-port" "${BRIDGE_PORT}"
    "--symbol" "${SYMBOL:-XAUUSD}"
    "--mode" "${STRATEGY_MODE:-FLAT_RISK_TOUCH}"
    "--timeframe" "${TIMEFRAME:-1h}"
    "--min-rr" "${MIN_RR:-1.25}"
    "--scale-out-r" "${SCALE_OUT_R:-1.25}"
    "--runner-trail-buffer" "${RUNNER_TRAIL_BUFFER:-4.50}"
    "--runner-target-2-multiplier" "${RUNNER_TARGET_2_MULTIPLIER:-3.00}"
    "--max-positions" "${MAX_POSITIONS:-2}"
    "--max-unhedged-positions" "${MAX_UNHEDGED_POSITIONS:-1}"
    "--loss-cooldown-minutes" "${LOSS_COOLDOWN_MINUTES:-60}"
    "--max-daily-trades" "${MAX_DAILY_TRADES:-6}"
    "--order-tag" "${ORDER_TAG:-DockerNode}"
    "--db-path" "${DB_PATH}"
    "--interval-seconds" "${SCAN_INTERVAL_SECONDS:-60}"
    "--active-interval-seconds" "${ACTIVE_INTERVAL_SECONDS:-5}"
    "--loop"
)

# Sizing & Risk Model
if [ "${RISK_MODE}" = "PERCENT" ]; then
    ARGS+=( "--risk-pct" "${RISK_PCT:-0.40}" )
else
    ARGS+=( "--flat-risk" "--fixed-risk-usd" "${FIXED_RISK_USD:-75.44}" )
fi

# Runner compounding
if [ "${ALLOW_RUNNER_COMPOUNDING:-true}" = "true" ]; then
    ARGS+=( "--allow-runner-compounding" )
fi

# Slope alignment
if [ "${REQUIRE_SLOPE_ALIGNMENT:-true}" = "true" ]; then
    ARGS+=( "--require-slope-alignment" )
fi

# One trade per bar
if [ "${ONE_TRADE_PER_BAR:-true}" = "true" ]; then
    ARGS+=( "--one-trade-per-bar" )
fi

# Dynamic runner trail buffer R
if [ -n "${RUNNER_TRAIL_BUFFER_R}" ]; then
    ARGS+=( "--runner-trail-buffer-r" "${RUNNER_TRAIL_BUFFER_R}" )
fi

# ADX threshold
if [ -n "${ADX_MIN_THRESHOLD}" ]; then
    ARGS+=( "--adx-min-threshold" "${ADX_MIN_THRESHOLD}" )
fi

# ML Filter
if [ "${ENABLE_ML_FILTER:-false}" = "true" ]; then
    ARGS+=( "--enable-ml-filter" "--ml-min-prob" "${ML_MIN_PROB:-0.50}" )
else
    ARGS+=( "--no-enable-ml-filter" )
fi

# News Catalyst Guard
if [ "${ENABLE_NEWS_GUARD:-true}" = "true" ]; then
    ARGS+=( "--enable-news-catalyst-guard" "--news-guard-buffer-minutes" "${NEWS_GUARD_BUFFER_MINUTES:-15}" )
else
    ARGS+=( "--no-enable-news-catalyst-guard" )
fi

# Session Filter
if [ "${ENABLE_SESSION_FILTER:-true}" = "false" ]; then
    ARGS+=( "--no-session-filter" )
fi

export PYTHONPATH="/app/src:${PYTHONPATH}"

echo "Executing AutoTrader: ${PYTHON_BIN} ${ARGS[*]}"
exec "${PYTHON_BIN}" "${ARGS[@]}"
