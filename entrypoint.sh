#!/bin/bash
# ==============================================================================
# Aegis Alpha — Container Master Entrypoint
# Initializes environment, mounts, Wine configuration, and supervisord
# ==============================================================================

set -e

echo "================================================================="
echo " Aegis Alpha Autonomous Trading Node initializing..."
echo " Node ID: ${CONTAINER_NODE_ID:-aegis-node}"
echo " Time   : $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "================================================================="

# 1. Ensure required data directories exist
mkdir -p /app/data/terminal
mkdir -p /app/data/trading_data
mkdir -p /app/config

# 2. Bridge compatibility: link /app/config/.env to bridge_server default lookup path
mkdir -p /home/mcpuser/MCP/gold-trading-system
if [ -f "/app/config/.env" ]; then
    ln -sf /app/config/.env /home/mcpuser/MCP/gold-trading-system/.env
    ln -sf /app/config/.env /app/.env
    echo "✅ Loaded environment configuration from /app/config/.env"
else
    echo "ℹ️  No /app/config/.env found; relying on container system environment variables."
fi

# 3. Ensure noVNC index directs straight to the VNC viewer
ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# 4. Check for MT5 terminal binary in volume
if [ ! -f "/app/data/terminal/terminal64.exe" ]; then
    echo "================================================================="
    echo "⚠️  NOTICE: MT5 terminal not yet initialized in /app/data/terminal"
    echo "   You can connect to this container's web browser UI at:"
    echo "   http://<host-ip>:${NOVNC_PORT:-8080}"
    echo "   and install/copy your broker's MT5 portable files into the volume."
    echo "================================================================="
else
    echo "✅ MT5 terminal detected at /app/data/terminal/terminal64.exe"
fi

# 5. Hand over control to supervisord
echo "Starting Supervisord process coordinator..."
exec supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
