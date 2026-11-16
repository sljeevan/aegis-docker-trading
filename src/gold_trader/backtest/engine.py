"""
Aegis Alpha — Account Backtest Engine
High-fidelity, zero-lookahead backtesting simulation with 100% production execution parity.
Implements Two-Stage Execution (+1.25R scale-out, BE shift, Runner Trailing),
Adverse-first same-bar priority, Wholesale channel limit pricing,
Concurrency Sweet-Spot, Broker frictions, and Prop Firm Compliance Auditing.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

try:
    import scipy.stats as stats
except ImportError:
    stats = None

from gold_trader.backtest.compliance import ComplianceResult, PropFirmAuditor
from gold_trader.backtest.profile_loader import AccountProfile
from gold_trader.core.data_feed import fetch_gold_candles
from gold_trader.core.indicators import (
    calc_adx,
    calc_atr,
    calc_donchian,
    calc_ema,
    calc_supertrend,
)

# Standard asset properties & institutional session frictions
ASSET_SPECS: Dict[str, Dict[str, Any]] = {
    "XAUUSD": {
        "contract_size": 100.0,
        "min_sl_dist": 8.00,
        "sl_atr_mult": 1.50,
        "min_lot": 0.01,
        "be_offset": 0.50,
        "spread": 0.35,
        "slippage": 0.15,
        "swap_daily": 0.05,
        "default_session_filter": True,
        "adx_min": None,
        "trail_buffer": 4.50,
        "scale_out_r": 1.25,
        "normal_spread": 0.35,
        "rollover_spread": 1.80,
        "ny_open_spread": 0.85,
    },
    "BTCUSD": {
        "contract_size": 1.0,
        "min_sl_dist": 500.00,
        "sl_atr_mult": 1.50,
        "min_lot": 0.01,
        "be_offset": 30.00,
        "spread": 15.00,
        "slippage": 5.00,
        "swap_daily": 2.50,
        "default_session_filter": False,
        "adx_min": None,
        "trail_buffer": 350.00,
        "scale_out_r": 1.00,
        "normal_spread": 15.00,
        "rollover_spread": 22.00,
        "ny_open_spread": 18.00,
    },
    "ETHUSD": {
        "contract_size": 1.0,
        "min_sl_dist": 20.00,
        "sl_atr_mult": 1.50,
        "min_lot": 0.02,
        "be_offset": 2.00,
        "spread": 0.75,
        "slippage": 0.25,
        "swap_daily": 0.10,
        "default_session_filter": False,
        "adx_min": 22.0,
        "trail_buffer": 10.00,
        "scale_out_r": 1.00,
        "normal_spread": 0.75,
        "rollover_spread": 2.00,
        "ny_open_spread": 1.20,
    },
}


def get_session_friction(
    symbol: str,
    dt_str: str,
    base_spec: Dict[str, Any],
    dynamic_enabled: bool = True,
) -> Tuple[float, float, float]:
    """
    Returns (spread, slippage, half_friction) taking into account session liquidity.
    Accounts for 21:00-23:00 UTC rollover spread blowouts and NY cash open volatility.
    """
    base_spread = base_spec["spread"]
    base_slippage = base_spec["slippage"]
    if not dynamic_enabled:
        return base_spread, base_slippage, (base_spread / 2.0) + base_slippage

    try:
        time_part = dt_str.split(" ")[1] if " " in dt_str else dt_str.split("T")[1]
        hour = int(time_part.split(":")[0])
    except Exception:
        hour = 12

    if symbol == "XAUUSD":
        if hour in (21, 22):
            spread = base_spec.get("rollover_spread", 1.80)
            slippage = 0.40
        elif hour in (13, 14):
            spread = base_spec.get("ny_open_spread", 0.85)
            slippage = 0.30
        else:
            spread = base_spread
            slippage = base_slippage
    elif symbol == "ETHUSD":
        if hour in (21, 22, 23):
            spread = base_spec.get("rollover_spread", 2.00)
            slippage = 0.50
        elif hour in (13, 14):
            spread = base_spec.get("ny_open_spread", 1.20)
            slippage = 0.40
        else:
            spread = base_spread
            slippage = base_slippage
    elif symbol == "BTCUSD":
        if hour in (21, 22):
            spread = base_spec.get("rollover_spread", 22.00)
            slippage = 7.00
        else:
            spread = base_spread
            slippage = base_slippage
    else:
        spread = base_spread
        slippage = base_slippage

    half_friction = (spread / 2.0) + slippage
    return spread, slippage, half_friction


class BacktestNewsGuard:
    """
    Simulates production News Catalyst Guard by querying the Aegis OpenBB microservice
    or local OpenBB engine to block entries around high-impact US macro releases.
    """
    _cached_events: Optional[List[Tuple[float, str]]] = None

    def __init__(self, buffer_minutes: int = 15):
        self.buffer_minutes = buffer_minutes
        self.event_timestamps: List[Tuple[float, str]] = []
        if BacktestNewsGuard._cached_events is not None:
            self.event_timestamps = list(BacktestNewsGuard._cached_events)
        else:
            self._init_calendar()
            BacktestNewsGuard._cached_events = list(self.event_timestamps)

    def _init_calendar(self):
        try:
            resp = requests.get("http://localhost:8789/api/v1/calendar?limit=200", timeout=1.5)
            if resp.status_code == 200:
                data = resp.json()
                for ev in data.get("events", []):
                    t_str = ev.get("time_utc")
                    if t_str:
                        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        self.event_timestamps.append((dt.timestamp(), ev.get("title", "High-Impact Catalyst")))
        except Exception:
            pass

        if not self.event_timestamps:
            try:
                from gold_trader.core.openbb_feed import _get_cached_economic_calendar
                events = _get_cached_economic_calendar()
                for ev in events:
                    t_val = ev.get("time_utc")
                    if isinstance(t_val, str):
                        dt = datetime.fromisoformat(t_val.replace("Z", "+00:00"))
                    elif isinstance(t_val, datetime):
                        dt = t_val
                    else:
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self.event_timestamps.append((dt.timestamp(), ev.get("title", "News Catalyst")))
            except Exception:
                pass

    def is_in_news_window(self, dt_str: str) -> Tuple[bool, str]:
        if not self.event_timestamps:
            return False, ""
        try:
            clean_str = dt_str.replace("T", " ")
            parts = clean_str.split(" ")
            d_part = parts[0]
            t_part = parts[1] if len(parts) > 1 else "00:00"
            h, m = int(t_part.split(":")[0]), int(t_part.split(":")[1])
            y, mo, d = [int(x) for x in d_part.split("-")]
            candle_dt = datetime(y, mo, d, h, m, tzinfo=timezone.utc)
            candle_ts = candle_dt.timestamp()
        except Exception:
            return False, ""

        buf_sec = self.buffer_minutes * 60.0
        for ev_ts, title in self.event_timestamps:
            if abs(ev_ts - candle_ts) <= buf_sec:
                return True, title
        return False, ""


@dataclass
class BacktestTrade:
    symbol: str
    direction: str  # 'BUY' or 'SELL'
    entry_bar: int
    entry_date: str
    entry_price: float
    initial_sl: float
    current_sl: float
    risk_distance: float
    planned_risk_usd: float
    initial_volume: float
    remaining_volume: float
    scale_out_executed: bool = False
    scale_out_price: Optional[float] = None
    scale_out_pnl: float = 0.0
    scale_out_date: Optional[str] = None
    is_runner: bool = False
    peak_excursion: float = 0.0
    exit_bar: Optional[int] = None
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_pnl: float = 0.0
    net_r: float = 0.0
    commission: float = 0.0
    swaps: float = 0.0


@dataclass
class BacktestResult:
    profile: AccountProfile
    period: str
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    net_profit_usd: float
    return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    net_r: float
    payoff_ratio: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    sharpe_ratio: float
    deflated_sharpe_ratio: float
    compliance: ComplianceResult
    trades: List[Dict[str, Any]] = field(default_factory=list)
    monthly_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def compute_deflated_sharpe_ratio(
    returns: List[float],
    num_trials: int = 50,
    annualization_factor: float = math.sqrt(252),
) -> float:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio."""
    if len(returns) < 5:
        return 0.0
    r = np.array(returns)
    mean = np.mean(r)
    std = np.std(r, ddof=1)
    if std <= 1e-8:
        return 0.0
    sr_ann = (mean / std) * annualization_factor
    n = len(r)

    if stats is None:
        # Standard fallback if scipy is not installed
        z_stat = (sr_ann - 1.0) / max(0.1, 1.0 / math.sqrt(n))
        return float(max(0.0, min(1.0, 0.5 * (1.0 + math.erf(z_stat / math.sqrt(2.0))))))

    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    euler = 0.5772156649
    z = math.sqrt(2.0 * math.log(max(num_trials, 2)))
    exp_max_sr = ((1.0 - euler) * stats.norm.ppf(1.0 - 1.0 / max(num_trials, 2)) + euler * stats.norm.ppf(1.0 - 1.0 / (max(num_trials, 2) * math.e)))
    gamma_1 = skew / annualization_factor
    gamma_2 = (kurt - 3.0) / (annualization_factor**2)
    var_sr = (1.0 - gamma_1 * (sr_ann / annualization_factor) + (gamma_2 / 4.0) * ((sr_ann / annualization_factor)**2)) / max(n - 1, 1)
    if var_sr <= 1e-12:
        return 0.0
    z_stat = (sr_ann - exp_max_sr) / math.sqrt(var_sr)
    return float(stats.norm.cdf(z_stat))


