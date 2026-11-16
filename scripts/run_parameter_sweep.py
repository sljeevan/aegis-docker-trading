#!/usr/bin/env python3
"""
Aegis Alpha — High-Performance Parameter Permutation & Win-Rate Optimizer.
Explores combinations of:
- scale_out_r (Target 1 scale-out distance)
- sl_atr_mult (Stop-Loss ATR distance)
- donchian_period (Breakout filter period: 10, 15, 20)
- adx_min (Trend strength gatekeeper: None, 18, 22, 25)
- runner_trail_buffer_mult (Trailing stop sensitivity: 0.75x, 1.0x, 1.25x)
- runner_target_2_multiplier (Target 2 R: 2.5R, 3.0R)

Runs across 2-year market data with in-memory cached market feeds for maximum velocity.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from gold_trader.backtest.engine import AccountBacktestEngine
from gold_trader.backtest.profile_loader import AccountProfile, load_profile


def run_parameter_sweep(
    base_profile_name: str = "xm_live",
    period: str = "2y",
    output_file: str = "reports/backtests/parameter_sweep_results.json",
):
    print("=" * 95)
    print("🔬 AEGIS ALPHA — STRATEGY PERMUTATION & WIN-RATE OPTIMIZER")
    print(f"Horizon: {period.upper()} | Base Profile: {base_profile_name.upper()}")
    print("=" * 95)

    # 1. Initialize engine and pre-load all 2-year market feeds into memory once
    base_profile = load_profile(base_profile_name)
    engine = AccountBacktestEngine(base_profile)

    print("⏳ Pre-loading 2-year market data and sub-bars across all symbols...")
    t0 = time.time()
    cached_market_data = {
        s: engine.load_market_data(s, period=period, interval="1h")
        for s in base_profile.symbols
    }
    t_load = time.time() - t0
    print(f"✅ Market data loaded in {t_load:.2f}s ({sum(len(d['dates']) for d in cached_market_data.values())} total 1H bars)")

    # 2. Define Permutation Grid
    param_grid = {
        "scale_out_r": [0.50, 0.75, 1.00, 1.25],
        "sl_atr_mult": [1.25, 1.50, 1.75, 2.00, 2.25],
        "donchian_period": [10, 15, 20],
        "adx_min": [None, 18.0, 22.0, 25.0],
        "runner_trail_buffer_mult": [0.75, 1.00],
        "runner_target_2_multiplier": [2.50, 3.00],
    }

    # Generate permutations
    keys = list(param_grid.keys())
    combinations = list(itertools.product(*[param_grid[k] for k in keys]))
    total_combos = len(combinations)
    print(f"\n📊 Total Parameter Permutations to Evaluate: {total_combos}")
    print("-" * 95)

    results: List[Dict[str, Any]] = []
    t_start = time.time()

    for idx, combo in enumerate(combinations, 1):
        params = dict(zip(keys, combo))

        # Build in-memory profile with current permutation
        profile = load_profile(base_profile_name, overrides=params)
        test_engine = AccountBacktestEngine(profile)

        # Inject pre-cached market data to bypass IO
        test_engine.load_market_data = lambda symbol, period="2y", interval="1h": cached_market_data[symbol]

        t_sim_0 = time.time()
        res = test_engine.run(period=period)
        sim_elapsed = time.time() - t_sim_0

        # Calculate per-symbol win rates
        sym_stats = {}
        for s in profile.symbols:
            s_trades = [t for t in res.trades if t["symbol"] == s]
            s_wins = [t for t in s_trades if t["net_pnl"] > 0]
            sym_stats[s] = {
                "trades": len(s_trades),
                "win_rate": round(len(s_wins) / len(s_trades) * 100.0, 1) if s_trades else 0.0,
                "pnl": round(sum(t["net_pnl"] for t in s_trades), 2),
            }

        record = {
            "id": idx,
            "params": params,
            "total_trades": res.total_trades,
            "win_rate_pct": res.win_rate_pct,
            "profit_factor": res.profit_factor,
            "net_profit_usd": res.net_profit_usd,
            "return_pct": res.return_pct,
            "net_r": res.net_r,
            "max_drawdown_pct": res.max_drawdown_pct,
            "sharpe_ratio": res.sharpe_ratio,
            "deflated_sharpe_ratio": res.deflated_sharpe_ratio,
            "symbols": sym_stats,
        }
        results.append(record)

        if idx % 50 == 0 or idx == total_combos:
            pct = (idx / total_combos) * 100.0
            elapsed = time.time() - t_start
            rate = idx / elapsed
            eta = (total_combos - idx) / rate if rate > 0 else 0
            print(f"[{idx:4d}/{total_combos}] ({pct:5.1f}%) | "
                  f"Current WR: {res.win_rate_pct:5.2f}% | PF: {res.profit_factor:4.2f} | PnL: ${res.net_profit_usd:+9.2f} | "
                  f"Rate: {rate:4.1f} runs/s | ETA: {eta:4.1f}s", flush=True)

    total_time = time.time() - t_start
    print("=" * 95)
    print(f"🏁 Permutation Sweep Finished in {total_time:.1f}s ({total_combos / total_time:.1f} runs/sec)!")

    # 3. Save full JSON results
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"📁 Full results saved to: {out_path}")

    # 4. Analyze Rankings
    # Ranking A: Highest Win Rate (with min 1000 trades & profitable)
    viable = [r for r in results if r["total_trades"] >= 1000 and r["profit_factor"] >= 1.20]
    viable.sort(key=lambda x: x["win_rate_pct"], reverse=True)

    print("\n" + "=" * 115)
    print("🏆 TOP 10 PERMUTATIONS BY WIN RATE (Min 1,000 Trades & PF >= 1.20)")
    print("=" * 115)
    header = f"{'Rank':<5} {'Win Rate':<10} {'PF':<6} {'Trades':<8} {'Net PnL ($)':<14} {'Net R':<9} {'ScaleR':<7} {'SL ATR':<7} {'DC Per':<7} {'ADX':<6} {'TrailMult':<10} {'T2 R'}"
    print(header)
    print("-" * 115)

    for rank, r in enumerate(viable[:10], 1):
        p = r["params"]
        adx_str = str(int(p["adx_min"])) if p["adx_min"] is not None else "None"
        row = (
            f"#{rank:<4} {r['win_rate_pct']:>6.2f}%   {r['profit_factor']:>5.2f}  "
            f"{r['total_trades']:>6d}   ${r['net_profit_usd']:>11,.2f}   {r['net_r']:>+6.1f}R  "
            f"{p['scale_out_r']:>5.2f}R  {p['sl_atr_mult']:>5.2f}x  {p['donchian_period']:>4d}    "
            f"{adx_str:>5s}  {p['runner_trail_buffer_mult']:>7.2f}x     {p['runner_target_2_multiplier']:>4.1f}R"
        )
        print(row)

    # Ranking B: Optimal Balance (Maximizing Win Rate * Profit Factor)
    balanced = [r for r in results if r["total_trades"] >= 1000 and r["profit_factor"] >= 1.30]
    balanced.sort(key=lambda x: (x["win_rate_pct"] * x["profit_factor"]), reverse=True)

    print("\n" + "=" * 115)
    print("⚖️ TOP 10 OPTIMAL BALANCED PERMUTATIONS (Composite: Win Rate × Profit Factor)")
    print("=" * 115)
    print(header)
    print("-" * 115)

    for rank, r in enumerate(balanced[:10], 1):
        p = r["params"]
        adx_str = str(int(p["adx_min"])) if p["adx_min"] is not None else "None"
        row = (
            f"#{rank:<4} {r['win_rate_pct']:>6.2f}%   {r['profit_factor']:>5.2f}  "
            f"{r['total_trades']:>6d}   ${r['net_profit_usd']:>11,.2f}   {r['net_r']:>+6.1f}R  "
            f"{p['scale_out_r']:>5.2f}R  {p['sl_atr_mult']:>5.2f}x  {p['donchian_period']:>4d}    "
            f"{adx_str:>5s}  {p['runner_trail_buffer_mult']:>7.2f}x     {p['runner_target_2_multiplier']:>4.1f}R"
        )
        print(row)

    print("=" * 115)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aegis Alpha Parameter Permutation Sweep")
    parser.add_argument("--profile", default="xm_live")
    parser.add_argument("--period", default="2y")
    parser.add_argument("--output", default="reports/backtests/parameter_sweep_results.json")
    args = parser.parse_args()

    run_parameter_sweep(
        base_profile_name=args.profile,
        period=args.period,
        output_file=args.output,
    )
