#!/usr/bin/env python3
"""
Aegis Alpha — Intra-Session Price Action & Liquidity Strategy Backtest Runner
Benchmark Harry Sharp's liquidity sweep strategy across assets (XAUUSD, BTCUSD, EURUSD)
and timeframes (15m, 5m), comparing Exact PDF Specification vs. Aegis Enhanced Profile.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from gold_trader.backtest.liquidity_backtest import LiquidityBacktestEngine, BacktestSummary
from gold_trader.core.data_feed import fetch_gold_candles


def run_benchmark(
    symbols: list[str],
    timeframes: list[str],
    period: str = "60d",
    fixed_risk_usd: float = 75.44,
    starting_balance: float = 1000.0,
    output_dir: str = "reports/backtests",
):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    all_results: list[BacktestSummary] = []

    print("\n" + "=" * 95)
    print("🚀 RUNNING INTRA-SESSION PRICE ACTION & LIQUIDITY STRATEGY BENCHMARK")
    print(f"Horizon: {period} | Fixed Risk: ${fixed_risk_usd:.2f} | Capital: ${starting_balance:.2f}")
    print("=" * 95)

    symbol_map = {
        "XAUUSD": "GC=F",
        "BTCUSD": "BTC-USD",
        "EURUSD": "EURUSD=X",
    }

    for symbol in symbols:
        ticker = symbol_map.get(symbol, symbol)
        for tf in timeframes:
            print(f"\n📥 Fetching {period} {tf} historical bars for {symbol} ({ticker})...")
            try:
                bars = fetch_gold_candles(symbol=ticker, interval=tf, period=period)
                if not bars:
                    print(f"⚠️  No bars returned for {symbol} {tf}. Skipping.")
                    continue
                print(f"✅ Loaded {len(bars)} {tf} bars spanning {bars[0]['date']} -> {bars[-1]['date']}.")
            except Exception as e:
                print(f"❌ Error fetching bars for {symbol}: {e}")
                continue

            # Run 1: Baseline PDF Specification (1:1 R:R Full Close)
            engine_base = LiquidityBacktestEngine(
                symbol=symbol,
                timeframe=tf,
                starting_balance=starting_balance,
                fixed_risk_usd=fixed_risk_usd,
                mode="BASELINE",
                enable_session_filter=True,
            )
            res_base = engine_base.run_backtest(execution_bars=bars)
            all_results.append(res_base)

            # Run 2: Aegis Enhanced Profile (50% scale-out @ 1R, BE shift, 50% runner @ 2.5R)
            engine_enh = LiquidityBacktestEngine(
                symbol=symbol,
                timeframe=tf,
                starting_balance=starting_balance,
                fixed_risk_usd=fixed_risk_usd,
                mode="ENHANCED",
                enable_session_filter=True,
            )
            res_enh = engine_enh.run_backtest(execution_bars=bars)
            all_results.append(res_enh)

    # Print Summary Comparison Table
    print("\n" + "=" * 115)
    print("📊 SYSTEMATIC BACKTEST RESULTS SUMMARY")
    print("=" * 115)
    header = f"{'Symbol':<8} {'TF':<5} {'Profile':<10} {'Trades':<8} {'Win Rate':<10} {'Net PnL ($)':<14} {'Net R':<8} {'Profit Factor':<15} {'Max DD ($)':<12} {'Expectancy':<12}"
    print(header)
    print("-" * 115)

    for r in all_results:
        row = (
            f"{r.symbol:<8} "
            f"{r.timeframe:<5} "
            f"{r.mode:<10} "
            f"{r.executed_trades:<8} "
            f"{r.win_rate_pct:>6.1f}%   "
            f"${r.total_pnl_usd:>10.2f}   "
            f"{r.total_pnl_r:>+6.2f}R "
            f"{r.profit_factor:>8.2f}        "
            f"${r.max_drawdown_usd:>8.2f}    "
            f"{r.expectancy_r:>+5.2f}R (${r.expectancy_usd:+.2f})"
        )
        print(row)

    print("=" * 115)

    # Save detailed JSON artifact
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = Path(output_dir) / f"liquidity_strategy_backtest_{timestamp_str}.json"
    
    serializable_results = []
    for r in all_results:
        serializable_results.append({
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "mode": r.mode,
            "total_signals": r.total_signals,
            "executed_trades": r.executed_trades,
            "unfilled_expired": r.unfilled_expired,
            "winning_trades": r.winning_trades,
            "losing_trades": r.losing_trades,
            "breakeven_trades": r.breakeven_trades,
            "win_rate_pct": r.win_rate_pct,
            "loss_rate_pct": r.loss_rate_pct,
            "total_pnl_usd": r.total_pnl_usd,
            "total_pnl_r": r.total_pnl_r,
            "profit_factor": r.profit_factor,
            "expectancy_r": r.expectancy_r,
            "expectancy_usd": r.expectancy_usd,
            "max_drawdown_usd": r.max_drawdown_usd,
            "max_drawdown_pct": r.max_drawdown_pct,
            "trades": [
                {
                    "ticket": t.ticket,
                    "direction": t.direction,
                    "signal_time": t.signal_time,
                    "fill_time": t.fill_time,
                    "exit_time": t.exit_time,
                    "entry_price": t.entry_price,
                    "initial_sl": t.initial_sl,
                    "tp1_price": t.tp1_price,
                    "tp2_price": t.tp2_price,
                    "lots": t.lots,
                    "result": t.result,
                    "realized_pnl_usd": t.realized_pnl_usd,
                    "realized_r": t.realized_r,
                    "session": t.session,
                    "exit_reason": t.exit_reason,
                }
                for t in r.trades
            ],
        })

    with open(json_path, "w") as f:
        json.dump(serializable_results, f, indent=2)

    print(f"\n📁 Detailed JSON Artifact saved to: {json_path}")
    return all_results


def main():
    parser = argparse.ArgumentParser(description="Aegis Alpha — Liquidity Strategy Backtest Runner")
    parser.add_argument("--symbols", "-s", nargs="+", default=["XAUUSD", "BTCUSD", "EURUSD"], help="Symbols to test")
    parser.add_argument("--timeframes", "-t", nargs="+", default=["15m", "5m"], help="Timeframes to test")
    parser.add_argument("--period", "-p", default="60d", help="Horizon (e.g. 60d, 30d)")
    parser.add_argument("--fixed-risk", type=float, default=75.44, help="Fixed risk USD per trade")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting account capital")
    args = parser.parse_args()

    run_benchmark(
        symbols=args.symbols,
        timeframes=args.timeframes,
        period=args.period,
        fixed_risk_usd=args.fixed_risk,
        starting_balance=args.capital,
    )


if __name__ == "__main__":
    main()
