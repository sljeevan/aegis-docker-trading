"""
Aegis Alpha — Centralized OpenBB Macroeconomic & News Catalyst Microservice Gateway.

Provides high-availability, low-latency REST endpoints for:
1. High-Impact News Catalyst Guard: Blackout windows around CPI, PPI, FOMC, NFP, GDP releases.
2. Macro Confluence Driver: US Treasury Yield Curves (10Y, 2Y, spread), Deribit BTC DVOL, CFTC Gold COT.
3. In-memory caching with asynchronous background refresh to ensure <1ms response time to trading engines.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aegis-openbb-service")

# High-impact catalyst keywords that induce extreme spread spikes and slippage
HIGH_IMPACT_CATALYSTS = [
    "FOMC",
    "FEDERAL RESERVE",
    "INTEREST RATE",
    "FUNDS RATE",
    "CPI",
    "CONSUMER PRICE",
    "INFLATION",
    "PAYROLL",
    "NONFARM",
    "EMPLOYMENT SITUATION",
    "UNEMPLOYMENT",
    "GDP",
    "GROSS DOMESTIC",
    "PCE",
    "PERSONAL CONSUMPTION",
]

# Cache TTL Configuration
CACHE_TTL_CALENDAR_SECONDS = float(os.environ.get("OPENBB_CACHE_TTL_CALENDAR_SECONDS", 900.0))  # 15 minutes
CACHE_TTL_MACRO_SECONDS = float(os.environ.get("OPENBB_CACHE_TTL_MACRO_SECONDS", 1800.0))     # 30 minutes

_SERVICE_START_TIME = time.time()
_INIT_LOCK = threading.Lock()
_OPENBB_OBB = None

# Global Caches protected by locks
_CACHE_LOCK = threading.Lock()
_CALENDAR_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "last_updated_utc": None,
    "events": [],
    "error": None,
}
_MACRO_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "last_updated_utc": None,
    "data": {},
    "error": None,
}

_REFRESH_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None
_RUNNING = True


def get_obb_instance():
    """Lazily load and return the OpenBB singleton."""
    global _OPENBB_OBB
    if _OPENBB_OBB is None:
        with _INIT_LOCK:
            if _OPENBB_OBB is None:
                try:
                    from openbb import obb
                    _OPENBB_OBB = obb
                    logger.info("OpenBB platform initialized successfully.")
                except Exception as e:
                    logger.warning("OpenBB platform unavailable: %s", e)
                    return None
    return _OPENBB_OBB


def _parse_fed_calendar_datetime(date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
    """Parse date and time strings from Federal Reserve release calendar into UTC datetime."""
    if not date_str:
        return None
    try:
        time_clean = (time_str or "12:00 p.m.").strip().lower().replace(".", "")
        dt_str = f"{date_str} {time_clean}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %I:%M %p")
        return dt.replace(tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _normalize_to_utc(dt_val: Any) -> Optional[datetime]:
    """Convert various datetime/timestamp types to UTC datetime."""
    if not dt_val:
        return None
    try:
        if isinstance(dt_val, datetime):
            if dt_val.tzinfo is None:
                return dt_val.replace(tzinfo=timezone.utc)
            return dt_val.astimezone(timezone.utc)
        if hasattr(dt_val, "to_pydatetime"):
            py_dt = dt_val.to_pydatetime()
            if py_dt.tzinfo is None:
                return py_dt.replace(tzinfo=timezone.utc)
            return py_dt.astimezone(timezone.utc)
        if isinstance(dt_val, str):
            return datetime.fromisoformat(dt_val).astimezone(timezone.utc)
    except Exception:
        pass
    return None


def fetch_raw_economic_calendar() -> List[Dict[str, Any]]:
    """Fetch and filter high-impact calendar events directly from OpenBB."""
    obb = get_obb_instance()
    if not obb:
        raise RuntimeError("OpenBB platform is not available in the environment")

    events = []

    # 1. Federal Reserve Release Calendar
    try:
        fed_cal = obb.federal_reserve.release_calendar()
        if isinstance(fed_cal, list):
            for item in fed_cal:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).upper()
                release = str(item.get("release", "")).upper()
                date_str = item.get("date")
                time_str = item.get("time", "12:00 p.m.")
                if any(kw in title or kw in release for kw in HIGH_IMPACT_CATALYSTS):
                    dt = _parse_fed_calendar_datetime(date_str, time_str)
                    if dt:
                        events.append({
                            "source": "FED",
                            "title": item.get("title", release),
                            "time_utc": dt.isoformat(),
                            "_dt": dt,
                        })
    except Exception as e:
        logger.warning("Fed release calendar query warning: %s", e)

    # 2. FRED Economic Calendar
    try:
        fred_cal = obb.fred.economy.calendar()
        results = getattr(fred_cal, "results", []) or []
        for item in results:
            ev_name = str(getattr(item, "event", "")).upper()
            if any(kw in ev_name for kw in HIGH_IMPACT_CATALYSTS):
                dt_raw = getattr(item, "date", None)
                if dt_raw:
                    dt = _normalize_to_utc(dt_raw)
                    if dt:
                        events.append({
                            "source": "FRED",
                            "title": getattr(item, "event", "High-Impact Release"),
                            "time_utc": dt.isoformat(),
                            "_dt": dt,
                        })
    except Exception as e:
        logger.warning("FRED economic calendar query warning: %s", e)

    # Sort upcoming events chronologically
    events.sort(key=lambda x: x["_dt"] if x.get("_dt") else datetime.max.replace(tzinfo=timezone.utc))

    # Strip private datetime object before caching/returning
    clean_events = []
    for ev in events:
        clean_events.append({
            "source": ev["source"],
            "title": ev["title"],
            "time_utc": ev["time_utc"],
        })

    logger.info("Successfully fetched %d high-impact economic calendar events", len(clean_events))
    return clean_events


def fetch_raw_macro_data() -> Dict[str, Any]:
    """Fetch comprehensive macroeconomic data from OpenBB."""
    obb = get_obb_instance()
    if not obb:
        raise RuntimeError("OpenBB platform is not available in the environment")

    data: Dict[str, Any] = {
        "yield_10y": None,
        "yield_2y": None,
        "yield_3m": None,
        "yield_spread_10y_2y": None,
        "yield_curve_inverted": False,
        "btc_dvol": None,
        "gold_cot_net_speculative": None,
        "macro_confluence_score": 0,
        "signals": [],
    }

    # 1. US Treasury Yield Curve
    try:
        yc = obb.federal_reserve.yield_curve()
        results = getattr(yc, "results", []) or []
        rate_map = {}
        for r in results:
            mat = str(getattr(r, "maturity", "")).lower()
            val = getattr(r, "rate", None)
            if val is not None:
                rate_pct = val * 100.0 if val < 1.0 else val
                rate_map[mat] = rate_pct

        y10 = rate_map.get("year_10")
        y2 = rate_map.get("year_2")
        y3m = rate_map.get("month_3")

        data["yield_10y"] = y10
        data["yield_2y"] = y2
        data["yield_3m"] = y3m

        if y10 is not None and y2 is not None:
            spread = round(y10 - y2, 3)
            data["yield_spread_10y_2y"] = spread
            data["yield_curve_inverted"] = spread < 0
            if spread < 0:
                data["signals"].append(f"Yield Curve Inverted ({spread:+.2f}% 10Y-2Y) -> Late-cycle recession risk / flight to Gold")
                data["macro_confluence_score"] += 1
            else:
                data["signals"].append(f"Yield Curve Normal/Steepening ({spread:+.2f}% 10Y-2Y)")
    except Exception as e:
        logger.warning("Yield curve query warning: %s", e)

    # 2. Deribit Bitcoin Volatility Index (DVOL)
    try:
        dvol_res = obb.deribit.volatility.index()
        results = getattr(dvol_res, "results", []) or []
        if results:
            latest = results[-1]
            close_vol = getattr(latest, "close", None)
            if close_vol is not None:
                data["btc_dvol"] = round(float(close_vol), 2)
                if float(close_vol) > 55.0:
                    data["signals"].append(f"BTC DVOL High ({close_vol:.1f}) -> Elevated option-implied volatility")
                elif float(close_vol) < 35.0:
                    data["signals"].append(f"BTC DVOL Compressed ({close_vol:.1f}) -> Volatility contraction / breakout pending")
    except Exception as e:
        logger.warning("Deribit volatility query warning: %s", e)

    # 3. Gold Institutional Commitment of Traders (COT)
    try:
        cot_res = obb.cftc.cot(code="CFTC_088691")
        results = getattr(cot_res, "results", []) or []
        if results:
            latest_cot = results[-1]
            long_pos = getattr(latest_cot, "non_commercial_positions_long_all", 0) or 0
            short_pos = getattr(latest_cot, "non_commercial_positions_short_all", 0) or 0
            net_spec = long_pos - short_pos
            data["gold_cot_net_speculative"] = net_spec
            if net_spec > 150000:
                data["signals"].append(f"CFTC Institutional Gold Bias: Bullish (+{net_spec:,} net long contracts)")
                data["macro_confluence_score"] += 1
            elif net_spec < 50000:
                data["signals"].append(f"CFTC Institutional Gold Bias: Light / De-risked ({net_spec:,} net long contracts)")
    except Exception as e:
        logger.warning("CFTC COT query warning: %s", e)

    logger.info("Successfully fetched macro data: score=%d, signals=%d", data["macro_confluence_score"], len(data["signals"]))
    return data


def _refresh_caches():
    """Worker function to refresh calendar and macro data caches."""
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Calendar
    try:
        cal = fetch_raw_economic_calendar()
        with _CACHE_LOCK:
            _CALENDAR_CACHE["events"] = cal
            _CALENDAR_CACHE["timestamp"] = now_ts
            _CALENDAR_CACHE["last_updated_utc"] = now_iso
            _CALENDAR_CACHE["error"] = None
    except Exception as e:
        logger.error("Failed to refresh economic calendar: %s", e)
        with _CACHE_LOCK:
            _CALENDAR_CACHE["error"] = str(e)

    # 2. Macro Data
    try:
        macro = fetch_raw_macro_data()
        with _CACHE_LOCK:
            _MACRO_CACHE["data"] = macro
            _MACRO_CACHE["timestamp"] = now_ts
            _MACRO_CACHE["last_updated_utc"] = now_iso
            _MACRO_CACHE["error"] = None
    except Exception as e:
        logger.error("Failed to refresh macro data: %s", e)
        with _CACHE_LOCK:
            _MACRO_CACHE["error"] = str(e)


def _background_cache_loop():
    """Background thread to keep data pre-warmed and cached."""
    logger.info("OpenBB background cache manager started.")
    # Initial warm-up
    _refresh_caches()

    while _RUNNING:
        try:
            # Wait up to 30s or until manually awakened
            _REFRESH_EVENT.wait(timeout=30.0)
            _REFRESH_EVENT.clear()

            now_ts = time.time()
            need_cal_refresh = (now_ts - _CALENDAR_CACHE["timestamp"]) >= CACHE_TTL_CALENDAR_SECONDS
            need_macro_refresh = (now_ts - _MACRO_CACHE["timestamp"]) >= CACHE_TTL_MACRO_SECONDS

            if need_cal_refresh:
                logger.info("Refreshing economic calendar cache (TTL expired)...")
                try:
                    cal = fetch_raw_economic_calendar()
                    with _CACHE_LOCK:
                        _CALENDAR_CACHE["events"] = cal
                        _CALENDAR_CACHE["timestamp"] = now_ts
                        _CALENDAR_CACHE["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
                        _CALENDAR_CACHE["error"] = None
                except Exception as e:
                    logger.error("Error refreshing calendar in background: %s", e)
                    with _CACHE_LOCK:
                        _CALENDAR_CACHE["error"] = str(e)

            if need_macro_refresh:
                logger.info("Refreshing macro confluence cache (TTL expired)...")
                try:
                    macro = fetch_raw_macro_data()
                    with _CACHE_LOCK:
                        _MACRO_CACHE["data"] = macro
                        _MACRO_CACHE["timestamp"] = now_ts
                        _MACRO_CACHE["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
                        _MACRO_CACHE["error"] = None
                except Exception as e:
                    logger.error("Error refreshing macro data in background: %s", e)
                    with _CACHE_LOCK:
                        _MACRO_CACHE["error"] = str(e)

        except Exception as e:
            logger.error("Exception in OpenBB cache loop: %s", e)
            time.sleep(5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan context manager for thread lifecycle."""
    global _WORKER_THREAD, _RUNNING
    _RUNNING = True
    _WORKER_THREAD = threading.Thread(target=_background_cache_loop, daemon=True, name="OpenBBCacheWorker")
    _WORKER_THREAD.start()
    yield
    _RUNNING = False
    _REFRESH_EVENT.set()
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        _WORKER_THREAD.join(timeout=2.0)
    logger.info("OpenBB background cache manager stopped.")


