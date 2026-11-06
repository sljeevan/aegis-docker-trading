#!/usr/bin/env python3
"""
Aegis Alpha — Account & Prop Firm Backtesting CLI
Run high-fidelity backtests tailored to any account profile (XM Live, FTMO, The5ers, BlueGuardian),
with parameter tuning overrides, prop firm compliance audits, and resource throttling.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from gold_trader.backtest.engine import AccountBacktestEngine
from gold_trader.backtest.profile_loader import load_profile
from gold_trader.backtest.reporter import BacktestReporter


def main():
    parser = argparse.ArgumentParser(
        description="Aegis Alpha — Account & Prop Firm Backtest Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        "-p",
        default="xm_live",
        help="Account profile name ('xm_live', 'ftmo_100k', 'the5ers_100k', 'blueguardian_50k') or path to .env file",
    )
    parser.add_argument(
        "--symbols",
        "-s",
        nargs="+",
        default=None,
        help="Symbols to backtest (e.g. XAUUSD BTCUSD ETHUSD). Default: from profile.",
    )
    parser.add_argument(
        "--period",
        default="2y",
        choices=["6mo", "1y", "2y", "3mo"],
        help="Historical horizon period (default: 2y)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="reports/backtests",
        help="Directory to save certification reports and JSON artifacts",
    )
    parser.add_argument(
        "--nice",
        type=int,
        default=10,
        help="Linux CPU niceness (default: 10, ensures background backtest never starves live trading)",
    )

    # Runtime Parameter Tuning Overrides (In-Memory Only, Zero Live Impact)
    tuning_group = parser.add_argument_group("Tuning Overrides (In-Memory Only)")
    tuning_group.add_argument("--tune-risk-pct", type=float, default=None, help="Override risk percentage (e.g. 0.40)")
    tuning_group.add_argument("--tune-fixed-risk", type=float, default=None, help="Override flat dollar risk (e.g. 75.44)")
    tuning_group.add_argument("--tune-rr", type=float, default=None, help="Override min R:R target")
    tuning_group.add_argument("--tune-scale-out-r", type=float, default=None, help="Override scale-out R target (e.g. 1.25)")
    tuning_group.add_argument("--tune-runner-t2", type=float, default=None, help="Override runner Target 2 multiplier (e.g. 3.0)")
    tuning_group.add_argument("--tune-trail-buffer", type=float, default=None, help="Override runner trailing stop buffer")
    tuning_group.add_argument("--tune-ml-filter", type=str, choices=["true", "false"], default=None, help="Override ML filter activation")

    args = parser.parse_args()

    # Apply CPU scheduling priority to protect live trading daemons
    try:
        os.nice(args.nice)
    except Exception:
        pass

    # Collect tuning overrides
    overrides = {}
    if args.symbols:
        overrides["symbols"] = args.symbols
    if args.tune_risk_pct is not None:
        overrides["risk_pct"] = args.tune_risk_pct
    if args.tune_fixed_risk is not None:
        overrides["fixed_risk_usd"] = args.tune_fixed_risk
    if args.tune_rr is not None:
        overrides["min_rr"] = args.tune_rr
    if args.tune_scale_out_r is not None:
        overrides["scale_out_r"] = args.tune_scale_out_r
    if args.tune_runner_t2 is not None:
        overrides["runner_target_2_multiplier"] = args.tune_runner_t2
    if args.tune_trail_buffer is not None:
        overrides["runner_trail_buffer"] = args.tune_trail_buffer
    if args.tune_ml_filter is not None:
        overrides["enable_ml_filter"] = args.tune_ml_filter == "true"

    print("=" * 90)
    print(f"🚀 AEGIS ALPHA — ACCOUNT BACKTEST ENGINE: {args.profile.upper()}")
    print("=" * 90)

    # 1. Load Profile
    profile = load_profile(args.profile, overrides=overrides)
    print(f"Account Profile:   {profile.name} (Prefix: {profile.account_prefix})")
    print(f"Starting Capital:  ${profile.starting_balance:,.2f}")
    print(f"Risk Calibration:  {profile.risk_mode} ({profile.risk_pct:.2f}% equity / ${profile.fixed_risk_usd:.2f} flat)")
    print(f"Execution Targets: Scale-Out @ {profile.scale_out_r:.2f}R | Target 2 @ {profile.runner_target_2_multiplier:.2f}R")
    print(f"Assets Evaluated:  {', '.join(profile.symbols)}")
    if profile.applied_overrides:
        print(f"Tuning Overrides:  {profile.applied_overrides}")
    print(f"Prop Firm Mode:    {'YES (Strict compliance auditing active)' if profile.is_prop_firm else 'NO (Broker account)'}")
    print("-" * 90)

    # 2. Execute Simulation
    engine = AccountBacktestEngine(profile)
    print(f"Executing {args.period} walk-forward simulation across multi-asset bars...")
    result = engine.run(period=args.period)

    # 3. Save Reports
    output_dir = Path(args.output_dir)
    md_path, json_path = BacktestReporter.save(result, output_dir)

    # 4. Display Terminal Summary
    c = result.compliance
    status_symbol = "🟢 [PASS]" if c.status == "CERTIFIED_PASS" else "🔴 [BREACH]"

    print("\n" + "=" * 90)
    print(f"🏆 BACKTEST EXECUTION AUDIT COMPLETE: {status_symbol}")
    print("=" * 90)
    print(f"Horizon Period:        {result.start_date} to {result.end_date} ({result.period})")
    print(f"Total Closed Trades:   {result.total_trades} ({result.winning_trades} Wins / {result.losing_trades} Losses)")
    print(f"Win Rate:              {result.win_rate_pct:.2f}%")
    print(f"Profit Factor:         {result.profit_factor:.2f}")
    print(f"Net Realized (R):      {result.net_r:+.1f} R")
    print(f"Net Realized Profit:   ${result.net_profit_usd:+,.2f} ({result.return_pct:+.2f}% on initial capital)")
    print(f"Ending Equity:         ${result.final_equity:,.2f}")
    print(f"Max Peak-to-Trough DD: -{result.max_drawdown_pct:.2f}% (-${result.max_drawdown_usd:,.2f})")
    print(f"Sharpe Ratio:          {result.sharpe_ratio:.2f} (Deflated Sharpe: {result.deflated_sharpe_ratio:.3f})")

    if profile.is_prop_firm:
        print("\n--- PROP FIRM COMPLIANCE CARD ---")
        print(f"Max Daily Drawdown:    -{c.max_daily_drawdown_pct:.2f}% (Limit: -{profile.max_daily_loss_pct:.2f}%)")
        print(f"Circuit Breaker Trips: {c.circuit_breaker_trips} trips")
        print(f"Max Overall Drawdown:  -{c.max_overall_drawdown_pct:.2f}% (Limit: -{profile.max_total_drawdown_pct:.2f}%)")
        print(f"Phase 1 Completion:    {'✅ Passed on ' + str(c.phase1_date) if c.phase1_passed else '⏳ In Progress'}")
        print(f"Phase 2 Completion:    {'✅ Passed on ' + str(c.phase2_date) if c.phase2_passed else '⏳ In Progress'}")
        print(f"Estimated Net Payout:  +${c.funded_payout_usd:,.2f} ({profile.profit_split_pct:.0f}% profit split)")

    print("\n--- GENERATED ARTIFACTS ---")
    print(f"📄 Markdown Report: {md_path}")
    print(f"📊 Telemetry JSON:   {json_path}")
    print("=" * 90)


if __name__ == "__main__":
    main()
