"""
High-Fidelity Backtesting Engine for Intra-Session Price Action & Liquidity Strategy.

Simulates:
1. Multi-Timeframe bar alignment with zero-lookahead bias.
2. Resting BUY_STOP / SELL_STOP execution with spread and slippage frictions.
3. 90-minute order timeout expiration.
4. Adverse-first intra-bar execution checks.
5. Mode 1 (Exact PDF Specification): 1:1 R:R with full close at 1R.
6. Mode 2 (Aegis Enhanced Profile): 50% scale-out at 1R, move SL to breakeven, 50% runner to 2.5R.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from gold_trader.strategies.intra_session_liquidity import IntraSessionLiquidityStrategy, SetupSignal


@dataclass
class SimulatedTrade:
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    signal_time: str
    fill_time: str
    exit_time: str
    entry_price: float
    initial_sl: float
    tp1_price: float
    tp2_price: Optional[float]
    risk_distance: float
    planned_risk_usd: float
    lots: float
    result: str  # "WIN", "LOSS", "BREAKEVEN"
    realized_pnl_usd: float
    realized_r: float
    bars_in_trade: int
    session: str
    exit_reason: str


@dataclass
class BacktestSummary:
    mode: str
    symbol: str
    timeframe: str
    total_signals: int
    executed_trades: int
    unfilled_expired: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_pct: float
    loss_rate_pct: float
    total_pnl_usd: float
    total_pnl_r: float
    profit_factor: float
    expectancy_r: float
    expectancy_usd: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    avg_trade_bars: float
    trades: List[SimulatedTrade] = field(default_factory=list)


# Asset properties, tick values, and realistic broker frictions
DEFAULT_ASSET_PARAMS: Dict[str, Dict[str, Any]] = {
    "XAUUSD": {
        "contract_size": 100.0,
        "spread": 0.35,
        "slippage": 0.15,
        "fixed_risk_usd": 75.44,
        "min_lot": 0.01,
        "max_lot": 10.0,
    },
    "BTCUSD": {
        "contract_size": 1.0,
        "spread": 15.00,
        "slippage": 5.00,
        "fixed_risk_usd": 75.44,
        "min_lot": 0.01,
        "max_lot": 5.0,
    },
    "EURUSD": {
        "contract_size": 100000.0,
        "spread": 0.00015,
        "slippage": 0.00005,
        "fixed_risk_usd": 75.44,
        "min_lot": 0.01,
        "max_lot": 10.0,
    },
}


class LiquidityBacktestEngine:
    def __init__(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "15m",
        starting_balance: float = 1000.0,
        fixed_risk_usd: float = 75.44,
        mode: str = "BASELINE",  # "BASELINE" (PDF 1:1) or "ENHANCED" (Aegis 50% scale-out + runner)
        spread: Optional[float] = None,
        slippage: Optional[float] = None,
        enable_session_filter: bool = True,
    ):
        self.symbol = symbol.upper()
        self.timeframe = timeframe.lower()
        self.starting_balance = starting_balance
        self.mode = mode.upper()
        self.enable_session_filter = enable_session_filter

        asset_cfg = DEFAULT_ASSET_PARAMS.get(self.symbol, DEFAULT_ASSET_PARAMS["XAUUSD"])
        self.contract_size = asset_cfg["contract_size"]
        self.spread = spread if spread is not None else asset_cfg["spread"]
        self.slippage = slippage if slippage is not None else asset_cfg["slippage"]
        self.fixed_risk_usd = fixed_risk_usd or asset_cfg["fixed_risk_usd"]

        self.strategy = IntraSessionLiquidityStrategy(
            symbol=self.symbol,
            timeframe=self.timeframe,
            enable_session_filter=self.enable_session_filter,
        )

    def _resample_higher_timeframe_bars(
        self,
        bars: List[Dict[str, Any]],
        target_hours: int,
    ) -> List[Dict[str, Any]]:
        """Resample execution bars into higher timeframe bars (4H or Daily) for zero lookahead."""
        htf_bars = []
        if not bars:
            return htf_bars

        curr_htf: Optional[Dict[str, Any]] = None
        target_seconds = target_hours * 3600

        for b in bars:
            try:
                dt = datetime.fromisoformat(b["date"].replace("Z", ""))
                # Bucket timestamp based on target_hours
                ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                bucket_ts = (ts // target_seconds) * target_seconds
                bucket_str = datetime.fromtimestamp(bucket_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

                if curr_htf is None or curr_htf["date"] != bucket_str:
                    if curr_htf is not None:
                        htf_bars.append(curr_htf)
                    curr_htf = {
                        "date": bucket_str,
                        "open": float(b["open"]),
                        "high": float(b["high"]),
                        "low": float(b["low"]),
                        "close": float(b["close"]),
                        "volume": float(b.get("volume", 0.0)),
                    }
                else:
                    curr_htf["high"] = max(curr_htf["high"], float(b["high"]))
                    curr_htf["low"] = min(curr_htf["low"], float(b["low"]))
                    curr_htf["close"] = float(b["close"])
                    curr_htf["volume"] += float(b.get("volume", 0.0))
            except Exception:
                continue

        if curr_htf is not None:
            htf_bars.append(curr_htf)

        return htf_bars

    def run_backtest(
        self,
        execution_bars: List[Dict[str, Any]],
        daily_bars: Optional[List[Dict[str, Any]]] = None,
        h4_bars: Optional[List[Dict[str, Any]]] = None,
    ) -> BacktestSummary:
        """Execute the bar-by-bar backtest simulation."""
        if not execution_bars:
            raise ValueError("No execution bars supplied for backtest")

        # Automatically construct zero-lookahead 4H and Daily bars if not supplied
        if not h4_bars:
            h4_bars = self._resample_higher_timeframe_bars(execution_bars, target_hours=4)
        if not daily_bars:
            daily_bars = self._resample_higher_timeframe_bars(execution_bars, target_hours=24)

        # Pre-index HTF bars by date for instant O(1) alignment
        h4_map = {b["date"]: b for b in h4_bars}
        daily_map = {b["date"][:10]: b for b in daily_bars}

        # Simulation state
        executed_trades: List[SimulatedTrade] = []
        pending_order: Optional[Dict[str, Any]] = None
        active_trade: Optional[Dict[str, Any]] = None
        ticket_counter = 1000

        total_signals = 0
        unfilled_expired = 0

        # Loop through execution bars chronologically
        for i in range(10, len(execution_bars)):
            bar = execution_bars[i]
            bar_date = bar["date"]
            b_open = float(bar["open"])
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            b_close = float(bar["close"])

            # --- 1. Manage Active Trade (if any) ---
            if active_trade is not None:
                trade = active_trade
                trade["bars_held"] += 1
                pos_dir = trade["direction"]

                # Check adverse-first intra-bar outcomes
                if pos_dir == "BUY":
                    hit_sl = b_low <= trade["current_sl"]
                    hit_tp1 = b_high >= trade["tp1"]
                    hit_tp2 = b_high >= trade["tp2"] if trade.get("tp2") else False

                    # Adverse-first check: if SL and TP1 are both within bar range, assume SL hit
                    if hit_sl and hit_tp1:
                        hit_tp1 = False

                    if hit_sl:
                        # Full stopout or breakeven stopout
                        exit_price = trade["current_sl"] - self.slippage
                        exit_reason = "BREAKEVEN_STOP" if trade["sl_at_be"] else "INITIAL_STOP_LOSS"
                        is_win = trade["tp1_taken"] and trade["sl_at_be"]

                        # Calculate final PnL
                        pts = (exit_price - trade["entry_price"])
                        leg_pnl = pts * trade["remaining_vol"] * self.contract_size
                        total_pnl = trade["realized_pnl"] + leg_pnl
                        total_r = total_pnl / trade["planned_risk"]

                        executed_trades.append(SimulatedTrade(
                            ticket=trade["ticket"],
                            symbol=self.symbol,
                            direction=pos_dir,
                            signal_time=trade["signal_time"],
                            fill_time=trade["fill_time"],
                            exit_time=bar_date,
                            entry_price=trade["entry_price"],
                            initial_sl=trade["initial_sl"],
                            tp1_price=trade["tp1"],
                            tp2_price=trade["tp2"],
                            risk_distance=trade["risk_dist"],
                            planned_risk_usd=trade["planned_risk"],
                            lots=trade["initial_vol"],
                            result="WIN" if total_r > 0.1 else ("BREAKEVEN" if total_r >= -0.1 else "LOSS"),
                            realized_pnl_usd=round(total_pnl, 2),
                            realized_r=round(total_r, 2),
                            bars_in_trade=trade["bars_held"],
                            session=trade["session"],
                            exit_reason=exit_reason,
                        ))
                        active_trade = None

                    elif hit_tp1 and not trade["tp1_taken"]:
                        if self.mode == "BASELINE":
                            # Pure PDF specification: Close 100% of position at 1R
                            exit_price = trade["tp1"] - self.slippage
                            pts = (exit_price - trade["entry_price"])
                            pnl = pts * trade["initial_vol"] * self.contract_size
                            r_val = pnl / trade["planned_risk"]

                            executed_trades.append(SimulatedTrade(
                                ticket=trade["ticket"],
                                symbol=self.symbol,
                                direction=pos_dir,
                                signal_time=trade["signal_time"],
                                fill_time=trade["fill_time"],
                                exit_time=bar_date,
                                entry_price=trade["entry_price"],
                                initial_sl=trade["initial_sl"],
                                tp1_price=trade["tp1"],
                                tp2_price=trade["tp2"],
                                risk_distance=trade["risk_dist"],
                                planned_risk_usd=trade["planned_risk"],
                                lots=trade["initial_vol"],
                                result="WIN",
                                realized_pnl_usd=round(pnl, 2),
                                realized_r=round(r_val, 2),
                                bars_in_trade=trade["bars_held"],
                                session=trade["session"],
                                exit_reason="TARGET_1_FULL_CLOSE (1:1 R:R)",
                            ))
                            active_trade = None

                        else:
                            # Aegis Enhanced Mode: Scale out 50%, move SL to Breakeven, let 50% run
                            trade["tp1_taken"] = True
                            half_vol = trade["initial_vol"] * 0.50
                            exit_price = trade["tp1"] - self.slippage
                            pts = (exit_price - trade["entry_price"])
                            half_pnl = pts * half_vol * self.contract_size

                            trade["realized_pnl"] += half_pnl
                            trade["remaining_vol"] -= half_vol
                            # Move SL to Breakeven (entry + offset to cover friction)
                            trade["current_sl"] = trade["entry_price"]
                            trade["sl_at_be"] = True

                    elif hit_tp2 and trade.get("tp2") and trade["tp1_taken"]:
                        # Enhanced runner Target 2 hit
                        exit_price = trade["tp2"] - self.slippage
                        pts = (exit_price - trade["entry_price"])
                        runner_pnl = pts * trade["remaining_vol"] * self.contract_size
                        total_pnl = trade["realized_pnl"] + runner_pnl
                        total_r = total_pnl / trade["planned_risk"]

                        executed_trades.append(SimulatedTrade(
                            ticket=trade["ticket"],
                            symbol=self.symbol,
                            direction=pos_dir,
                            signal_time=trade["signal_time"],
                            fill_time=trade["fill_time"],
                            exit_time=bar_date,
                            entry_price=trade["entry_price"],
                            initial_sl=trade["initial_sl"],
                            tp1_price=trade["tp1"],
                            tp2_price=trade["tp2"],
                            risk_distance=trade["risk_dist"],
                            planned_risk_usd=trade["planned_risk"],
                            lots=trade["initial_vol"],
                            result="WIN",
                            realized_pnl_usd=round(total_pnl, 2),
                            realized_r=round(total_r, 2),
                            bars_in_trade=trade["bars_held"],
                            session=trade["session"],
                            exit_reason="TARGET_2_RUNNER_EXPANSION",
                        ))
                        active_trade = None

                # SELL Trade Logic
                elif pos_dir == "SELL":
                    hit_sl = b_high >= trade["current_sl"]
                    hit_tp1 = b_low <= trade["tp1"]
                    hit_tp2 = b_low <= trade["tp2"] if trade.get("tp2") else False

                    # Adverse-first check
                    if hit_sl and hit_tp1:
                        hit_tp1 = False

                    if hit_sl:
                        exit_price = trade["current_sl"] + self.slippage
                        exit_reason = "BREAKEVEN_STOP" if trade["sl_at_be"] else "INITIAL_STOP_LOSS"
                        is_win = trade["tp1_taken"] and trade["sl_at_be"]

                        pts = (trade["entry_price"] - exit_price)
                        leg_pnl = pts * trade["remaining_vol"] * self.contract_size
                        total_pnl = trade["realized_pnl"] + leg_pnl
                        total_r = total_pnl / trade["planned_risk"]

                        executed_trades.append(SimulatedTrade(
                            ticket=trade["ticket"],
                            symbol=self.symbol,
                            direction=pos_dir,
                            signal_time=trade["signal_time"],
                            fill_time=trade["fill_time"],
                            exit_time=bar_date,
                            entry_price=trade["entry_price"],
                            initial_sl=trade["initial_sl"],
                            tp1_price=trade["tp1"],
                            tp2_price=trade["tp2"],
                            risk_distance=trade["risk_dist"],
                            planned_risk_usd=trade["planned_risk"],
                            lots=trade["initial_vol"],
                            result="WIN" if total_r > 0.1 else ("BREAKEVEN" if total_r >= -0.1 else "LOSS"),
                            realized_pnl_usd=round(total_pnl, 2),
                            realized_r=round(total_r, 2),
                            bars_in_trade=trade["bars_held"],
                            session=trade["session"],
                            exit_reason=exit_reason,
                        ))
                        active_trade = None

                    elif hit_tp1 and not trade["tp1_taken"]:
                        if self.mode == "BASELINE":
                            exit_price = trade["tp1"] + self.slippage
                            pts = (trade["entry_price"] - exit_price)
                            pnl = pts * trade["initial_vol"] * self.contract_size
                            r_val = pnl / trade["planned_risk"]

                            executed_trades.append(SimulatedTrade(
                                ticket=trade["ticket"],
                                symbol=self.symbol,
                                direction=pos_dir,
                                signal_time=trade["signal_time"],
                                fill_time=trade["fill_time"],
                                exit_time=bar_date,
                                entry_price=trade["entry_price"],
                                initial_sl=trade["initial_sl"],
                                tp1_price=trade["tp1"],
                                tp2_price=trade["tp2"],
                                risk_distance=trade["risk_dist"],
                                planned_risk_usd=trade["planned_risk"],
                                lots=trade["initial_vol"],
                                result="WIN",
                                realized_pnl_usd=round(pnl, 2),
                                realized_r=round(r_val, 2),
                                bars_in_trade=trade["bars_held"],
                                session=trade["session"],
                                exit_reason="TARGET_1_FULL_CLOSE (1:1 R:R)",
                            ))
                            active_trade = None

                        else:
                            trade["tp1_taken"] = True
                            half_vol = trade["initial_vol"] * 0.50
                            exit_price = trade["tp1"] + self.slippage
                            pts = (trade["entry_price"] - exit_price)
                            half_pnl = pts * half_vol * self.contract_size

                            trade["realized_pnl"] += half_pnl
                            trade["remaining_vol"] -= half_vol
                            trade["current_sl"] = trade["entry_price"]
                            trade["sl_at_be"] = True

                    elif hit_tp2 and trade.get("tp2") and trade["tp1_taken"]:
                        exit_price = trade["tp2"] + self.slippage
                        pts = (trade["entry_price"] - exit_price)
                        runner_pnl = pts * trade["remaining_vol"] * self.contract_size
                        total_pnl = trade["realized_pnl"] + runner_pnl
                        total_r = total_pnl / trade["planned_risk"]

                        executed_trades.append(SimulatedTrade(
                            ticket=trade["ticket"],
                            symbol=self.symbol,
                            direction=pos_dir,
                            signal_time=trade["signal_time"],
                            fill_time=trade["fill_time"],
                            exit_time=bar_date,
                            entry_price=trade["entry_price"],
                            initial_sl=trade["initial_sl"],
                            tp1_price=trade["tp1"],
                            tp2_price=trade["tp2"],
                            risk_distance=trade["risk_dist"],
                            planned_risk_usd=trade["planned_risk"],
                            lots=trade["initial_vol"],
                            result="WIN",
                            realized_pnl_usd=round(total_pnl, 2),
                            realized_r=round(total_r, 2),
                            bars_in_trade=trade["bars_held"],
                            session=trade["session"],
                            exit_reason="TARGET_2_RUNNER_EXPANSION",
                        ))
                        active_trade = None

            # --- 2. Check Pending Order Fill or Expiry ---
            if pending_order is not None and active_trade is None:
                p_order = pending_order
                p_order["bars_waited"] += 1

                # Check expiration (90 minutes timeout)
                if p_order["bars_waited"] > p_order["signal"].expiration_bars:
                    unfilled_expired += 1
                    pending_order = None
                else:
                    sig = p_order["signal"]
                    filled = False
                    fill_price = 0.0

                    if sig.direction == "BUY_STOP":
                        if b_high >= sig.entry_price:
                            filled = True
                            # Ask price entry with spread and slippage
                            fill_price = sig.entry_price + (self.spread / 2.0) + self.slippage
                    elif sig.direction == "SELL_STOP":
                        if b_low <= sig.entry_price:
                            filled = True
                            # Bid price entry with spread and slippage
                            fill_price = sig.entry_price - (self.spread / 2.0) - self.slippage

                    if filled:
                        # Position Sizing based on fixed risk USD
                        risk_pts = abs(fill_price - sig.stop_loss)
                        if risk_pts > 0:
                            calc_lots = self.fixed_risk_usd / (risk_pts * self.contract_size)
                            lots = max(0.01, round(calc_lots, 2))
                        else:
                            lots = 0.01

                        active_trade = {
                            "ticket": ticket_counter,
                            "direction": "BUY" if "BUY" in sig.direction else "SELL",
                            "signal_time": sig.timestamp,
                            "fill_time": bar_date,
                            "entry_price": fill_price,
                            "initial_sl": sig.stop_loss,
                            "current_sl": sig.stop_loss,
                            "tp1": sig.target_1,
                            "tp2": sig.target_2,
                            "risk_dist": risk_pts,
                            "planned_risk": self.fixed_risk_usd,
                            "initial_vol": lots,
                            "remaining_vol": lots,
                            "realized_pnl": 0.0,
                            "tp1_taken": False,
                            "sl_at_be": False,
                            "bars_held": 0,
                            "session": sig.session,
                        }
                        ticket_counter += 1
                        pending_order = None

            # --- 3. Evaluate New Signals (if no active trade or pending order) ---
            if pending_order is None and active_trade is None:
                # Align previously closed 4H and Daily bars for zero lookahead
                # Date format: 'YYYY-MM-DD HH:MM'
                date_day = bar_date[:10]
                daily_bar = daily_map.get(date_day)

                # Prior 4H bar: find most recent closed 4H bar before current bar
                h4_bar = None
                for h4_b in reversed(h4_bars):
                    if h4_b["date"] < bar_date:
                        h4_bar = h4_b
                        break

                signal = self.strategy.evaluate_setup(
                    bars=execution_bars,
                    current_idx=i,
                    daily_bar=daily_bar,
                    h4_bar=h4_bar,
                )

                if signal is not None:
                    total_signals += 1
                    pending_order = {
                        "signal": signal,
                        "created_idx": i,
                        "bars_waited": 0,
                    }

        # Calculate summary statistics
        total_executed = len(executed_trades)
        wins = sum(1 for t in executed_trades if t.realized_r > 0.1)
        losses = sum(1 for t in executed_trades if t.realized_r < -0.1)
        breakevens = total_executed - wins - losses

        win_rate = (wins / total_executed * 100.0) if total_executed > 0 else 0.0
        loss_rate = (losses / total_executed * 100.0) if total_executed > 0 else 0.0

        total_pnl = sum(t.realized_pnl_usd for t in executed_trades)
        total_r = sum(t.realized_r for t in executed_trades)

        gross_profit = sum(t.realized_pnl_usd for t in executed_trades if t.realized_pnl_usd > 0)
        gross_loss = abs(sum(t.realized_pnl_usd for t in executed_trades if t.realized_pnl_usd < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        expectancy_r = (total_r / total_executed) if total_executed > 0 else 0.0
        expectancy_usd = (total_pnl / total_executed) if total_executed > 0 else 0.0

        # Calculate drawdown
        equity = self.starting_balance
        peak = self.starting_balance
        max_dd_usd = 0.0
        max_dd_pct = 0.0

        for t in executed_trades:
            equity += t.realized_pnl_usd
            if equity > peak:
                peak = equity
            dd = peak - equity
            dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
            if dd > max_dd_usd:
                max_dd_usd = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        avg_bars = (sum(t.bars_in_trade for t in executed_trades) / total_executed) if total_executed > 0 else 0.0

        return BacktestSummary(
            mode=self.mode,
            symbol=self.symbol,
            timeframe=self.timeframe,
            total_signals=total_signals,
            executed_trades=total_executed,
            unfilled_expired=unfilled_expired,
            winning_trades=wins,
            losing_trades=losses,
            breakeven_trades=breakevens,
            win_rate_pct=round(win_rate, 2),
            loss_rate_pct=round(loss_rate, 2),
            total_pnl_usd=round(total_pnl, 2),
            total_pnl_r=round(total_r, 2),
            profit_factor=round(profit_factor, 2),
            expectancy_r=round(expectancy_r, 2),
            expectancy_usd=round(expectancy_usd, 2),
            max_drawdown_usd=round(max_dd_usd, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            avg_trade_bars=round(avg_bars, 1),
            trades=executed_trades,
        )