# Initialize FastAPI App
app = FastAPI(
    title="Aegis Alpha OpenBB Catalyst & Macro Gateway",
    version="1.0.0",
    description="Centralized, ultra-low latency OpenBB data provider for all Aegis trading engines",
    lifespan=lifespan,
)


@app.get("/health", tags=["System"])
def health_check():
    """Health check endpoint for Docker container and orchestrator."""
    with _CACHE_LOCK:
        cal_count = len(_CALENDAR_CACHE.get("events", []))
        macro_ready = bool(_MACRO_CACHE.get("data", {}))
        last_cal_upd = _CALENDAR_CACHE.get("last_updated_utc")
        last_macro_upd = _MACRO_CACHE.get("last_updated_utc")
        cal_err = _CALENDAR_CACHE.get("error")
        macro_err = _MACRO_CACHE.get("error")

    status_str = "healthy"
    return {
        "status": status_str,
        "service": "aegis-openbb-gateway",
        "uptime_seconds": round(time.time() - _SERVICE_START_TIME, 1),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "calendar": {
            "events_cached": cal_count,
            "last_updated_utc": last_cal_upd,
            "last_error": cal_err,
        },
        "macro": {
            "cached": macro_ready,
            "last_updated_utc": last_macro_upd,
            "last_error": macro_err,
        },
    }


