"""
24/7 Autonomous Multi-Asset Live Market Scanner & Risk-Reward Trading Daemon.
Supports Gold (XAUUSD), Bitcoin (BTCUSD), and Ethereum (ETHUSD).
Usage:
    python -m gold_trader.scripts.run_auto_trader --once
    python -m gold_trader.scripts.run_auto_trader --loop --interval-seconds 60
    python -m gold_trader.scripts.run_auto_trader --symbols XAUUSD,BTCUSD,ETHUSD --loop
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from gold_trader.brokers.socket_broker import SocketBroker
from gold_trader.core.auto_trader import AutoTraderConfig, GoldAutoTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("AutoTraderRunner")


def run_symbol_daemon(symbol: str, args: argparse.Namespace, stop_event: Optional[threading.Event] = None, is_multi: bool = False):
    prefix_tag = f"[{symbol}] " if is_multi else ""
    is_btc = "BTC" in symbol.upper()
    is_eth = "ETH" in symbol.upper()

    # Asset-specific calibration defaults
    if is_btc:
        default_trail = 350.00
        adx_thresh = args.adx_min_threshold
    elif is_eth:
        default_trail = 10.00
        adx_thresh = args.adx_min_threshold if args.adx_min_threshold is not None else 22.0
    else:
        default_trail = 4.50
        adx_thresh = args.adx_min_threshold

    trail_buffer_usd = (
        default_trail if args.runner_trail_buffer == 4.50 else args.runner_trail_buffer
    )

    # Session filter & Scale-Out R calibration (matches native daemons & production backtest spec)
    # Gold trades London/NY liquid hours (07:00-19:00 UTC); Crypto (BTC/ETH) trades 24/7 unconstrained
    # Target 1 scale-out: 1.25R for Gold, 1.00R for BTC and ETH
    if is_btc or is_eth:
        sym_session_filter = False
        sym_scale_out_r = 1.00
    else:
        sym_session_filter = args.session_filter
        sym_scale_out_r = args.scale_out_r

    # Isolated SQLite database path per asset
    if args.db_path:
        if is_multi:
            db_dir = os.path.dirname(args.db_path) or "."
            if is_btc:
                sym_db = os.path.join(db_dir, "btc_auto_trader.db")
            elif is_eth:
                sym_db = os.path.join(db_dir, "eth_auto_trader.db")
            else:
                sym_db = os.path.join(db_dir, "auto_trader.db")
        else:
            sym_db = args.db_path
    else:
        sym_db = None

    # Order tag per asset
    if args.order_tag:
        if is_multi:
            sym_suffix = "BTC" if is_btc else ("ETH" if is_eth else "XAU")
            sym_order_tag = f"{args.order_tag}_{sym_suffix}"
        else:
            sym_order_tag = args.order_tag
    else:
        sym_order_tag = None

    cfg = AutoTraderConfig(
        symbol=symbol,
        timeframe=args.timeframe,
        mode=args.mode,
        flat_risk=True,
        fixed_risk_usd=args.fixed_risk_usd,
        risk_pct_per_trade=args.risk_pct,
        min_risk_reward=args.min_rr,
        min_quality_score=args.min_quality,
        max_open_positions=args.max_positions,
        max_unhedged_positions=args.max_unhedged_positions,
        allow_runner_compounding=args.allow_runner_compounding,
        scan_interval_seconds=args.interval_seconds,
        active_interval_seconds=args.active_interval_seconds,
        enable_adaptive_dual_speed=args.adaptive_dual_speed,
        enable_two_tier_polling=args.enable_two_tier,
        micro_polling_interval_seconds=args.micro_interval,
        auto_execute=not args.dry_run,
        trail_to_breakeven_at_t1=True,
        one_trade_per_bar=args.one_trade_per_bar,
        loss_cooldown_minutes=args.loss_cooldown_minutes,
        use_pullback_limit=args.use_pullback_limit,
        pullback_discount_pips=args.pullback_discount_pips,
        max_daily_trades=args.max_daily_trades,
        session_filter=sym_session_filter,
        donchian_period=args.donchian_period,
        trend_filter_ema=args.trend_filter_ema,
        trigger_mode=args.trigger_mode,
        enable_partial_scale_out=args.enable_partial_scale_out,
        partial_scale_out_pct=args.partial_scale_out_pct,
        scale_out_r=sym_scale_out_r,
        enable_runner_trailing=args.enable_runner_trailing,
        runner_trail_buffer_r=args.runner_trail_buffer_r,
        runner_trail_buffer_usd=trail_buffer_usd,
        runner_target_2_multiplier=args.runner_target_2_multiplier,
        enable_news_catalyst_guard=args.enable_news_catalyst_guard,
        news_guard_buffer_minutes=args.news_guard_buffer_minutes,
        enable_ml_filter=args.enable_ml_filter,
        ml_min_probability=args.ml_min_prob,
        ml_dynamic_sizing=args.ml_dynamic_sizing,
        ml_model_path=args.ml_model_path,
        adx_min_threshold=adx_thresh,
        require_slope_alignment=args.require_slope_alignment,
        ladder_state_file=args.ladder_state_file,
        db_path=sym_db,
        order_tag=sym_order_tag,
    )

    trader = GoldAutoTrader(broker=SocketBroker(port=args.bridge_port), config_override=cfg)

    logger.info("=" * 60)
    logger.info(f"🚀 {prefix_tag}24/7 Autonomous {cfg.symbol} Trading Engine Initialized")
    logger.info(f"   {prefix_tag}Mode: {cfg.mode} | Symbol: {cfg.symbol} | Timeframe: {cfg.timeframe} | Min R:R: {cfg.min_risk_reward:.2f}R")
    logger.info(f"   {prefix_tag}Donchian: {cfg.donchian_period} | Filter: {cfg.trend_filter_ema} EMA | Trigger: {cfg.trigger_mode}")
    logger.info(f"   {prefix_tag}Risk Per Trade: {cfg.risk_pct_per_trade}% | Min Quality: {cfg.min_quality_score}/100")
    logger.info(f"   {prefix_tag}Bar Lock: {cfg.one_trade_per_bar} | Loss Cooldown: {cfg.loss_cooldown_minutes}m")
    logger.info(f"   {prefix_tag}Daily Trade Cap: {cfg.max_daily_trades} | Session Filter: {cfg.session_filter}")
    logger.info(f"   {prefix_tag}News Catalyst Guard (OpenBB): {cfg.enable_news_catalyst_guard} (±{cfg.news_guard_buffer_minutes}m buffer)")
    logger.info(f"   {prefix_tag}ML Meta-Labeling Filter: {cfg.enable_ml_filter} (Min Win Prob: {cfg.ml_min_probability:.1%}, Dynamic Sizing: {cfg.ml_dynamic_sizing})")
    logger.info(f"   {prefix_tag}Regime Safeguards: ADX Filter: {cfg.adx_min_threshold} | 50 EMA Slope Alignment: {cfg.require_slope_alignment}")
    logger.info(f"   {prefix_tag}Auto Execute: {cfg.auto_execute} | Max Open: {cfg.max_open_positions}")
    logger.info(f"   {prefix_tag}Partial Scale-Out: {cfg.enable_partial_scale_out} ({int(cfg.partial_scale_out_pct*100)}% @ {cfg.scale_out_r:.2f}R)")
    trail_desc = f"{cfg.runner_trail_buffer_r:.2f}R Dynamic" if cfg.runner_trail_buffer_r is not None else f"${cfg.runner_trail_buffer_usd:.2f} Fixed"
    logger.info(f"   {prefix_tag}Runner Trailing: {cfg.enable_runner_trailing} (Buffer: {trail_desc} | T2: {cfg.runner_target_2_multiplier:.2f}R)")
    logger.info(f"   {prefix_tag}Polling Cycle: Two-Tier High-Velocity (Tier 1 Macro: {cfg.scan_interval_seconds}s | Tier 2 Micro: {cfg.micro_polling_interval_seconds}s | In-Trade: {cfg.active_interval_seconds}s)")
    logger.info(f"🎯 {prefix_tag}Execution Sizing: FLAT RISK (Compounding Strategy Removed)")
    logger.info(f"   {prefix_tag}Capital Baseline: $1,000.00 | Fixed Risk: ${cfg.fixed_risk_usd:.2f} per trade")
    logger.info(f"   {prefix_tag}Expected Target 1 Gain: +${cfg.fixed_risk_usd * cfg.min_risk_reward:.2f} ({cfg.min_risk_reward:.2f}R)")
    logger.info("=" * 60)

    if args.once or not args.loop:
        result = trader.evaluate_and_trade()
        logger.info(f"{prefix_tag}Scan Decision: {result.get('decision')}")
        logger.info(f"{prefix_tag}Summary: Price=${result.get('current_price')} | Action={result.get('action')} | Bias={result.get('bias')} | R:R={result.get('risk_reward_ratio')}R")
        if result.get("order_result"):
            logger.info(f"{prefix_tag}Order Result: {result.get('order_result')}")
        return

    def sleep_with_stop(seconds: float):
        end_time = time.time() + seconds
        while time.time() < end_time:
            if stop_event and stop_event.is_set():
                break
            time.sleep(min(0.5, max(0.05, end_time - time.time())))

    logger.info(f"{prefix_tag}Entering continuous monitoring loop (Idle: {cfg.scan_interval_seconds}s | In-Trade: {cfg.active_interval_seconds}s)...")
    cycle = 1
    in_trade_ticks = 0

    while not (stop_event and stop_event.is_set()):
        try:
            matching_positions = trader.get_matching_open_positions()
            is_in_trade = len(matching_positions) > 0

            if is_in_trade and cfg.enable_adaptive_dual_speed:
                in_trade_ticks += 1
                res = trader.manage_active_positions()
                managed_actions = res.get("managed_actions", [])

                if managed_actions:
                    logger.info(f"🛡️ {prefix_tag}[In-Trade Fast Tick #{in_trade_ticks}] ACTION: {managed_actions}")
                elif in_trade_ticks % 12 == 1:
                    p_info = matching_positions[0] if matching_positions else {}
                    cur_p = float(p_info.get("current_price", 0.0))
                    op_p = float(p_info.get("open_price", 0.0))
                    lots = float(p_info.get("lots", 0.0))
                    sl_val = float(p_info.get("sl", 0.0))
                    profit = float(p_info.get("pnl") if p_info.get("pnl") is not None else p_info.get("profit", 0.0))
                    logger.info(
                        f"⚡ {prefix_tag}[In-Trade Fast Cycle #{in_trade_ticks}] Price: ${cur_p:.2f} | Entry: ${op_p:.2f} | "
                        f"SL: ${sl_val:.2f} | Lots: {lots:.2f} | Floating PnL: ${profit:+.2f} | Active Polling {cfg.active_interval_seconds}s"
                    )

                if res.get("open_positions_count", 0) == 0:
                    logger.info(f"🏁 {prefix_tag}Active position closed! Transitioning back to normal {cfg.scan_interval_seconds}s market scan.")
                    in_trade_ticks = 0
                    sleep_with_stop(cfg.active_interval_seconds)
                    continue

                macro_interval_ticks = max(1, cfg.scan_interval_seconds // cfg.active_interval_seconds)
                if (
                    cfg.allow_runner_compounding
                    and (in_trade_ticks % macro_interval_ticks == 0)
                    and (cfg.max_open_positions == 0 or len(matching_positions) < cfg.max_open_positions)
                ):
                    unhedged = trader.get_unhedged_open_positions()
                    if len(unhedged) == 0:
                        logger.info(f"\n--- {prefix_tag}[Cycle #{cycle} | Runner Compounding Macro Scan] All active positions are Risk-Free Runners! Scanning {cfg.symbol} for next entry ---")
                        scan_res = trader.evaluate_and_trade()
                        logger.info(f"{prefix_tag}Runner Compounding Decision: {scan_res.get('decision')} | Price: ${scan_res.get('current_price')} | Score: {scan_res.get('quality_score')}")
                        if scan_res.get("order_result"):
                            logger.info(f"⚡ {prefix_tag}RUNNER COMPOUNDING ORDER EVENT: {scan_res.get('order_result')}")
                        cycle += 1

                sleep_with_stop(cfg.active_interval_seconds)
            else:
                in_trade_ticks = 0
                logger.info(f"\n--- {prefix_tag}[Cycle #{cycle} | Tier 1 Macro Scan] Scanning {cfg.symbol} Market @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")
                res = trader.evaluate_and_trade()
                logger.info(f"{prefix_tag}Price: ${res.get('current_price')} | Trend: {res.get('bias')} | Setup: {res.get('setup_name')} | Score: {res.get('quality_score')}")
                logger.info(f"{prefix_tag}Decision: {res.get('decision')}")
                if res.get("order_result"):
                    logger.info(f"⚡ {prefix_tag}ORDER EVENT: {res.get('order_result')}")
                if res.get("managed_actions"):
                    logger.info(f"🛡️ {prefix_tag}POSITION MANAGEMENT: {res.get('managed_actions')}")

                cycle += 1
                order_res = res.get("order_result")
                if order_res and order_res.get("status") == "SUCCESS":
                    logger.info(f"🚀 {prefix_tag}New position entered! Engaging {cfg.active_interval_seconds}-second fast position management loop.")
                    sleep_with_stop(cfg.active_interval_seconds)
                    continue

                armed_trigger = res.get("armed_trigger")
                if armed_trigger and getattr(cfg, "enable_two_tier_polling", True):
                    trig_act = armed_trigger["action"]
                    trig_px = armed_trigger["trigger_price"]
                    trig_cond = armed_trigger.get("trigger_condition", "BID_GTE" if trig_act == "BUY" else "ASK_LTE")
                    ml_p = armed_trigger.get("ml_prob", 0.50)
                    logger.info(
                        f"🎯 {prefix_tag}[Tier 2 Micro Loop Active] Armed {cfg.symbol} {trig_act} trigger @ ${trig_px:.2f} "
                        f"(Cond: {trig_cond}, ML: {ml_p:.1%}) | Polling live ticks every {cfg.micro_polling_interval_seconds}s..."
                    )

                    tier2_ticks = 0
                    max_ticks = max(10, int(cfg.scan_interval_seconds / max(0.5, cfg.micro_polling_interval_seconds)))
                    triggered = False

                    while tier2_ticks < max_ticks and not (stop_event and stop_event.is_set()):
                        sleep_with_stop(cfg.micro_polling_interval_seconds)
                        tier2_ticks += 1

                        sym_info = trader.broker.get_symbol_info(cfg.symbol)
                        if not sym_info or sym_info.get("status") != "SUCCESS":
                            continue

                        bid = float(sym_info.get("bid", 0.0))
                        ask = float(sym_info.get("ask", 0.0))
                        if bid <= 0 or ask <= 0:
                            continue

                        is_hit = False
                        if trig_cond == "BID_GTE" and bid >= trig_px:
                            is_hit = True
                        elif trig_cond == "ASK_LTE" and ask <= trig_px:
                            is_hit = True

                        if is_hit:
                            logger.info(
                                f"⚡ {prefix_tag}[Tier 2 Trigger Hit @ Tick #{tier2_ticks}] {cfg.symbol} {trig_act} Touch Detected! "
                                f"Trigger: ${trig_px:.2f} | Live Bid: ${bid:.2f} | Live Ask: ${ask:.2f} | Execution Latency: <{cfg.micro_polling_interval_seconds}s"
                            )
                            exec_res = trader.execute_armed_order(armed_trigger, sym_info)
                            ord_res = exec_res.get("order_result")
                            if ord_res and ord_res.get("status") == "SUCCESS":
                                logger.info(
                                    f"🚀 {prefix_tag}[Tier 2 Order Executed] Ticket #{ord_res.get('ticket')} @ ${ord_res.get('price')} "
                                    f"(Lots: {ord_res.get('lots')}, SL: ${ord_res.get('sl')}, TP: ${ord_res.get('tp')})"
                                )
                                triggered = True
                                break
                            else:
                                logger.error(f"{prefix_tag}Tier 2 execution failed: {exec_res.get('message')}")
                                break

                    if triggered:
                        sleep_with_stop(cfg.active_interval_seconds)
                else:
                    sleep_with_stop(cfg.scan_interval_seconds)
        except KeyboardInterrupt:
            logger.info(f"\n{prefix_tag}Auto-Trader stopped by user.")
            break
        except Exception as e:
            logger.error(f"{prefix_tag}Error in scan cycle: {e}", exc_info=True)
            sleep_with_stop(10)


def main():
    parser = argparse.ArgumentParser(description="24/7 Multi-Asset Live Scanner & Autonomous Auto-Trader")
    parser.add_argument("--symbol", default="XAUUSD", help="Trading symbol (XAUUSD / GOLD)")
    parser.add_argument("--symbols", default=None, help="Comma-separated trading symbols (e.g. XAUUSD,BTCUSD,ETHUSD)")
    parser.add_argument("--timeframe", default="1h", help="Candle timeframe (5m, 15m, 1h)")
    parser.add_argument(
        "--mode",
        default="FLAT_RISK_TOUCH",
        choices=["FLAT_RISK_TOUCH", "STANDARD_SWING", "DONCHIAN_BREAKOUT", "DONCHIAN_MOMENTUM_HYBRID", "COMPOUNDING_TOUCH", "COMPOUNDING_LADDER", "COMPOUNDING_HYBRID"],
        help="Trading mode (default: FLAT_RISK_TOUCH)",
    )
    parser.add_argument("--interval-seconds", type=int, default=60, help="Scan interval in seconds")
    parser.add_argument("--active-interval-seconds", type=int, default=5, help="Scan interval when in active trade (default: 5s)")
    parser.add_argument("--adaptive-dual-speed", action=argparse.BooleanOptionalAction, default=True, help="Enable adaptive dual-speed polling (60s idle / 5s in-trade)")
    parser.add_argument("--flat-risk", action=argparse.BooleanOptionalAction, default=True, help="Enforce fixed dollar risk execution (Compounding Removed)")
    parser.add_argument("--fixed-risk-usd", type=float, default=75.44, help="Fixed dollar risk per trade (default: $75.44)")
    parser.add_argument("--starting-capital", "--initial-capital", type=float, default=None, help="[Deprecated/Ignored] Starting capital")
    parser.add_argument("--ladder-level", type=int, default=None, help="[Deprecated/Ignored] Ladder level")
    parser.add_argument("--min-rr", type=float, default=1.25, help="Minimum Risk-Reward ratio (e.g. 1.25 for High-Velocity Touch)")
    parser.add_argument("--risk-pct", type=float, default=8.0, help="Risk percentage per trade (default: 8.0%%)")
    parser.add_argument("--donchian-period", type=int, default=10, help="Donchian channel lookback period (default: 10)")
    parser.add_argument("--trend-filter-ema", type=int, default=50, help="Trend filter EMA period (50 or 200, default: 50)")
    parser.add_argument("--trigger-mode", default="TOUCH", choices=["TOUCH", "CLOSE"], help="Trigger mode: TOUCH or CLOSE (default: TOUCH)")
    parser.add_argument("--min-quality", type=int, default=80, help="Minimum setup quality score (0-100)")
    parser.add_argument("--max-positions", type=int, default=2, help="Max open positions limit (default: 2)")
    parser.add_argument("--max-unhedged-positions", type=int, default=1, help="Max open positions with active unhedged risk (default: 1)")
    parser.add_argument("--allow-runner-compounding", action=argparse.BooleanOptionalAction, default=True, help="Permit entering new trade once existing trade has locked Break-Even")
    parser.add_argument("--dry-run", action="store_true", help="Analyze and evaluate without placing real orders")
    parser.add_argument("--once", action="store_true", help="Run a single evaluation cycle and exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a 24/7 monitoring loop")
    parser.add_argument("--one-trade-per-bar", action=argparse.BooleanOptionalAction, default=True, help="Enforce maximum 1 trade execution per candle bar")
    parser.add_argument("--loss-cooldown-minutes", type=int, default=60, help="Pause scanning/trading for N minutes after a stop loss (default: 60)")
    parser.add_argument("--use-pullback-limit", action=argparse.BooleanOptionalAction, default=False, help="Execute via Pullback Limit order")
    parser.add_argument("--pullback-discount-pips", type=float, default=0.0, help="Pips discount into EMA Value Zone (default: 0.0)")
    parser.add_argument("--max-daily-trades", type=int, default=6, help="Maximum trades executed per calendar day (default: 6)")
    parser.add_argument("--session-filter", action=argparse.BooleanOptionalAction, default=True, help="Restrict entries to 07:00-19:00 UTC London/NY hours")
    parser.add_argument("--enable-partial-scale-out", action=argparse.BooleanOptionalAction, default=True, help="Enable 50%% scale-out at Target 1 (+1.0R)")
    parser.add_argument("--partial-scale-out-pct", type=float, default=0.50, help="Partial scale-out percentage (default: 0.50)")
    parser.add_argument("--scale-out-r", type=float, default=1.00, help="Target 1 R-multiple for partial scale out (default: 1.00)")
    parser.add_argument("--enable-runner-trailing", action=argparse.BooleanOptionalAction, default=True, help="Enable dynamic runner trailing after Target 1")
    parser.add_argument("--runner-trail-buffer", type=float, default=4.50, help="Runner trailing buffer in USD (default: 4.50 for Gold, 350.00 for BTC, 10.00 for ETH)")
    parser.add_argument("--runner-trail-buffer-r", type=float, default=None, help="Runner trailing stop buffer in R-multiples (default: None for fixed USD buffer)")
    parser.add_argument("--runner-target-2-multiplier", type=float, default=3.00, help="Target 2 extension multiplier (default: 3.00)")
    parser.add_argument("--enable-news-catalyst-guard", action=argparse.BooleanOptionalAction, default=True, help="Pause trades ±15m around high-impact US macro events (CPI, NFP, FOMC)")
    parser.add_argument("--news-guard-buffer-minutes", type=int, default=15, help="Minutes blackout buffer before/after high-impact news (default: 15)")
    parser.add_argument("--enable-ml-filter", action=argparse.BooleanOptionalAction, default=False, help="Enable Machine Learning win probability filter gate (default: False)")
    parser.add_argument("--ml-min-prob", type=float, default=0.50, help="Minimum win probability threshold for ML filter (default: 0.50)")
    parser.add_argument("--ml-dynamic-sizing", action=argparse.BooleanOptionalAction, default=False, help="Enable dynamic ML confidence-weighted position sizing (default: False)")
    parser.add_argument("--ml-model-path", default=None, help="Custom path to ML model joblib file")
    parser.add_argument("--adx-min-threshold", type=float, default=None, help="Minimum 1H ADX trend strength threshold (default: 22.0 for ETH, None for others)")
    parser.add_argument("--require-slope-alignment", action=argparse.BooleanOptionalAction, default=True, help="Require 50 EMA slope alignment in trade direction (default: True)")
    parser.add_argument("--ladder-state-file", default=None, help="[Deprecated/Ignored] Path to ladder state JSON file")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database file")
    parser.add_argument("--order-tag", default=None, help="Order comment tag for multi-engine isolation (e.g. YieldA)")
    parser.add_argument("--bridge-port", type=int, default=8888, help="TCP port of the MT5 Socket Bridge (default: 8888)")
    parser.add_argument("--enable-two-tier", action="store_true", default=True, help="Enable Two-Tier 1-second high-velocity tick polling (default: True)")
    parser.add_argument("--no-two-tier", dest="enable_two_tier", action="store_false", help="Disable Two-Tier polling")
    parser.add_argument("--micro-interval", type=float, default=1.0, help="Micro polling interval in seconds for Tier 2 (default: 1.0s)")

    args = parser.parse_args()

    raw_symbols = args.symbols if args.symbols else args.symbol
    symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]

    if len(symbols) <= 1:
        run_symbol_daemon(symbols[0] if symbols else "XAUUSD", args, is_multi=False)
        return

    # Multi-symbol mode
    logger.info(f"🌐 Launching Multi-Asset Trading Engine for {symbols}...")
    if args.once or not args.loop:
        for s in symbols:
            run_symbol_daemon(s, args, is_multi=True)
        return

    stop_event = threading.Event()

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig} received, stopping all trader threads...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = []
    for s in symbols:
        t = threading.Thread(
            target=run_symbol_daemon,
            args=(s, args, stop_event, True),
            name=f"Trader-{s}",
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(1.0)  # staggered start for clean socket initialization

    logger.info(f"✅ All {len(symbols)} trading engines running. Press Ctrl+C to terminate.")
    try:
        while not stop_event.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, stopping all trader threads...")
        stop_event.set()

    for t in threads:
        t.join(timeout=5.0)
    logger.info("All trading engine threads have terminated.")


if __name__ == "__main__":
    main()

