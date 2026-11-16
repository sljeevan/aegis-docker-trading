"""
OpenBB Macroeconomic & News Catalyst Feed Integration for Gold & Bitcoin Autonomous Trading.

Provides:
1. High-Impact News Catalyst Guard: detects upcoming FOMC, CPI, NFP, GDP releases to pause trading within ±15m.
2. Macro Confluence Driver: fetches US Treasury Yield Curves (10Y, 2Y, spread) and Fed data.
3. Crypto Volatility & Derivatives: fetches Deribit BTC Volatility Index (DVOL).
4. Institutional Positioning: CFTC Commitment of Traders (COT) for Gold and Bitcoin.
5. Microservice Client Architecture: seamlessly queries Aegis OpenBB microservice container (port 8789)
   with local memory micro-caching (<1ms) and graceful local fallback if offline.
6. 100% Fail-safe: Graceful fallback if OpenBB or external API is offline.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

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

_OPENBB_OBB = None
_INIT_LOCK = threading.Lock()
_CALENDAR_CACHE: Dict[str, Any] = {"timestamp": 0.0, "events": []}
_MACRO_CACHE: Dict[str, Any] = {"timestamp": 0.0, "data": {}}
_CACHE_TTL_CALENDAR_SECONDS = 900.0  # 15 minutes
_CACHE_TTL_MACRO_SECONDS = 1800.0   # 30 minutes

# Microservice Discovery & Client Cache Configuration
_SERVICE_CONFIGURED_URL = os.environ.get("OPENBB_SERVICE_URL", "").strip().rstrip("/")
_DEFAULT_CANDIDATE_URLS = [
    "http://openbb-service:8789",
    "http://aegis-openbb:8789",
    "http://127.0.0.1:8789",
    "http://localhost:8789",
]

_SERVICE_LOCK = threading.Lock()
_RESOLVED_SERVICE_URL: Optional[str] = None
_LAST_SERVICE_PROBE_TIME: float = 0.0
_SERVICE_PROBE_INTERVAL: float = 30.0  # Re-probe every 30s if offline or finding service

# Client-side fast microcache (avoids HTTP requests within the same tick cycle)
_CLIENT_NEWS_MICROCACHE: Dict[str, Any] = {"timestamp": 0.0, "buffer": 0, "result": None}
_CLIENT_MACRO_MICROCACHE: Dict[str, Any] = {"timestamp": 0.0, "result": None}
_MICROCACHE_TTL_SECONDS = 5.0  # 5 seconds


def get_obb_instance():
    """Lazily load and return the OpenBB singleton for local execution."""
    global _OPENBB_OBB
    if _OPENBB_OBB is None:
        with _INIT_LOCK:
            if _OPENBB_OBB is None:
                try:
                    from openbb import obb
                    _OPENBB_OBB = obb
                    logger.info("OpenBB platform initialized successfully (local).")
                except Exception as e:
                    logger.debug("OpenBB local platform unavailable: %s", e)
                    return None
    return _OPENBB_OBB


def _get_active_service_url() -> Optional[str]:
    """
    Resolve the active OpenBB microservice URL.
    Checks environment variable first, then probes default candidates.
    Caches successful endpoint to eliminate lookup overhead.
    """
    global _RESOLVED_SERVICE_URL, _LAST_SERVICE_PROBE_TIME
    now = time.time()

    if _RESOLVED_SERVICE_URL and (now - _LAST_SERVICE_PROBE_TIME < _SERVICE_PROBE_INTERVAL):
        return _RESOLVED_SERVICE_URL

    with _SERVICE_LOCK:
        # Double-check inside lock
        if _RESOLVED_SERVICE_URL and (now - _LAST_SERVICE_PROBE_TIME < _SERVICE_PROBE_INTERVAL):
            return _RESOLVED_SERVICE_URL

        _LAST_SERVICE_PROBE_TIME = now

        # 1. If explicit URL is supplied in environment, prioritize it
        candidates = []
        if _SERVICE_CONFIGURED_URL:
            candidates.append(_SERVICE_CONFIGURED_URL)
        candidates.extend([c for c in _DEFAULT_CANDIDATE_URLS if c != _SERVICE_CONFIGURED_URL])

        for url in candidates:
            try:
                resp = requests.get(f"{url}/health", timeout=0.8)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("service") in ("aegis-openbb-gateway", "aegis-openbb-service") or data.get("status") == "healthy":
                        if _RESOLVED_SERVICE_URL != url:
                            logger.info("Connected to Aegis OpenBB microservice at %s", url)
                        _RESOLVED_SERVICE_URL = url
                        return _RESOLVED_SERVICE_URL
            except Exception:
                continue

        # If none succeeded
        _RESOLVED_SERVICE_URL = None
        return None


def check_high_impact_news_window(
    buffer_minutes: int = 15,
    custom_now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Check if the current UTC time is within ±`buffer_minutes` of a high-impact US economic release.

    Returns:
        {
            "is_in_news_window": bool,
            "minutes_to_nearest_event": float,
            "event_title": str,
            "event_time_utc": str,
            "action": "PAUSE" | "TRADE_ALLOWED",
            "upcoming_events": List[Dict]
        }
    """
    global _CLIENT_NEWS_MICROCACHE
    now_ts = time.time()

    # Fast client-side micro-cache check (if custom_now is not set and buffer matches)
    if custom_now is None:
        if (
            _CLIENT_NEWS_MICROCACHE["result"] is not None
            and _CLIENT_NEWS_MICROCACHE["buffer"] == buffer_minutes
            and (now_ts - _CLIENT_NEWS_MICROCACHE["timestamp"] < _MICROCACHE_TTL_SECONDS)
        ):
            return _CLIENT_NEWS_MICROCACHE["result"]

    # 1. Query Aegis OpenBB Microservice if reachable
    service_url = _get_active_service_url()
    if service_url:
        try:
            params = {"buffer_minutes": buffer_minutes}
            if custom_now:
                params["custom_now"] = custom_now.isoformat()
            
            resp = requests.get(f"{service_url}/api/v1/news-catalyst-guard", params=params, timeout=2.0)
            if resp.status_code == 200:
                result = resp.json()
                if custom_now is None:
                    _CLIENT_NEWS_MICROCACHE = {
                        "timestamp": now_ts,
                        "buffer": buffer_minutes,
                        "result": result,
                    }
                return result
        except Exception as e:
            logger.debug("OpenBB microservice query error (%s). Falling back to local/cached data.", e)

    # 2. Local fallback evaluation if service is unreachable
    now = custom_now or datetime.now(timezone.utc)
    events = _get_cached_economic_calendar()

    if not events:
        fallback_res = {
            "is_in_news_window": False,
            "minutes_to_nearest_event": 999.0,
            "event_title": "None",
            "event_time_utc": "",
            "action": "TRADE_ALLOWED",
            "upcoming_events": [],
        }
        if custom_now is None:
            _CLIENT_NEWS_MICROCACHE = {"timestamp": now_ts, "buffer": buffer_minutes, "result": fallback_res}
        return fallback_res

    nearest_event = None
    min_abs_diff = float("inf")
    is_in_window = False

    for ev in events:
        ev_time = ev.get("time_utc")
        if not ev_time:
            continue
        try:
            if isinstance(ev_time, str):
                ev_time_dt = datetime.fromisoformat(ev_time)
                if ev_time_dt.tzinfo is None:
                    ev_time_dt = ev_time_dt.replace(tzinfo=timezone.utc)
            else:
                ev_time_dt = ev_time

            diff_minutes = (ev_time_dt - now).total_seconds() / 60.0
            abs_diff = abs(diff_minutes)
            if abs_diff < min_abs_diff:
                min_abs_diff = abs_diff
                nearest_event = (ev, diff_minutes, ev_time_dt)

            if -buffer_minutes <= diff_minutes <= buffer_minutes:
                is_in_window = True
        except Exception:
            continue

    if is_in_window and nearest_event:
        ev_obj, diff_m, ev_dt = nearest_event
        res = {
            "is_in_news_window": True,
            "minutes_to_nearest_event": round(diff_m, 1),
            "event_title": ev_obj.get("title", "High-Impact Catalyst"),
            "event_time_utc": ev_dt.strftime("%Y-%m-%d %H:%M UTC") if hasattr(ev_dt, "strftime") else str(ev_dt),
            "action": "PAUSE",
            "upcoming_events": [ev for ev in events[:5]],
        }
        if custom_now is None:
            _CLIENT_NEWS_MICROCACHE = {"timestamp": now_ts, "buffer": buffer_minutes, "result": res}
        return res

    nearest_ev_obj = nearest_event[0] if nearest_event else None
    nearest_time = nearest_event[2] if nearest_event else None
    res = {
        "is_in_news_window": False,
        "minutes_to_nearest_event": round(nearest_event[1], 1) if nearest_event else 999.0,
        "event_title": nearest_ev_obj.get("title", "None") if nearest_ev_obj else "None",
        "event_time_utc": nearest_time.strftime("%Y-%m-%d %H:%M UTC") if nearest_time and hasattr(nearest_time, "strftime") else "",
        "action": "TRADE_ALLOWED",
        "upcoming_events": [ev for ev in events[:5]],
    }
    if custom_now is None:
        _CLIENT_NEWS_MICROCACHE = {"timestamp": now_ts, "buffer": buffer_minutes, "result": res}
    return res


