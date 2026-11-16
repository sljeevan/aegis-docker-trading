"""
Intra-Session Price Action & Liquidity Strategy
Systematic implementation of Harry Sharp's intraday liquidity sweep framework.

Core Architecture:
1. Multi-timeframe Directional Bias Engine (Daily + 4H consensus).
2. Body-Strict Support/Resistance & Clean Range mapping.
3. 3-to-8 Candle Consolidation Detection with Micro-Liquidity Sweep.
4. Displacement Confirmation with Over-Extension Guard (<50% range width).
5. Resting Stop Orders (BUY_STOP / SELL_STOP) with 1:1 R:R target, Breakeven Shift, and 90m Timeout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SetupSignal:
    timestamp: str
    symbol: str
    direction: str  # "BUY_STOP" or "SELL_STOP"
    entry_price: float
    stop_loss: float
    target_1: float
    risk_distance: float
    target_2: Optional[float] = None  # Extended runner target for Aegis Enhanced profile
    range_high: float = 0.0
    range_low: float = 0.0
    bias: int = 0
    session: str = ""
    expiration_bars: int = 18  # 90 minutes (18 * 5m or 6 * 15m)


class IntraSessionLiquidityStrategy:
    """
    Formal implementation of Harry Sharp's Intra-Session Price Action & Liquidity Strategy.
    """

    def __init__(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "15m",
        min_consolidation_bars: int = 3,
        max_consolidation_bars: int = 8,
        max_displacement_ratio: float = 0.50,  # Max 50% of range width to filter over-extended entries
        order_timeout_minutes: int = 90,
        enable_session_filter: bool = True,
        extended_runner_multiplier: float = 2.50,  # For Aegis Enhanced profile
    ):
        self.symbol = symbol
        self.timeframe = timeframe.lower()
        self.min_consolidation_bars = min_consolidation_bars
        self.max_consolidation_bars = max_consolidation_bars
        self.max_displacement_ratio = max_displacement_ratio
        self.order_timeout_minutes = order_timeout_minutes
        self.enable_session_filter = enable_session_filter
        self.extended_runner_multiplier = extended_runner_multiplier

        # Calculate timeout bars based on timeframe
        tf_mins = 5 if "5m" in self.timeframe else (15 if "15m" in self.timeframe else 60)
        self.expiration_bars = max(1, order_timeout_minutes // tf_mins)

    @staticmethod
    def calculate_bias(daily_bar: Optional[Dict[str, Any]], h4_bar: Optional[Dict[str, Any]]) -> int:
        """
        Directional Bias Engine:
        • Bullish Bias (+1) : If CD,-1 > OD,-1 AND C4H,-1 > O4H,-1
        • Bearish Bias (-1) : If CD,-1 < OD,-1 AND C4H,-1 < O4H,-1
        • Mixed Bias (0)    : Conflicting timeframes -> Execution restricted.
        """
        if not daily_bar or not h4_bar:
            return 0

        d_close = float(daily_bar.get("close", 0.0))
        d_open = float(daily_bar.get("open", 0.0))
        h4_close = float(h4_bar.get("close", 0.0))
        h4_open = float(h4_bar.get("open", 0.0))

        daily_bull = d_close > d_open
        h4_bull = h4_close > h4_open
        daily_bear = d_close < d_open
        h4_bear = h4_close < h4_open

        if daily_bull and h4_bull:
            return 1
        elif daily_bear and h4_bear:
            return -1
        return 0

    @staticmethod
    def is_in_high_liquidity_session(bar_date_str: str) -> Tuple[bool, str]:
        """
        Check if candle timestamp falls inside high-liquidity session windows:
        • London Open: 07:00 – 11:00 UTC
        • New York Open: 12:30 – 16:30 UTC
        """
        try:
            # Expected format: 'YYYY-MM-DD HH:MM' or ISO
            dt = datetime.fromisoformat(bar_date_str.replace("Z", ""))
            hour_minute = dt.hour + dt.minute / 60.0

            # London Open Window: 07:00 to 11:00 UTC
            if 7.0 <= hour_minute <= 11.0:
                return True, "LONDON"

            # New York Open Window: 12:30 to 16:30 UTC
            if 12.5 <= hour_minute <= 16.5:
                return True, "NEW_YORK"

            return False, "OFF_SESSION"
        except Exception:
            return True, "UNKNOWN"

    def evaluate_setup(
        self,
        bars: List[Dict[str, Any]],
        current_idx: int,
        daily_bar: Optional[Dict[str, Any]],
        h4_bar: Optional[Dict[str, Any]],
    ) -> Optional[SetupSignal]:
        """
        Evaluate candle pattern at `current_idx` for Harry Sharp liquidity setup.
        Returns a SetupSignal if conditions are satisfied, else None.
        """
        if current_idx < self.max_consolidation_bars + 2:
            return None

        current_bar = bars[current_idx]
        bar_date = current_bar.get("date", "")

        # 1. Session Filter Check
        if self.enable_session_filter:
            in_session, session_name = self.is_in_high_liquidity_session(bar_date)
            if not in_session:
                return None
        else:
            session_name = "ALL"

        # 2. Multi-Timeframe Directional Bias Check
        bias = self.calculate_bias(daily_bar, h4_bar)
        if bias == 0:
            return None

        # 3. Detect Consolidation Range over 3 to 8 prior candles
        # Consolidation window: prior candles from (current_idx - N) to (current_idx - 1)
        # Search for valid consolidation length N in [min_bars, max_bars]
        best_signal = None

        for n in range(self.min_consolidation_bars, self.max_consolidation_bars + 1):
            prior_bars = bars[current_idx - n : current_idx]
            if len(prior_bars) != n:
                continue

            # In the specification, Support/Resistance is mapped to candle body closes
            # Range boundaries:
            body_highs = [max(float(b["open"]), float(b["close"])) for b in prior_bars]
            body_lows = [min(float(b["open"]), float(b["close"])) for b in prior_bars]
            wick_highs = [float(b["high"]) for b in prior_bars]
            wick_lows = [float(b["low"]) for b in prior_bars]

            range_high = max(body_highs)
            range_low = min(body_lows)
            range_width = range_high - range_low

            if range_width <= 0:
                continue

            c_open = float(current_bar["open"])
            c_high = float(current_bar["high"])
            c_low = float(current_bar["low"])
            c_close = float(current_bar["close"])
            candle_displacement = abs(c_close - c_open)

            # Filter: High displacement (>50% range width) is skipped to avoid over-extended entries
            if candle_displacement > (range_width * self.max_displacement_ratio):
                continue

            # --- LONG SETUP (Bias == +1) ---
            if bias == 1:
                # A. Liquidity Sweep: Price wicks beyond range low before reversing
                # Check if current bar or immediate prior bar swept the range low
                swept_low = any(float(b["low"]) < range_low for b in prior_bars[-2:]) or (c_low < range_low)
                has_bottom_wick = (min(c_open, c_close) - c_low) > 0.0

                # B. Displacement Candle: Subsequent candle closes cleanly above consolidation range boundary
                closed_above = c_close > range_high

                if swept_low and closed_above and has_bottom_wick:
                    entry = round(c_high, 4)
                    sl = round(c_low, 4)
                    risk = entry - sl

                    if risk > 0:
                        tp1 = round(entry + risk, 4)
                        tp2 = round(entry + (risk * self.extended_runner_multiplier), 4)

                        best_signal = SetupSignal(
                            timestamp=bar_date,
                            symbol=self.symbol,
                            direction="BUY_STOP",
                            entry_price=entry,
                            stop_loss=sl,
                            target_1=tp1,
                            risk_distance=risk,
                            target_2=tp2,
                            range_high=range_high,
                            range_low=range_low,
                            bias=bias,
                            session=session_name,
                            expiration_bars=self.expiration_bars,
                        )
                        break

            # --- SHORT SETUP (Bias == -1) ---
            elif bias == -1:
                # A. Liquidity Sweep: Price wicks beyond range high before reversing
                swept_high = any(float(b["high"]) > range_high for b in prior_bars[-2:]) or (c_high > range_high)
                has_top_wick = (c_high - max(c_open, c_close)) > 0.0

                # B. Displacement Candle: Candle closes cleanly below consolidation range boundary
                closed_below = c_close < range_low

                if swept_high and closed_below and has_top_wick:
                    entry = round(c_low, 4)
                    sl = round(c_high, 4)
                    risk = sl - entry

                    if risk > 0:
                        tp1 = round(entry - risk, 4)
                        tp2 = round(entry - (risk * self.extended_runner_multiplier), 4)

                        best_signal = SetupSignal(
                            timestamp=bar_date,
                            symbol=self.symbol,
                            direction="SELL_STOP",
                            entry_price=entry,
                            stop_loss=sl,
                            target_1=tp1,
                            risk_distance=risk,
                            target_2=tp2,
                            range_high=range_high,
                            range_low=range_low,
                            bias=bias,
                            session=session_name,
                            expiration_bars=self.expiration_bars,
                        )
                        break

        return best_signal
