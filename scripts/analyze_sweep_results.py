#!/usr/bin/env python3
import json
from pathlib import Path

def main():
    json_path = Path("reports/backtests/parameter_sweep_results.json")
    with open(json_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    print(f"Total evaluated runs: {len(results)}")

    # 1. Top by Win Rate (min 1000 trades)
    wr_sorted = sorted([r for r in results if r["total_trades"] >= 1000], key=lambda x: x["win_rate_pct"], reverse=True)
    print("\n" + "=" * 110)
    print("TOP 5 BY WIN RATE")
    print("=" * 110)
    for i, r in enumerate(wr_sorted[:5], 1):
        p = r["params"]
        print(f"#{i} WR: {r['win_rate_pct']:.2f}% | PF: {r['profit_factor']:.2f} | Trades: {r['total_trades']} | PnL: ${r['net_profit_usd']:,.2f} | Net R: {r['net_r']:+.1f}R | DD: {r['max_drawdown_pct']:.2f}% | Sharpe: {r['sharpe_ratio']:.2f}")
        print(f"    Params: scale_out_r={p['scale_out_r']}, sl_atr_mult={p['sl_atr_mult']}, donchian={p['donchian_period']}, adx={p['adx_min']}, trail_mult={p['runner_trail_buffer_mult']}, t2={p['runner_target_2_multiplier']}")
        print(f"    Symbols: {r['symbols']}")

    # 2. Top by Net PnL (min 1000 trades)
    pnl_sorted = sorted([r for r in results if r["total_trades"] >= 1000], key=lambda x: x["net_profit_usd"], reverse=True)
    print("\n" + "=" * 110)
    print("TOP 5 BY NET PNL (PROFIT MAXIMIZERS)")
    print("=" * 110)
    for i, r in enumerate(pnl_sorted[:5], 1):
        p = r["params"]
        print(f"#{i} PnL: ${r['net_profit_usd']:,.2f} | WR: {r['win_rate_pct']:.2f}% | PF: {r['profit_factor']:.2f} | Trades: {r['total_trades']} | Net R: {r['net_r']:+.1f}R | DD: {r['max_drawdown_pct']:.2f}% | Sharpe: {r['sharpe_ratio']:.2f}")
        print(f"    Params: scale_out_r={p['scale_out_r']}, sl_atr_mult={p['sl_atr_mult']}, donchian={p['donchian_period']}, adx={p['adx_min']}, trail_mult={p['runner_trail_buffer_mult']}, t2={p['runner_target_2_multiplier']}")
        print(f"    Symbols: {r['symbols']}")

    # 3. Top by Composite (WR * PF)
    comp_sorted = sorted([r for r in results if r["total_trades"] >= 1000], key=lambda x: x["win_rate_pct"] * x["profit_factor"], reverse=True)
    print("\n" + "=" * 110)
    print("TOP 5 BY COMPOSITE EFFICIENCY (Win Rate × Profit Factor)")
    print("=" * 110)
    for i, r in enumerate(comp_sorted[:5], 1):
        p = r["params"]
        comp = r['win_rate_pct'] * r['profit_factor']
        print(f"#{i} Score: {comp:.1f} | WR: {r['win_rate_pct']:.2f}% | PF: {r['profit_factor']:.2f} | Trades: {r['total_trades']} | PnL: ${r['net_profit_usd']:,.2f} | Net R: {r['net_r']:+.1f}R | DD: {r['max_drawdown_pct']:.2f}%")
        print(f"    Params: scale_out_r={p['scale_out_r']}, sl_atr_mult={p['sl_atr_mult']}, donchian={p['donchian_period']}, adx={p['adx_min']}, trail_mult={p['runner_trail_buffer_mult']}, t2={p['runner_target_2_multiplier']}")
        print(f"    Symbols: {r['symbols']}")

    # 4. Sensitivity Analysis
    print("\n" + "=" * 110)
    print("PARAMETER SENSITIVITY BREAKDOWN (Marginal Averages across 960 runs)")
    print("=" * 110)

    # Scale out R
    print("--- Sensitivity to scale_out_r ---")
    for val in [0.50, 0.75, 1.00, 1.25]:
        sub = [r for r in results if r["params"]["scale_out_r"] == val]
        avg_wr = sum(r["win_rate_pct"] for r in sub) / len(sub)
        avg_pf = sum(r["profit_factor"] for r in sub) / len(sub)
        avg_pnl = sum(r["net_profit_usd"] for r in sub) / len(sub)
        avg_trades = sum(r["total_trades"] for r in sub) / len(sub)
        print(f"scale_out_r = {val:.2f}R: Avg WR = {avg_wr:.2f}%, Avg PF = {avg_pf:.2f}, Avg PnL = ${avg_pnl:,.2f}, Avg Trades = {avg_trades:.0f}")

    # SL ATR mult
    print("\n--- Sensitivity to sl_atr_mult ---")
    for val in [1.25, 1.50, 1.75, 2.00, 2.25]:
        sub = [r for r in results if r["params"]["sl_atr_mult"] == val]
        avg_wr = sum(r["win_rate_pct"] for r in sub) / len(sub)
        avg_pf = sum(r["profit_factor"] for r in sub) / len(sub)
        avg_pnl = sum(r["net_profit_usd"] for r in sub) / len(sub)
        avg_trades = sum(r["total_trades"] for r in sub) / len(sub)
        print(f"sl_atr_mult = {val:.2f}x: Avg WR = {avg_wr:.2f}%, Avg PF = {avg_pf:.2f}, Avg PnL = ${avg_pnl:,.2f}, Avg Trades = {avg_trades:.0f}")

    # Donchian Period
    print("\n--- Sensitivity to donchian_period ---")
    for val in [10, 15, 20]:
        sub = [r for r in results if r["params"]["donchian_period"] == val]
        avg_wr = sum(r["win_rate_pct"] for r in sub) / len(sub)
        avg_pf = sum(r["profit_factor"] for r in sub) / len(sub)
        avg_pnl = sum(r["net_profit_usd"] for r in sub) / len(sub)
        avg_trades = sum(r["total_trades"] for r in sub) / len(sub)
        print(f"donchian_period = {val}: Avg WR = {avg_wr:.2f}%, Avg PF = {avg_pf:.2f}, Avg PnL = ${avg_pnl:,.2f}, Avg Trades = {avg_trades:.0f}")

    # ADX Min
    print("\n--- Sensitivity to adx_min ---")
    for val in [None, 18.0, 22.0, 25.0]:
        sub = [r for r in results if r["params"]["adx_min"] == val]
        avg_wr = sum(r["win_rate_pct"] for r in sub) / len(sub)
        avg_pf = sum(r["profit_factor"] for r in sub) / len(sub)
        avg_pnl = sum(r["net_profit_usd"] for r in sub) / len(sub)
        avg_trades = sum(r["total_trades"] for r in sub) / len(sub)
        val_str = f"{val:.0f}" if val is not None else "None"
        print(f"adx_min = {val_str:>5}: Avg WR = {avg_wr:.2f}%, Avg PF = {avg_pf:.2f}, Avg PnL = ${avg_pnl:,.2f}, Avg Trades = {avg_trades:.0f}")

    # Runner trail buffer
    print("\n--- Sensitivity to runner_trail_buffer_mult ---")
    for val in [0.75, 1.00]:
        sub = [r for r in results if r["params"]["runner_trail_buffer_mult"] == val]
        avg_wr = sum(r["win_rate_pct"] for r in sub) / len(sub)
        avg_pf = sum(r["profit_factor"] for r in sub) / len(sub)
        avg_pnl = sum(r["net_profit_usd"] for r in sub) / len(sub)
        avg_trades = sum(r["total_trades"] for r in sub) / len(sub)
        print(f"runner_trail_buffer_mult = {val:.2f}x: Avg WR = {avg_wr:.2f}%, Avg PF = {avg_pf:.2f}, Avg PnL = ${avg_pnl:,.2f}, Avg Trades = {avg_trades:.0f}")

if __name__ == "__main__":
    main()
