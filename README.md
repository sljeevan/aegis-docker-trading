# Aegis Alpha — Multi-Account Docker Containerization Engine

Production-grade, fully isolated containerization architecture for autonomous quantitative trading on **MetaTrader 5 (MT5)**. Designed for running multiple accounts simultaneously (Live Native Brokers, FTMO, The5ers Prop Firms) with zero port collisions, separate risk models, dedicated databases, and live web-based GUI streaming via **noVNC**.

---

## 1. Architectural Highlights

```
                   ┌────────────────────────────────────────────────────────┐
                   │               Host Linux Server (Ubuntu)               │
                   │                                                        │
                   │  Isolated Docker Bridge Network & Persistent Volumes   │
                   └───────────┬────────────────────────────────────────────┘
                               │
       ┌───────────────────────┼────────────────────────┐
       ▼                       ▼                        ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Container 1:     │  │ Container 2:     │  │ Container 3:     │
│ aegis-xm-live    │  │ aegis-ftmo-100k  │  │ aegis-the5ers-   │
│                  │  │                  │  │ 100k             │
│ • Xvfb (:99)     │  │ • Xvfb (:99)     │  │ • Xvfb (:99)     │
│ • noVNC Web:8081 │  │ • noVNC Web:8082 │  │ • noVNC Web:8083 │
│ • XM MT5 Wine    │  │ • FTMO MT5 Wine  │  │ • 5ers MT5 Wine  │
│ • Bridge (8888)  │  │ • Bridge (8888)  │  │ • Bridge (8888)  │
│ • AutoTrader     │  │ • AutoTrader     │  │ • AutoTrader     │
│   (Flat $75.44,  │  │   (Prop 0.40%,   │  │   (Prop 0.30%,   │
│    1:2 R:R)      │  │    -2.5% Breaker)│  │    -2.0% Breaker)│
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                     │                     │
         ▼                     ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Volume:          │  │ Volume:          │  │ Volume:          │
│ ./data/xm_live/  │  │ ./data/ftmo_100k/│  │ ./data/the5ers/  │
│ • SQLite DB      │  │ • SQLite DB      │  │ • SQLite DB      │
│ • Journals/Logs  │  │ • Journals/Logs  │  │ • Journals/Logs  │
│ • MT5 Terminal   │  │ • MT5 Terminal   │  │ • MT5 Terminal   │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

### Core Capabilities
* **Full Process & Memory Isolation**: Each account runs in its own Linux container with its own Wine 64-bit instance, MT5 terminal, and Python runtime.
* **Zero Socket Port Collisions**: Every container runs the MT5 socket bridge on internal `127.0.0.1:8888`.
* **Remote Browser GUI (noVNC)**: Access and control any account's MT5 interface via browser without installing VNC software.
* **Prop Firm & Broker Profiles**: Tailored risk allocations (Flat Dollar $75.44, 0.40% FTMO Swing, 0.30% The5ers 1-Step) with automated daily drawdown circuit breakers.
* **Autonomous Quantitative Core**: Integrates Marcos López de Prado triple-barrier labeling, Donchian 10/20 breakout/touch, and dynamic ATR volatility stops.

---

## 2. Port & Account Mapping

| Account Service | Container Name | Host Port | Internal Port | Strategy Profile | Risk Model |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **`trader-sandbox`** | `aegis-sandbox` | **`8090`** | `8080` (noVNC) | 1H Donchian 10 Touch | Demo Sandbox ($75 Flat) |
| **`trader-xm-live`** | `aegis-xm-live` | **`8081`** | `8080` (noVNC) | 1H Donchian 10 Touch | Flat Risk $75.44 (1:2 R:R) |
| **`trader-ftmo-100k`** | `aegis-ftmo-100k` | **`8082`** | `8080` (noVNC) | 1H Donchian 10 Touch | 0.40% Risk ($400) / -2.5% Breaker |
| **`trader-the5ers-100k`**| `aegis-the5ers-100k` | **`8083`** | `8080` (noVNC) | 1H Donchian 10 Touch | 0.30% Risk ($300) / -2.0% Breaker |

---

## 3. Quick Start & Verification

### 1. Build the Docker Image
```bash
docker compose build trader-sandbox
```

### 2. Copy MT5 Terminal Files into the Account Volume
Copy your broker's portable MT5 files (`terminal64.exe` and folders) into the volume:
```bash
mkdir -p data/sandbox/terminal data/sandbox/trading_data
cp -r /path/to/your/MT5/* data/sandbox/terminal/
```

### 3. Launch the Container
```bash
docker compose up -d trader-sandbox
```

### 4. Access the Live MT5 GUI in Your Browser
Set up a secure SSH tunnel from your local machine:
```bash
ssh -L 8090:localhost:8090 ubuntu@<your-server-ip>
```
Open your browser to:
👉 **`http://localhost:8090/vnc.html`** and click **Connect**.

---

## 4. Deploying Prop Firm & Live Accounts

### FTMO $100,000 Prop Firm Setup
1. Copy template:
   ```bash
   cp configs/ftmo_100k.env.example configs/ftmo_100k.env
   ```
2. Enter your FTMO login credentials in `configs/ftmo_100k.env`.
3. Copy your FTMO MT5 terminal files into `data/ftmo_100k/terminal/`.
4. Launch:
   ```bash
   docker compose up -d trader-ftmo-100k
   ```
5. View GUI: Forward port `8082` via SSH (`ssh -L 8082:localhost:8082 ...`) and browse `http://localhost:8082/vnc.html`.

### The5ers $100,000 Prop Firm Setup
1. Copy template:
   ```bash
   cp configs/the5ers_100k.env.example configs/the5ers_100k.env
   ```
2. Enter credentials in `configs/the5ers_100k.env`.
3. Launch:
   ```bash
   docker compose up -d trader-the5ers-100k
   ```

---

## 5. Operations & Monitoring

```bash
# View live container status
docker compose ps

# Follow live trading logs for an account
docker compose logs -f trader-sandbox

# Inspect internal container process tree
docker exec -it aegis-sandbox ps aux

# Query live SQLite trading database
python3 -c "
import sqlite3
conn = sqlite3.connect('data/sandbox/trading_data/auto_trader.db')
c = conn.cursor()
c.execute('SELECT timestamp, symbol, price, decision FROM scan_logs ORDER BY id DESC LIMIT 5')
for r in c.fetchall(): print(r)
"

# Stop a container cleanly
docker compose stop trader-sandbox
```

---

## 6. License & Security

* **Security**: All real `.env` files and `data/` volume directories are strictly gitignored to guarantee zero credential leakage.
* **Core Engine**: Powered by [sljeevan/gold-trading-system](https://github.com/sljeevan/gold-trading-system).