class AccountBacktestEngine:
    def __init__(self, profile: AccountProfile):
        self.profile = profile
        self.auditor = PropFirmAuditor(profile)

    def load_market_data(self, symbol: str, period: str = "2y", interval: str = "1h") -> Dict[str, Any]:
        """Fetch and cache candle data with technical indicators."""
        candles = fetch_gold_candles(symbol, period=period, interval=interval)
        n = len(candles)
        if n < 50:
            raise RuntimeError(f"Insufficient candle history for {symbol}: {n} candles.")

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        opens = [float(c["open"]) for c in candles]
        dates = [str(c["date"]) for c in candles]

        # Calculate indicators
        ema50 = calc_ema(closes, 50)
        atr14 = calc_atr(highs, lows, closes, 14)
        st = calc_supertrend(highs, lows, closes, 10, 3.0)
        dc10 = calc_donchian(highs, lows, 10)
        dc_channels = {
            10: dc10,
            15: calc_donchian(highs, lows, 15),
            20: calc_donchian(highs, lows, 20),
            25: calc_donchian(highs, lows, 25),
        }
        adx_dict = calc_adx(highs, lows, closes, 14)
        adx = adx_dict.get("adx", [20.0] * n)

        # 50 EMA Slope
        ema50_slopes = [0.0] * n
        for i in range(6, n):
            if ema50[i] is not None and ema50[i - 5] is not None and ema50[i - 5] > 0:
                ema50_slopes[i] = ((ema50[i] - ema50[i - 5]) / ema50[i - 5]) * 100.0

        # Sub-bar (5M) high-fidelity resolution data for micro-loop touch & intra-bar exits
        subbars_by_hour: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if getattr(self.profile, "use_subbar_resolution", True):
            try:
                sub_period = "60d" if period in ("6mo", "1y", "2y", "5y", "max") else period
                sub_candles = fetch_gold_candles(
                    symbol, period=sub_period, interval=getattr(self.profile, "subbar_timeframe", "5m")
                )
                for sc in sub_candles:
                    d_str = str(sc["date"])
                    h_key = d_str[:13] + ":00"
                    subbars_by_hour[h_key].append({
                        "open": float(sc["open"]),
                        "high": float(sc["high"]),
                        "low": float(sc["low"]),
                        "close": float(sc["close"]),
                        "date": d_str,
                    })
            except Exception:
                pass

        return {
            "symbol": symbol,
            "candles": candles,
            "n": n,
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "opens": opens,
            "dates": dates,
            "ema50": ema50,
            "ema50_slopes": ema50_slopes,
            "atr14": atr14,
            "st": st,
            "dc10": dc10,
            "dc_channels": dc_channels,
            "adx": adx,
            "subbars_by_hour": subbars_by_hour,
        }

    def run(self, period: str = "2y") -> BacktestResult:
        """Run synchronized multi-asset backtest with strict production parity."""
        # Normalize periods (e.g. 6m -> 6mo)
        if period.lower() in ("6m", "3m", "1m"):
            period = period.lower() + "mo"

        # Load market data for all configured symbols
        market_data: Dict[str, Dict[str, Any]] = {}
        for s in self.profile.symbols:
            market_data[s] = self.load_market_data(s, period=period, interval="1h")

        # Map all unique timestamps across symbols chronologically
        date_set = set()
        for s, d in market_data.items():
            date_set.update(d["dates"])
        all_dates = sorted(list(date_set))

        # Build date to index lookup per symbol
        date_to_idx: Dict[str, Dict[str, int]] = {
            s: {d: idx for idx, d in enumerate(data["dates"])}
            for s, data in market_data.items()
        }

        # Initialize production-parity news guard
        news_guard = None
        if getattr(self.profile, "enable_news_guard", True):
            news_guard = BacktestNewsGuard(
                buffer_minutes=getattr(self.profile, "news_guard_buffer_minutes", 15)
            )

        # Chronological execution per-symbol matching daemon process architecture
        closed_trades: List[BacktestTrade] = []

        for s in self.profile.symbols:
            c_data = market_data[s]
            spec = ASSET_SPECS[s]
            n = c_data["n"]
            closes = c_data["closes"]
            highs = c_data["highs"]
            lows = c_data["lows"]
            opens = c_data["opens"]
            dates = c_data["dates"]
            ema50 = c_data["ema50"]
            ema_slopes = c_data["ema50_slopes"]
            atr14 = c_data["atr14"]
            st = c_data["st"]
            dc10 = c_data["dc10"]
            dc_curr = c_data.get("dc_channels", {}).get(
                getattr(self.profile, "donchian_period", 10), dc10
            )
            adx_vals = c_data["adx"]
            subbars_by_hour = c_data.get("subbars_by_hour", {})
            use_subbars = getattr(self.profile, "use_subbar_resolution", True) and bool(subbars_by_hour)
            dynamic_spreads = getattr(self.profile, "enable_dynamic_spreads", True)
            loss_cooldown = getattr(self.profile, "loss_cooldown_bars", 1)

            swap_daily = spec["swap_daily"]
            contract_size = spec["contract_size"]
            sl_atr_mult = getattr(self.profile, "sl_atr_mult", None) or spec["sl_atr_mult"]
            min_sl_dist = spec["min_sl_dist"]
            eff_scale_out_r = self.profile.scale_out_r if self.profile.scale_out_r is not None else spec.get("scale_out_r", 1.25)
            tsl_buffer = (spec.get("trail_buffer", self.profile.runner_trail_buffer)) * getattr(self.profile, "runner_trail_buffer_mult", 1.0)
            be_offset = spec["be_offset"]
            tp2_mult = self.profile.runner_target_2_multiplier
            adx_threshold = getattr(self.profile, "adx_min", None) if getattr(self.profile, "adx_min", None) is not None else spec.get("adx_min")

            active_trades: List[Dict[str, Any]] = []
            last_loss_exit_bar = -100
            daily_trade_counts: Dict[str, int] = defaultdict(int)

            for i in range(200, n - 48):
                h_curr, l_curr, o_curr, c_curr = highs[i], lows[i], opens[i], closes[i]
                dt_str = dates[i]
                day_str = dt_str[:10]
                subbars = subbars_by_hour.get(dt_str[:13] + ":00", []) if use_subbars else []

                # Dynamic friction for this bar / session
                _, _, cur_exit_friction = get_session_friction(s, dt_str, spec, dynamic_spreads)
                _, _, cur_entry_friction = get_session_friction(s, dt_str, spec, dynamic_spreads)

                if subbars:
                    # =========================================================================
                    # SUB-BAR (5M) HIGH-FIDELITY RESOLUTION: Micro-Loop Touch & Exact Intrabar Exits
                    # =========================================================================
                    for sub_b in subbars:
                        sb_o, sb_h, sb_l, sb_c = sub_b["open"], sub_b["high"], sub_b["low"], sub_b["close"]
                        sb_date = sub_b["date"]
                        _, _, sb_exit_fric = get_session_friction(s, sb_date, spec, dynamic_spreads)

                        # 1. Manage active trades within this sub-bar
                        surviving = []
                        for t in active_trades:
                            entry_bar = t["entry_bar"]
                            hours_held = i - entry_bar
                            swap_cost = (hours_held / 24.0) * swap_daily
                            sl_dist = t["sl_dist"]
                            eff_entry = t["eff_entry_fill"]
                            planned_risk = t["planned_risk_usd"]
                            lots = t["lots"]

                            if t["action"] == "BUY":
                                t["peak_price"] = max(t["peak_price"], sb_h)
                                if not t["stage1_closed"]:
                                    if sb_l <= t["current_sl"]:
                                        exit_fill = t["current_sl"] - sb_exit_fric
                                        net_pts = (exit_fill - eff_entry) - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="STOP_LOSS", net_pnl=pnl_usd, net_r=net_r
                                        ))
                                        last_loss_exit_bar = i
                                        continue
                                    if sb_h >= t["tp1_price"]:
                                        t["stage1_closed"] = True
                                        t["stage1_exit_fill"] = t["tp1_price"] - sb_exit_fric
                                        t["stage1_pts"] = 0.5 * (t["stage1_exit_fill"] - eff_entry)
                                        t["current_sl"] = max(t["current_sl"], t["entry_price"] + be_offset)
                                        trailed = max(t["entry_price"] + be_offset, t["peak_price"] - tsl_buffer)
                                        t["current_sl"] = max(t["current_sl"], trailed)
                                else:
                                    trailed = max(t["entry_price"] + be_offset, t["peak_price"] - tsl_buffer)
                                    t["current_sl"] = max(t["current_sl"], trailed)
                                    if sb_l <= t["current_sl"]:
                                        exit_fill = t["current_sl"] - sb_exit_fric
                                        stage2_pts = 0.5 * (exit_fill - eff_entry)
                                        net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="RUNNER_TRAILING_STOP", net_pnl=pnl_usd, net_r=net_r,
                                            scale_out_executed=True, is_runner=True
                                        ))
                                        continue
                                    if sb_h >= t["tp2_price"]:
                                        exit_fill = t["tp2_price"] - sb_exit_fric
                                        stage2_pts = 0.5 * (exit_fill - eff_entry)
                                        net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="TARGET_2_LIMIT", net_pnl=pnl_usd, net_r=net_r,
                                            scale_out_executed=True, is_runner=True
                                        ))
                                        continue
                                if hours_held >= 48:
                                    exit_fill = sb_c - sb_exit_fric
                                    net_pts = (exit_fill - eff_entry) - swap_cost if not t["stage1_closed"] else t["stage1_pts"] + 0.5 * (exit_fill - eff_entry) - swap_cost
                                    net_r = round(net_pts / sl_dist, 2)
                                    pnl_usd = round(net_r * planned_risk, 2)
                                    closed_trades.append(BacktestTrade(
                                        symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t["entry_date"],
                                        entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                        risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                        remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                        exit_reason="TIME_BARRIER", net_pnl=pnl_usd, net_r=net_r
                                    ))
                                    if net_r <= 0:
                                        last_loss_exit_bar = i
                                    continue
                                surviving.append(t)
                            else:  # SELL
                                t["peak_price"] = min(t["peak_price"], sb_l)
                                if not t["stage1_closed"]:
                                    if sb_h >= t["current_sl"]:
                                        exit_fill = t["current_sl"] + sb_exit_fric
                                        net_pts = (eff_entry - exit_fill) - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="STOP_LOSS", net_pnl=pnl_usd, net_r=net_r
                                        ))
                                        last_loss_exit_bar = i
                                        continue
                                    if sb_l <= t["tp1_price"]:
                                        t["stage1_closed"] = True
                                        t["stage1_exit_fill"] = t["tp1_price"] + sb_exit_fric
                                        t["stage1_pts"] = 0.5 * (eff_entry - t["stage1_exit_fill"])
                                        t["current_sl"] = min(t["current_sl"], t["entry_price"] - be_offset)
                                        trailed = min(t["entry_price"] - be_offset, t["peak_price"] + tsl_buffer)
                                        t["current_sl"] = min(t["current_sl"], trailed)
                                else:
                                    trailed = min(t["entry_price"] - be_offset, t["peak_price"] + tsl_buffer)
                                    t["current_sl"] = min(t["current_sl"], trailed)
                                    if sb_h >= t["current_sl"]:
                                        exit_fill = t["current_sl"] + sb_exit_fric
                                        stage2_pts = 0.5 * (eff_entry - exit_fill)
                                        net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="RUNNER_TRAILING_STOP", net_pnl=pnl_usd, net_r=net_r,
                                            scale_out_executed=True, is_runner=True
                                        ))
                                        continue
                                    if sb_l <= t["tp2_price"]:
                                        exit_fill = t["tp2_price"] + sb_exit_fric
                                        stage2_pts = 0.5 * (eff_entry - exit_fill)
                                        net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                        net_r = round(net_pts / sl_dist, 2)
                                        pnl_usd = round(net_r * planned_risk, 2)
                                        closed_trades.append(BacktestTrade(
                                            symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t["entry_date"],
                                            entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                            remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                            exit_reason="TARGET_2_LIMIT", net_pnl=pnl_usd, net_r=net_r,
                                            scale_out_executed=True, is_runner=True
                                        ))
                                        continue
                                if hours_held >= 48:
                                    exit_fill = sb_c + sb_exit_fric
                                    net_pts = (eff_entry - exit_fill) - swap_cost if not t["stage1_closed"] else t["stage1_pts"] + 0.5 * (eff_entry - exit_fill) - swap_cost
                                    net_r = round(net_pts / sl_dist, 2)
                                    pnl_usd = round(net_r * planned_risk, 2)
                                    closed_trades.append(BacktestTrade(
                                        symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t["entry_date"],
                                        entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                        risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                        remaining_volume=lots, exit_bar=i, exit_date=sb_date, exit_price=exit_fill,
                                        exit_reason="TIME_BARRIER", net_pnl=pnl_usd, net_r=net_r
                                    ))
                                    if net_r <= 0:
                                        last_loss_exit_bar = i
                                    continue
                                surviving.append(t)
                        active_trades = surviving

                    # 2. Check for NEW entry within bar i using 5M sub-bar resolution
                    prev_i = i - 1
                    if ema50[prev_i] is None or atr14[prev_i] is None or dc_curr["upper"][prev_i] is None:
                        continue

                    try:
                        time_part = dt_str.split(" ")[1] if " " in dt_str else dt_str.split("T")[1]
                        hour = int(time_part.split(":")[0])
                    except Exception:
                        hour = 12

                    session_filter = self.profile.enable_session_filter and spec["default_session_filter"]
                    if session_filter and not (7 <= hour <= 19):
                        continue

                    c_prev, trend_val, st_dir = closes[prev_i], ema50[prev_i], st["direction"][prev_i]
                    prev_d_up, prev_d_low = dc_curr["upper"][prev_i], dc_curr["lower"][prev_i]
                    slope_val = ema_slopes[prev_i]
                    adx_val = adx_vals[prev_i] if (prev_i < len(adx_vals) and adx_vals[prev_i] is not None) else 20.0
                    h_prev, l_prev, o_prev = highs[prev_i], lows[prev_i], opens[prev_i]

                    macro_bull = c_prev > trend_val
                    macro_bear = c_prev < trend_val
                    if self.profile.require_slope_alignment:
                        if macro_bull and slope_val < 0.0:
                            macro_bull = False
                        if macro_bear and slope_val > 0.0:
                            macro_bear = False

                    if adx_threshold is not None and adx_val < adx_threshold:
                        continue

                    min_body = max(1.0, 0.15 * atr14[prev_i])
                    prev_body_bull = max(c_prev - o_prev, 0.0)
                    prev_upper_wick = h_prev - max(c_prev, o_prev)
                    if prev_upper_wick > 1.8 * max(prev_body_bull, min_body):
                        macro_bull = False

                    prev_body_bear = max(o_prev - c_prev, 0.0)
                    prev_lower_wick = min(c_prev, o_prev) - l_prev
                    if prev_lower_wick > 1.8 * max(prev_body_bear, min_body):
                        macro_bear = False

                    if i - last_loss_exit_bar < loss_cooldown:
                        continue

                    if daily_trade_counts[day_str] >= self.profile.max_daily_trades:
                        continue

                    if news_guard:
                        in_news, _ = news_guard.is_in_news_window(dt_str)
                        if in_news:
                            continue

                    if len(active_trades) >= self.profile.max_positions:
                        continue

                    unhedged = [t for t in active_trades if not t["stage1_closed"]]
                    if len(unhedged) >= self.profile.max_unhedged_positions:
                        continue

                    # Scan 5M subbars for the touch trigger
                    for sub_idx, sub_b in enumerate(subbars):
                        sb_o, sb_h, sb_l, sb_c = sub_b["open"], sub_b["high"], sub_b["low"], sub_b["close"]
                        sb_date = sub_b["date"]

                        is_bull = (sb_h >= prev_d_up) and (st_dir == 1) and macro_bull
                        is_bear = (sb_l <= prev_d_low) and (st_dir == -1) and macro_bear
                        if not (is_bull or is_bear):
                            continue

                        action = "BUY" if is_bull else "SELL"
                        eff_entry = max(sb_o, prev_d_up) if action == "BUY" else min(sb_o, prev_d_low)
                        sl_dist = max(sl_atr_mult * atr14[prev_i], min_sl_dist)
                        tp1_dist = eff_scale_out_r * sl_dist
                        tp2_dist = tp2_mult * sl_dist

                        planned_risk = self.profile.calculate_trade_risk_usd(self.profile.starting_balance)
                        lots = round(planned_risk / (sl_dist * contract_size), 2)
                        lots = max(spec["min_lot"], lots)

                        initial_sl = eff_entry - sl_dist if action == "BUY" else eff_entry + sl_dist
                        _, _, sb_entry_fric = get_session_friction(s, sb_date, spec, dynamic_spreads)

                        new_trade = {
                            "entry_bar": i,
                            "action": action,
                            "entry_price": eff_entry,
                            "eff_entry_fill": eff_entry + (sb_entry_fric if action == "BUY" else -sb_entry_fric),
                            "entry_date": sb_date,
                            "sl_dist": sl_dist,
                            "initial_sl": initial_sl,
                            "current_sl": initial_sl,
                            "tp1_price": eff_entry + tp1_dist if action == "BUY" else eff_entry - tp1_dist,
                            "tp2_price": eff_entry + tp2_dist if action == "BUY" else eff_entry - tp2_dist,
                            "peak_price": eff_entry,
                            "stage1_closed": False,
                            "stage1_pts": 0.0,
                            "planned_risk_usd": planned_risk,
                            "lots": lots,
                        }
                        daily_trade_counts[day_str] += 1
                        active_trades.append(new_trade)
                        break
                    continue

                # =========================================================================
                # 1H DISCRETE FALLBACK (used when 5M subbars are not available)
                # =========================================================================
                # 1. Update and manage active open trades on bar i
                surviving_trades: List[Dict[str, Any]] = []
                for t in active_trades:
                    entry_bar = t["entry_bar"]
                    hours_held = i - entry_bar
                    swap_cost = (hours_held / 24.0) * swap_daily
                    sl_dist = t["sl_dist"]
                    eff_entry = t["eff_entry_fill"]
                    planned_risk = t["planned_risk_usd"]
                    lots = t["lots"]

                    if t["action"] == "BUY":
                        t["peak_price"] = max(t["peak_price"], h_curr)
                        if not t["stage1_closed"]:
                            if l_curr <= t["current_sl"]:
                                exit_fill = t["current_sl"] - cur_exit_friction
                                net_pts = (exit_fill - eff_entry) - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="STOP_LOSS", net_pnl=pnl_usd, net_r=net_r
                                ))
                                last_loss_exit_bar = i
                                continue
                            if h_curr >= t["tp1_price"]:
                                t["stage1_closed"] = True
                                t["stage1_exit_fill"] = t["tp1_price"] - cur_exit_friction
                                t["stage1_pts"] = 0.5 * (t["stage1_exit_fill"] - eff_entry)
                                t["current_sl"] = max(t["current_sl"], t["entry_price"] + be_offset)
                                trailed = max(t["entry_price"] + be_offset, t["peak_price"] - tsl_buffer)
                                t["current_sl"] = max(t["current_sl"], trailed)
                        else:
                            trailed = max(t["entry_price"] + be_offset, t["peak_price"] - tsl_buffer)
                            t["current_sl"] = max(t["current_sl"], trailed)
                            if l_curr <= t["current_sl"]:
                                exit_fill = t["current_sl"] - cur_exit_friction
                                stage2_pts = 0.5 * (exit_fill - eff_entry)
                                net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="RUNNER_TRAILING_STOP", net_pnl=pnl_usd, net_r=net_r,
                                    scale_out_executed=True, is_runner=True
                                ))
                                continue
                            if h_curr >= t["tp2_price"]:
                                exit_fill = t["tp2_price"] - cur_exit_friction
                                stage2_pts = 0.5 * (exit_fill - eff_entry)
                                net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="TARGET_2_LIMIT", net_pnl=pnl_usd, net_r=net_r,
                                    scale_out_executed=True, is_runner=True
                                ))
                                continue

                        if hours_held >= 48:
                            exit_fill = c_curr - cur_exit_friction
                            if not t["stage1_closed"]:
                                net_pts = (exit_fill - eff_entry) - swap_cost
                            else:
                                net_pts = t["stage1_pts"] + 0.5 * (exit_fill - eff_entry) - swap_cost
                            net_r = round(net_pts / sl_dist, 2)
                            pnl_usd = round(net_r * planned_risk, 2)
                            closed_trades.append(BacktestTrade(
                                symbol=s, direction="BUY", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                exit_reason="TIME_BARRIER", net_pnl=pnl_usd, net_r=net_r
                            ))
                            if net_r <= 0:
                                last_loss_exit_bar = i
                            continue
                        surviving_trades.append(t)
                    else:  # SELL
                        t["peak_price"] = min(t["peak_price"], l_curr)
                        if not t["stage1_closed"]:
                            if h_curr >= t["current_sl"]:
                                exit_fill = t["current_sl"] + cur_exit_friction
                                net_pts = (eff_entry - exit_fill) - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="STOP_LOSS", net_pnl=pnl_usd, net_r=net_r
                                ))
                                last_loss_exit_bar = i
                                continue
                            if l_curr <= t["tp1_price"]:
                                t["stage1_closed"] = True
                                t["stage1_exit_fill"] = t["tp1_price"] + cur_exit_friction
                                t["stage1_pts"] = 0.5 * (eff_entry - t["stage1_exit_fill"])
                                t["current_sl"] = min(t["current_sl"], t["entry_price"] - be_offset)
                                trailed = min(t["entry_price"] - be_offset, t["peak_price"] + tsl_buffer)
                                t["current_sl"] = min(t["current_sl"], trailed)
                        else:
                            trailed = min(t["entry_price"] - be_offset, t["peak_price"] + tsl_buffer)
                            t["current_sl"] = min(t["current_sl"], trailed)
                            if h_curr >= t["current_sl"]:
                                exit_fill = t["current_sl"] + cur_exit_friction
                                stage2_pts = 0.5 * (eff_entry - exit_fill)
                                net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="RUNNER_TRAILING_STOP", net_pnl=pnl_usd, net_r=net_r,
                                    scale_out_executed=True, is_runner=True
                                ))
                                continue
                            if l_curr <= t["tp2_price"]:
                                exit_fill = t["tp2_price"] + cur_exit_friction
                                stage2_pts = 0.5 * (eff_entry - exit_fill)
                                net_pts = t["stage1_pts"] + stage2_pts - swap_cost
                                net_r = round(net_pts / sl_dist, 2)
                                pnl_usd = round(net_r * planned_risk, 2)
                                closed_trades.append(BacktestTrade(
                                    symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                    entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                    risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                    remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                    exit_reason="TARGET_2_LIMIT", net_pnl=pnl_usd, net_r=net_r,
                                    scale_out_executed=True, is_runner=True
                                ))
                                continue

                        if hours_held >= 48:
                            exit_fill = c_curr + cur_exit_friction
                            if not t["stage1_closed"]:
                                net_pts = (eff_entry - exit_fill) - swap_cost
                            else:
                                net_pts = t["stage1_pts"] + 0.5 * (eff_entry - exit_fill) - swap_cost
                            net_r = round(net_pts / sl_dist, 2)
                            pnl_usd = round(net_r * planned_risk, 2)
                            closed_trades.append(BacktestTrade(
                                symbol=s, direction="SELL", entry_bar=entry_bar, entry_date=t.get("entry_date", dates[entry_bar]),
                                entry_price=t["entry_price"], initial_sl=t["initial_sl"], current_sl=t["current_sl"],
                                risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                                remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                                exit_reason="TIME_BARRIER", net_pnl=pnl_usd, net_r=net_r
                            ))
                            if net_r <= 0:
                                last_loss_exit_bar = i
                            continue
                        surviving_trades.append(t)

                active_trades = surviving_trades

                # 2. Check for NEW entry trigger on bar i
                prev_i = i - 1
                if ema50[prev_i] is None or atr14[prev_i] is None or dc_curr["upper"][prev_i] is None:
                    continue

                try:
                    time_part = dt_str.split(" ")[1] if " " in dt_str else dt_str.split("T")[1]
                    hour = int(time_part.split(":")[0])
                except Exception:
                    hour = 12

                session_filter = self.profile.enable_session_filter and spec["default_session_filter"]
                if session_filter and not (7 <= hour <= 19):
                    continue

                c_prev, trend_val, st_dir = closes[prev_i], ema50[prev_i], st["direction"][prev_i]
                prev_d_up, prev_d_low = dc_curr["upper"][prev_i], dc_curr["lower"][prev_i]
                slope_val = ema_slopes[prev_i]
                adx_val = adx_vals[prev_i] if (prev_i < len(adx_vals) and adx_vals[prev_i] is not None) else 20.0
                h_prev, l_prev, o_prev = highs[prev_i], lows[prev_i], opens[prev_i]

                macro_bull = c_prev > trend_val
                macro_bear = c_prev < trend_val
                if self.profile.require_slope_alignment:
                    if macro_bull and slope_val < 0.0:
                        macro_bull = False
                    if macro_bear and slope_val > 0.0:
                        macro_bear = False

                if adx_threshold is not None and adx_val < adx_threshold:
                    continue

                min_body = max(1.0, 0.15 * atr14[prev_i])
                prev_body_bull = max(c_prev - o_prev, 0.0)
                prev_upper_wick = h_prev - max(c_prev, o_prev)
                if prev_upper_wick > 1.8 * max(prev_body_bull, min_body):
                    macro_bull = False

                prev_body_bear = max(o_prev - c_prev, 0.0)
                prev_lower_wick = min(c_prev, o_prev) - l_prev
                if prev_lower_wick > 1.8 * max(prev_body_bear, min_body):
                    macro_bear = False

                is_bull = (h_curr >= prev_d_up) and (st_dir == 1) and macro_bull
                is_bear = (l_curr <= prev_d_low) and (st_dir == -1) and macro_bear
                if not (is_bull or is_bear):
                    continue

                if i - last_loss_exit_bar < loss_cooldown:
                    continue

                if daily_trade_counts[day_str] >= self.profile.max_daily_trades:
                    continue

                if news_guard:
                    in_news, _ = news_guard.is_in_news_window(dt_str)
                    if in_news:
                        continue

                if len(active_trades) >= self.profile.max_positions:
                    continue

                unhedged = [t for t in active_trades if not t["stage1_closed"]]
                if len(unhedged) >= self.profile.max_unhedged_positions:
                    continue

                action = "BUY" if is_bull else "SELL"
                eff_entry = max(o_curr, prev_d_up) if action == "BUY" else min(o_curr, prev_d_low)
                sl_dist = max(sl_atr_mult * atr14[prev_i], min_sl_dist)
                tp1_dist = eff_scale_out_r * sl_dist
                tp2_dist = tp2_mult * sl_dist

                planned_risk = self.profile.calculate_trade_risk_usd(self.profile.starting_balance)
                lots = round(planned_risk / (sl_dist * contract_size), 2)
                lots = max(spec["min_lot"], lots)

                initial_sl = eff_entry - sl_dist if action == "BUY" else eff_entry + sl_dist

                new_trade = {
                    "entry_bar": i,
                    "action": action,
                    "entry_price": eff_entry,
                    "eff_entry_fill": eff_entry + (cur_entry_friction if action == "BUY" else -cur_entry_friction),
                    "entry_date": dates[i],
                    "sl_dist": sl_dist,
                    "initial_sl": initial_sl,
                    "current_sl": initial_sl,
                    "tp1_price": eff_entry + tp1_dist if action == "BUY" else eff_entry - tp1_dist,
                    "tp2_price": eff_entry + tp2_dist if action == "BUY" else eff_entry - tp2_dist,
                    "peak_price": eff_entry,
                    "stage1_closed": False,
                    "stage1_pts": 0.0,
                    "planned_risk_usd": planned_risk,
                    "lots": lots,
                }
                daily_trade_counts[day_str] += 1

                # Check same-bar adverse excursion priority
                if action == "BUY":
                    if l_curr <= new_trade["current_sl"]:
                        exit_fill = new_trade["current_sl"] - cur_exit_friction
                        net_pts = exit_fill - new_trade["eff_entry_fill"]
                        net_r = round(net_pts / sl_dist, 2)
                        pnl_usd = round(net_r * planned_risk, 2)
                        closed_trades.append(BacktestTrade(
                            symbol=s, direction="BUY", entry_bar=i, entry_date=dates[i],
                            entry_price=eff_entry, initial_sl=initial_sl, current_sl=initial_sl,
                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                            remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                            exit_reason="SAME_BAR_SL", net_pnl=pnl_usd, net_r=net_r
                        ))
                        last_loss_exit_bar = i
                        continue
                    elif h_curr >= new_trade["tp1_price"]:
                        new_trade["stage1_closed"] = True
                        new_trade["stage1_pts"] = 0.5 * (new_trade["tp1_price"] - cur_exit_friction - new_trade["eff_entry_fill"])
                        new_trade["current_sl"] = max(new_trade["current_sl"], eff_entry + be_offset)
                        new_trade["peak_price"] = h_curr
                        active_trades.append(new_trade)
                    else:
                        active_trades.append(new_trade)
                else:  # SELL
                    if h_curr >= new_trade["current_sl"]:
                        exit_fill = new_trade["current_sl"] + cur_exit_friction
                        net_pts = new_trade["eff_entry_fill"] - exit_fill
                        net_r = round(net_pts / sl_dist, 2)
                        pnl_usd = round(net_r * planned_risk, 2)
                        closed_trades.append(BacktestTrade(
                            symbol=s, direction="SELL", entry_bar=i, entry_date=dates[i],
                            entry_price=eff_entry, initial_sl=initial_sl, current_sl=initial_sl,
                            risk_distance=sl_dist, planned_risk_usd=planned_risk, initial_volume=lots,
                            remaining_volume=lots, exit_bar=i, exit_date=dates[i], exit_price=exit_fill,
                            exit_reason="SAME_BAR_SL", net_pnl=pnl_usd, net_r=net_r
                        ))
                        last_loss_exit_bar = i
                        continue
                    elif l_curr <= new_trade["tp1_price"]:
                        new_trade["stage1_closed"] = True
                        new_trade["stage1_pts"] = 0.5 * (new_trade["eff_entry_fill"] - (new_trade["tp1_price"] + cur_exit_friction))
                        new_trade["current_sl"] = min(new_trade["current_sl"], eff_entry - be_offset)
                        new_trade["peak_price"] = l_curr
                        active_trades.append(new_trade)
                    else:
                        active_trades.append(new_trade)

        # Sort all completed trades chronologically by exit_date
        closed_trades.sort(key=lambda t: t.exit_date)
        current_equity = self.profile.starting_balance
        for t in closed_trades:
            current_equity += t.net_pnl
            self.auditor.process_bar(t.exit_date, current_equity)

        # -------------------------------------------------------------
        # COMPUTE PERFORMANCE METRICS
        # -------------------------------------------------------------
        total_trades = len(closed_trades)
        wins = [t for t in closed_trades if t.net_pnl > 0]
        losses = [t for t in closed_trades if t.net_pnl <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        profit_factor = round(gross_profit / max(1.0, gross_loss), 2)

        net_profit_usd = round(sum(t.net_pnl for t in closed_trades), 2)
        net_r = round(sum(t.net_r for t in closed_trades), 1)
        return_pct = round((net_profit_usd / self.profile.starting_balance) * 100.0, 2)

        avg_win = (gross_profit / win_count) if win_count > 0 else 0.0
        avg_loss = (gross_loss / loss_count) if loss_count > 0 else 0.0
        payoff_ratio = round(avg_win / max(0.01, avg_loss), 2)

        # Compute Monthly Breakdown
        monthly_groups: Dict[str, List[BacktestTrade]] = defaultdict(list)
        for t in closed_trades:
            m = t.exit_date[:7] if t.exit_date else "Unknown"
            monthly_groups[m].append(t)

        monthly_breakdown: Dict[str, Dict[str, Any]] = {}
        for m, m_trades in sorted(monthly_groups.items()):
            m_wins = [t for t in m_trades if t.net_pnl > 0]
            m_pnl = sum(t.net_pnl for t in m_trades)
            monthly_breakdown[m] = {
                "trades": len(m_trades),
                "win_rate": round(len(m_wins) / len(m_trades) * 100.0, 1),
                "pnl_usd": round(m_pnl, 2),
                "net_r": round(sum(t.net_r for t in m_trades), 1),
            }

        # Deflated Sharpe Ratio
        trade_returns = [t.net_r for t in closed_trades]
        dsr = compute_deflated_sharpe_ratio(trade_returns)
        sharpe = round((np.mean(trade_returns) / max(1e-6, np.std(trade_returns))) * math.sqrt(252), 2) if len(trade_returns) > 5 else 0.0

        # Compliance Audit Finalization
        compliance = self.auditor.finalize(current_equity)

        # Convert trade objects to serializable dicts
        serializable_trades = [
            {
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_date": t.entry_date,
                "entry_price": t.entry_price,
                "exit_date": t.exit_date,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "net_pnl": t.net_pnl,
                "net_r": t.net_r,
                "volume": t.initial_volume,
            }
            for t in closed_trades
        ]

        return BacktestResult(
            profile=self.profile,
            period=period,
            start_date=all_dates[0],
            end_date=all_dates[-1],
            initial_capital=self.profile.starting_balance,
            final_equity=round(current_equity, 2),
            net_profit_usd=net_profit_usd,
            return_pct=return_pct,
            total_trades=total_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate_pct=round(win_rate, 2),
            profit_factor=profit_factor,
            net_r=net_r,
            payoff_ratio=payoff_ratio,
            max_drawdown_pct=compliance.max_overall_drawdown_pct,
            max_drawdown_usd=compliance.max_overall_drawdown_usd,
            sharpe_ratio=sharpe,
            deflated_sharpe_ratio=round(dsr, 3),
            compliance=compliance,
            trades=serializable_trades,
            monthly_breakdown=monthly_breakdown,
        )