@app.get("/api/v1/news-catalyst-guard", tags=["Trading Guards"])
def get_news_catalyst_guard(
    buffer_minutes: int = Query(default=15, ge=1, le=120, description="Blackout window buffer in minutes"),
    custom_now: Optional[str] = Query(default=None, description="Optional ISO timestamp for simulation/backtesting"),
):
    """
    Check if current UTC time is within ±buffer_minutes of a high-impact US economic release.
    Returns identical contract expected by auto_trader.py.
    """
    if custom_now:
        try:
            now = datetime.fromisoformat(custom_now)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid custom_now ISO format")
    else:
        now = datetime.now(timezone.utc)

    with _CACHE_LOCK:
        raw_events = list(_CALENDAR_CACHE.get("events", []))

    if not raw_events:
        return {
            "is_in_news_window": False,
            "minutes_to_nearest_event": 999.0,
            "event_title": "None",
            "event_time_utc": "",
            "action": "TRADE_ALLOWED",
            "upcoming_events": [],
            "source": "cache_empty",
        }

    nearest_event = None
    min_abs_diff = float("inf")
    is_in_window = False

    for ev in raw_events:
        time_str = ev.get("time_utc")
        if not time_str:
            continue
        try:
            ev_dt = datetime.fromisoformat(time_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            
            diff_minutes = (ev_dt - now).total_seconds() / 60.0
            abs_diff = abs(diff_minutes)
            if abs_diff < min_abs_diff:
                min_abs_diff = abs_diff
                nearest_event = (ev, diff_minutes, ev_dt)

            if -buffer_minutes <= diff_minutes <= buffer_minutes:
                is_in_window = True
        except Exception:
            continue

    if is_in_window and nearest_event:
        ev_obj, diff_m, ev_dt = nearest_event
        return {
            "is_in_news_window": True,
            "minutes_to_nearest_event": round(diff_m, 1),
            "event_title": ev_obj.get("title", "High-Impact Catalyst"),
            "event_time_utc": ev_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "action": "PAUSE",
            "buffer_minutes": buffer_minutes,
            "upcoming_events": raw_events[:5],
        }

    nearest_ev_obj = nearest_event[0] if nearest_event else None
    nearest_time = nearest_event[2] if nearest_event else None
    return {
        "is_in_news_window": False,
        "minutes_to_nearest_event": round(nearest_event[1], 1) if nearest_event else 999.0,
        "event_title": nearest_ev_obj.get("title", "None") if nearest_ev_obj else "None",
        "event_time_utc": nearest_time.strftime("%Y-%m-%d %H:%M UTC") if nearest_time else "",
        "action": "TRADE_ALLOWED",
        "buffer_minutes": buffer_minutes,
        "upcoming_events": raw_events[:5],
    }


@app.get("/api/v1/macro-confluence", tags=["Macro Intelligence"])
def get_macro_confluence():
    """Retrieve comprehensive US Treasury, Volatility (DVOL), and COT positioning."""
    with _CACHE_LOCK:
        data = dict(_MACRO_CACHE.get("data", {}))
        last_updated = _MACRO_CACHE.get("last_updated_utc")

    if not data:
        # Fallback empty structure
        return {
            "yield_10y": None,
            "yield_2y": None,
            "yield_3m": None,
            "yield_spread_10y_2y": None,
            "yield_curve_inverted": False,
            "btc_dvol": None,
            "gold_cot_net_speculative": None,
            "macro_confluence_score": 0,
            "signals": [],
            "last_updated_utc": None,
        }

    data["last_updated_utc"] = last_updated
    return data


@app.get("/api/v1/calendar", tags=["Macro Intelligence"])
def get_calendar(limit: int = Query(default=50, ge=1, le=200)):
    """Retrieve upcoming high-impact economic calendar events."""
    with _CACHE_LOCK:
        events = list(_CALENDAR_CACHE.get("events", []))
        last_updated = _CALENDAR_CACHE.get("last_updated_utc")

    return {
        "total_events": len(events),
        "limit": limit,
        "last_updated_utc": last_updated,
        "events": events[:limit],
    }


@app.post("/api/v1/cache/refresh", tags=["System"])
def refresh_cache_endpoint():
    """Trigger an immediate asynchronous cache refresh."""
    _REFRESH_EVENT.set()
    return {
        "status": "refresh_triggered",
        "message": "OpenBB cache refresh cycle requested in background.",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8789))
    uvicorn.run("gold_trader.services.openbb_service:app", host="0.0.0.0", port=port, reload=False)