def _get_cached_economic_calendar() -> List[Dict[str, Any]]:
    """Retrieve filtered high-impact calendar events with TTL cache (local execution)."""
    global _CALENDAR_CACHE
    now_ts = datetime.now(timezone.utc).timestamp()

    if _CALENDAR_CACHE["events"] and (now_ts - _CALENDAR_CACHE["timestamp"] < _CACHE_TTL_CALENDAR_SECONDS):
        return _CALENDAR_CACHE["events"]

    obb = get_obb_instance()
    if not obb:
        return _CALENDAR_CACHE.get("events", [])

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
                            "time_utc": dt,
                        })
    except Exception as e:
        logger.debug("Fed release calendar query notice: %s", e)

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
                            "time_utc": dt,
                        })
    except Exception as e:
        logger.debug("FRED economic calendar query notice: %s", e)

    events.sort(key=lambda x: x["time_utc"] if x.get("time_utc") else datetime.max.replace(tzinfo=timezone.utc))

    _CALENDAR_CACHE = {
        "timestamp": now_ts,
        "events": events,
    }
    return events


def fetch_openbb_macro_data() -> Dict[str, Any]:
    """
    Fetch comprehensive macroeconomic data:
    1. US Treasury Yield Curve (10Y, 2Y, 3M, 10Y-2Y spread)
    2. Bitcoin Option-Implied Volatility (Deribit DVOL)
    3. COMEX Gold Institutional COT positioning
    """
    global _CLIENT_MACRO_MICROCACHE, _MACRO_CACHE
    now_ts = time.time()

    # Fast client-side microcache
    if (
        _CLIENT_MACRO_MICROCACHE["result"] is not None
        and (now_ts - _CLIENT_MACRO_MICROCACHE["timestamp"] < _MICROCACHE_TTL_SECONDS)
    ):
        return _CLIENT_MACRO_MICROCACHE["result"]

    # 1. Query Aegis OpenBB Microservice if reachable
    service_url = _get_active_service_url()
    if service_url:
        try:
            resp = requests.get(f"{service_url}/api/v1/macro-confluence", timeout=2.0)
            if resp.status_code == 200:
                result = resp.json()
                _CLIENT_MACRO_MICROCACHE = {"timestamp": now_ts, "result": result}
                return result
        except Exception as e:
            logger.debug("OpenBB microservice macro query error (%s). Falling back to local/cached data.", e)

    # 2. Local fallback evaluation
    if _MACRO_CACHE["data"] and (now_ts - _MACRO_CACHE["timestamp"] < _CACHE_TTL_MACRO_SECONDS):
        return _MACRO_CACHE["data"]

    obb = get_obb_instance()
    if not obb:
        fallback_data = _MACRO_CACHE.get("data", {
            "yield_10y": None,
            "yield_2y": None,
            "yield_3m": None,
            "yield_spread_10y_2y": None,
            "yield_curve_inverted": False,
            "btc_dvol": None,
            "gold_cot_net_speculative": None,
            "macro_confluence_score": 0,
            "signals": [],
        })
        _CLIENT_MACRO_MICROCACHE = {"timestamp": now_ts, "result": fallback_data}
        return fallback_data

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
        logger.debug("Yield curve fetch note: %s", e)

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
        logger.debug("Deribit volatility fetch note: %s", e)

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
        logger.debug("CFTC COT fetch note: %s", e)

    _MACRO_CACHE = {
        "timestamp": now_ts,
        "data": data,
    }
    _CLIENT_MACRO_MICROCACHE = {"timestamp": now_ts, "result": data}
    return data


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
