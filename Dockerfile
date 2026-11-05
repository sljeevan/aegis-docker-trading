# syntax=docker/dockerfile:1
FROM ubuntu:22.04

LABEL maintainer="Aegis Alpha Quantitative Team"
LABEL description="Isolated Multi-Account Execution Node: Wine 64-bit + MT5 + noVNC + Python 3.11"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8
ENV DISPLAY=:99
ENV WINEARCH=win64
ENV WINEPREFIX=/root/.wine
ENV WINEDEBUG=-all
ENV WINEDLLOVERRIDES="mscoree,mshtml="

# 1. Install base system dependencies, X11, fluxbox, VNC, noVNC, supervisor, and WineHQ Stable
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        gnupg2 \
        software-properties-common \
        locales \
        procps \
        net-tools \
        unzip \
        scrot \
        xvfb \
        fluxbox \
        x11vnc \
        novnc \
        websockify \
        supervisor \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential && \
    mkdir -pm755 /etc/apt/keyrings && \
    wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key && \
    wget -qNP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources && \
    apt-get update && \
    apt-get install -y --install-recommends winehq-stable && \
    locale-gen en_US.UTF-8 && \
    ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. Initialize Wine 64-bit environment and install Windows Python 3.11 into Wine (C:\python64)
RUN wineboot --init && \
    while pgrep wineserver > /dev/null; do sleep 1; done && \
    wget -q https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe -O /tmp/python-installer.exe && \
    xvfb-run -a wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 TargetDir=C:\\python64 && \
    while pgrep wineserver > /dev/null; do sleep 1; done && \
    rm -f /tmp/python-installer.exe

# 3. Install MetaTrader5 and numpy into Wine Python
RUN xvfb-run -a wine C:\\python64\\python.exe -m pip install --no-cache-dir --upgrade pip && \
    xvfb-run -a wine C:\\python64\\python.exe -m pip install --no-cache-dir MetaTrader5==5.0.45 numpy==1.26.4 && \
    while pgrep wineserver > /dev/null; do sleep 1; done

# 4. Set up Linux Python virtual environment for Aegis AutoTrader
RUN python3 -m venv /opt/aegis_venv && \
    /opt/aegis_venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/aegis_venv/bin/pip install --no-cache-dir \
        "numpy>=1.24.0" \
        "pandas>=2.0.0" \
        "pydantic>=2.0.0" \
        "requests>=2.31.0" \
        "yfinance>=0.2.35" \
        "scikit-learn>=1.3.0" \
        "lightgbm>=4.0.0" \
        "joblib>=1.3.0" \
        "scipy>=1.11.0" \
        "rich>=13.0.0"

# 5. Create application directories
WORKDIR /app
RUN mkdir -p /app/src /app/config /app/data/terminal /app/data/trading_data /app/docker/scripts

# 6. Copy container control scripts and supervisor configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
COPY scripts/run_bridge_container.sh /app/docker/scripts/run_bridge_container.sh
COPY scripts/run_trader_container.sh /app/docker/scripts/run_trader_container.sh

RUN chmod +x /entrypoint.sh /app/docker/scripts/*.sh

# 7. Clone Aegis Quantitative Core Engine into default source tree
RUN git clone https://github.com/sljeevan/gold-trading-system.git /tmp/aegis-core && \
    cp -r /tmp/aegis-core/src/* /app/src/ && \
    rm -rf /tmp/aegis-core

# Port 8080: noVNC browser interface
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
