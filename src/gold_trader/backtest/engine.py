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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

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

# Standard asset properties & default institutional frictions
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
    },
}


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
        adx_dict = calc_adx(highs, lows, closes, 14)
        adx = adx_dict.get("adx", [20.0] * n)

        # 50 EMA Slope
        ema50_slopes = [0.0] * n
        for i in range(6, n):
            if ema50[i] is not None and ema50[i - 5] is not None and ema50[i - 5] > 0:
                ema50_slopes[i] = ((ema50[i] - ema50[i - 5]) / ema50[i - 5]) * 100.0

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
            "adx": adx,
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

        current_equity = self.profile.starting_balance
        closed_trades: List[BacktestTrade] = []
        open_positions: List[BacktestTrade] = []

        # Chronological bar-by-bar execution loop
        for date_str in all_dates:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M") if len(date_str) == 16 else datetime.strptime(date_str, "%Y-%m-%d")
            hour = dt.hour
            is_new_day = (hour == 0 and dt.minute == 0)

            # Inform auditor of new bar
            self.auditor.process_bar(date_str, current_equity)

            # -------------------------------------------------------------
            # 1. EVALUATE & MANAGE OPEN POSITIONS
            # -------------------------------------------------------------
            retained_positions: List[BacktestTrade] = []
            for pos in open_positions:
                s = pos.symbol
                if date_str not in date_to_idx[s]:
                    retained_positions.append(pos)
                    continue

                idx = date_to_idx[s][date_str]
                c_data = market_data[s]
                h_bar = c_data["highs"][idx]
                l_bar = c_data["lows"][idx]
                c_bar = c_data["closes"][idx]
                spec = ASSET_SPECS[s]
                contract_size = spec["contract_size"]

                # Apply overnight swaps at 00:00 UTC
                if is_new_day:
                    swap_cost = spec["swap_daily"] * pos.remaining_volume
                    pos.swaps += swap_cost
                    current_equity -= swap_cost

                # Same-Bar Adverse Excursion Priority (Adverse stop checked first)
                hit_sl = False
                exit_price = pos.current_sl
                if pos.direction == "BUY":
                    if l_bar <= pos.current_sl:
                        hit_sl = True
                        exit_price = pos.current_sl - spec["slippage"]
                else:  # SELL
                    if h_bar >= pos.current_sl:
                        hit_sl = True
                        exit_price = pos.current_sl + spec["slippage"]

                if hit_sl:
                    # Position closed at stop loss
                    pos.exit_bar = idx
                    pos.exit_date = date_str
                    pos.exit_price = exit_price
                    pos.exit_reason = "STOP_LOSS" if not pos.is_runner else "RUNNER_TRAILING_STOP"

                    diff = (exit_price - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - exit_price)
                    remaining_pnl = diff * contract_size * pos.remaining_volume
                    pos.net_pnl = round(pos.scale_out_pnl + remaining_pnl - pos.commission - pos.swaps, 2)
                    pos.net_r = round(pos.net_pnl / max(0.01, pos.planned_risk_usd), 2)
                    current_equity += remaining_pnl
                    closed_trades.append(pos)
                    continue

                # ---------------------------------------------------------
                # Two-Stage Execution: Scale-Out & Runner Trailing Stop
                # ---------------------------------------------------------
                r_dist = pos.risk_distance
                if not pos.scale_out_executed:
                    # Target 1 Price (+scale_out_r)
                    target_1 = (pos.entry_price + (self.profile.scale_out_r * r_dist)) if pos.direction == "BUY" else (pos.entry_price - (self.profile.scale_out_r * r_dist))
                    t1_reached = (h_bar >= target_1) if pos.direction == "BUY" else (l_bar <= target_1)

                    if t1_reached and pos.initial_volume >= (spec["min_lot"] * 2):
                        # Close 50% volume into realized cash
                        scale_vol = round(pos.initial_volume * 0.5, 2)
                        pos.remaining_volume = round(pos.initial_volume - scale_vol, 2)
                        pos.scale_out_executed = True
                        pos.scale_out_price = target_1
                        pos.scale_out_date = date_str

                        scale_diff = (target_1 - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - target_1)
                        pos.scale_out_pnl = scale_diff * contract_size * scale_vol
                        current_equity += pos.scale_out_pnl

                        # Lock remaining stop at Break-Even + offset
                        pos.is_runner = True
                        pos.current_sl = (pos.entry_price + spec["be_offset"]) if pos.direction == "BUY" else (pos.entry_price - spec["be_offset"])
                        pos.peak_excursion = target_1

                # If position is a runner, update peak excursion and trail stop to Target 2
                if pos.is_runner:
                    trail_buffer = self.profile.runner_trail_buffer
                    target_2 = (pos.entry_price + (self.profile.runner_target_2_multiplier * r_dist)) if pos.direction == "BUY" else (pos.entry_price - (self.profile.runner_target_2_multiplier * r_dist))

                    # Check Target 2 limit fill
                    t2_reached = (h_bar >= target_2) if pos.direction == "BUY" else (l_bar <= target_2)
                    if t2_reached:
                        pos.exit_bar = idx
                        pos.exit_date = date_str
                        pos.exit_price = target_2
                        pos.exit_reason = "TARGET_2_LIMIT"
                        diff = (target_2 - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - target_2)
                        runner_pnl = diff * contract_size * pos.remaining_volume
                        pos.net_pnl = round(pos.scale_out_pnl + runner_pnl - pos.commission - pos.swaps, 2)
                        pos.net_r = round(pos.net_pnl / max(0.01, pos.planned_risk_usd), 2)
                        current_equity += runner_pnl
                        closed_trades.append(pos)
                        continue

                    # Update trailing stop behind peak excursion
                    if pos.direction == "BUY":
                        if h_bar > pos.peak_excursion:
                            pos.peak_excursion = h_bar
                        new_sl = pos.peak_excursion - trail_buffer
                        if new_sl > pos.current_sl:
                            pos.current_sl = new_sl
                    else:  # SELL
                        if l_bar < pos.peak_excursion:
                            pos.peak_excursion = l_bar
                        new_sl = pos.peak_excursion + trail_buffer
                        if new_sl < pos.current_sl:
                            pos.current_sl = new_sl

                retained_positions.append(pos)

            open_positions = retained_positions

            # -------------------------------------------------------------
            # 2. SCAN & EXECUTE NEW TRADES
            # -------------------------------------------------------------
            # Skip if auditor has halted trading for the day (Daily Circuit Breaker)
            if self.auditor.is_trading_halted_today():
                continue

            # Concurrency limit checks
            if len(open_positions) >= self.profile.max_positions:
                continue

            # Unhedged downside risk check (Sweet Spot concurrency)
            if self.profile.allow_runner_compounding:
                unhedged_count = sum(
                    1 for p in open_positions
                    if (p.direction == "BUY" and p.current_sl < p.entry_price)
                    or (p.direction == "SELL" and p.current_sl > p.entry_price)
                )
                if unhedged_count >= self.profile.max_unhedged_positions:
                    continue

            # Evaluate candidate signals across symbols
            for s in self.profile.symbols:
                if date_str not in date_to_idx[s]:
                    continue

                idx = date_to_idx[s][date_str]
                if idx < 200:  # Need 200 bars warm-up
                    continue

                c_data = market_data[s]
                spec = ASSET_SPECS[s]
                prev_i = idx - 1  # Strictly evaluate signal bar on completed closed bar i-1

                # Indicators on bar i-1
                c_prev = c_data["closes"][prev_i]
                o_prev = c_data["opens"][prev_i]
                h_prev = c_data["highs"][prev_i]
                l_prev = c_data["lows"][prev_i]
                st_prev = c_data["st"]["direction"][prev_i]
                dc_upper_prev = c_data["dc10"]["upper"][prev_i]
                dc_lower_prev = c_data["dc10"]["lower"][prev_i]
                atr_prev = c_data["atr14"][prev_i]
                ema_slope = c_data["ema50_slopes"][prev_i]
                adx_val = c_data["adx"][prev_i]

                if atr_prev is None or dc_upper_prev is None or st_prev is None:
                    continue

                # Session filter: London/NY overlap (07:00-19:00 UTC)
                session_filter = self.profile.enable_session_filter and spec["default_session_filter"]
                if session_filter and not (7 <= hour <= 19):
                    continue

                # ADX filter
                if spec["adx_min"] is not None and adx_val < spec["adx_min"]:
                    continue

                # Candle & Rejection Wick Confirmation
                body = abs(c_prev - o_prev)
                upper_wick = h_prev - max(o_prev, c_prev)
                lower_wick = min(o_prev, c_prev) - l_prev
                wick_threshold = 1.8 * max(body, 0.15 * atr_prev)

                h_curr = c_data["highs"][idx]
                l_curr = c_data["lows"][idx]
                o_curr = c_data["opens"][idx]

                # ---------------- BUY SETUP ----------------
                buy_valid = (
                    st_prev == 1
                    and (not self.profile.require_slope_alignment or ema_slope > 0)
                    and upper_wick <= wick_threshold
                    and c_prev >= o_prev  # Bullish candle
                    and h_curr >= dc_upper_prev  # Touch intrabar
                )

                if buy_valid:
                    entry_price = max(o_curr, dc_upper_prev) + spec["spread"]
                    sl_dist = max(spec["sl_atr_mult"] * atr_prev, spec["min_sl_dist"])
                    initial_sl = entry_price - sl_dist
                    planned_risk = self.profile.calculate_trade_risk_usd(current_equity)
                    lots = round(planned_risk / (sl_dist * spec["contract_size"]), 2)
                    lots = max(spec["min_lot"], lots)
                    commission = round(lots * self.profile.commission_per_lot, 2)

                    pos = BacktestTrade(
                        symbol=s,
                        direction="BUY",
                        entry_bar=idx,
                        entry_date=date_str,
                        entry_price=entry_price,
                        initial_sl=initial_sl,
                        current_sl=initial_sl,
                        risk_distance=sl_dist,
                        planned_risk_usd=planned_risk,
                        initial_volume=lots,
                        remaining_volume=lots,
                        commission=commission,
                    )
                    open_positions.append(pos)
                    current_equity -= commission
                    continue

                # ---------------- SELL SETUP ----------------
                sell_valid = (
                    st_prev == -1
                    and (not self.profile.require_slope_alignment or ema_slope < 0)
                    and lower_wick <= wick_threshold
                    and c_prev <= o_prev  # Bearish candle
                    and l_curr <= dc_lower_prev  # Touch intrabar
                )

                if sell_valid:
                    entry_price = min(o_curr, dc_lower_prev) - spec["spread"]
                    sl_dist = max(spec["sl_atr_mult"] * atr_prev, spec["min_sl_dist"])
                    initial_sl = entry_price + sl_dist
                    planned_risk = self.profile.calculate_trade_risk_usd(current_equity)
                    lots = round(planned_risk / (sl_dist * spec["contract_size"]), 2)
                    lots = max(spec["min_lot"], lots)
                    commission = round(lots * self.profile.commission_per_lot, 2)

                    pos = BacktestTrade(
                        symbol=s,
                        direction="SELL",
                        entry_bar=idx,
                        entry_date=date_str,
                        entry_price=entry_price,
                        initial_sl=initial_sl,
                        current_sl=initial_sl,
                        risk_distance=sl_dist,
                        planned_risk_usd=planned_risk,
                        initial_volume=lots,
                        remaining_volume=lots,
                        commission=commission,
                    )
                    open_positions.append(pos)
                    current_equity -= commission
                    continue

        # Close any lingering positions at final candle close
        for pos in open_positions:
            s = pos.symbol
            idx = len(market_data[s]["closes"]) - 1
            last_date = market_data[s]["dates"][-1]
            c_bar = market_data[s]["closes"][idx]
            diff = (c_bar - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - c_bar)
            pnl = diff * ASSET_SPECS[s]["contract_size"] * pos.remaining_volume
            pos.exit_bar = idx
            pos.exit_date = last_date
            pos.exit_price = c_bar
            pos.exit_reason = "HORIZON_END"
            pos.net_pnl = round(pos.scale_out_pnl + pnl - pos.commission - pos.swaps, 2)
            pos.net_r = round(pos.net_pnl / max(0.01, pos.planned_risk_usd), 2)
            current_equity += pnl
            closed_trades.append(pos)

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
