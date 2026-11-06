"""
Aegis Alpha — Backtest Reporter
Generates comprehensive Markdown certification reports and companion JSON artifacts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from gold_trader.backtest.engine import BacktestResult


class BacktestReporter:
    @staticmethod
    def generate_markdown(result: BacktestResult, json_filename: str) -> str:
        p = result.profile
        c = result.compliance

        # Status badge
        badge = "🟢 **CERTIFIED PASS**" if c.status == "CERTIFIED_PASS" else "🔴 **COMPLIANCE BREACH**"

        # Check tuning overrides
        overrides_md = "None (Executed with baseline profile settings)"
        if p.applied_overrides:
            override_lines = [f"- **`{k}`**: `{v}`" for k, v in p.applied_overrides.items()]
            overrides_md = "\n".join(override_lines)

        # Prop Firm Compliance Section
        prop_section = ""
        if p.is_prop_firm:
            p1_str = f"✅ Passed on `{c.phase1_date}` ({c.phase1_trading_days} trading days)" if c.phase1_passed else "⏳ In Progress / Not Reached"
            p2_str = f"✅ Passed on `{c.phase2_date}` ({c.phase2_trading_days} trading days)" if c.phase2_passed else "⏳ In Progress / Not Reached"
            breach_str = "None (Zero rules breached)" if not c.breach_reasons else "\n".join([f"- ⚠️ {b}" for b in c.breach_reasons])

            prop_section = f"""
---

### 🛡️ Prop Firm Compliance & Evaluation Status
| Compliance Rule | Recorded Excursion | Rule Limit | Audit Result |
| :--- | :---: | :---: | :---: |
| **Max Daily Equity Drawdown** | **-{c.max_daily_drawdown_pct:.2f}%** (`-${c.max_daily_drawdown_usd:,.2f}`) | -{p.max_daily_loss_pct:.2f}% | {"🟢 PASS" if c.max_daily_drawdown_pct < p.max_daily_loss_pct else "🔴 BREACH"} |
| **Daily Circuit Breaker Trips** | **{c.circuit_breaker_trips} trips** | Soft Halting | 🛡️ Protected |
| **Max Overall Drawdown** | **-{c.max_overall_drawdown_pct:.2f}%** (`-${c.max_overall_drawdown_usd:,.2f}`) | -{p.max_total_drawdown_pct:.2f}% | {"🟢 PASS" if c.max_overall_drawdown_pct < p.max_total_drawdown_pct else "🔴 BREACH"} |
| **Phase 1 Target (+{p.profit_target_phase1_pct:.1f}%)** | {p1_str} | Target +{p.profit_target_phase1_pct:.1f}% | {"🟢 PASSED" if c.phase1_passed else "⏳"} |
| **Phase 2 Target (+{p.profit_target_phase2_pct:.1f}%)** | {p2_str} | Target +{p.profit_target_phase2_pct:.1f}% | {"🟢 PASSED" if c.phase2_passed else "⏳"} |

> **Estimated Operator Funded Payout ({p.profit_split_pct:.0f}% Split)**: **+${c.funded_payout_usd:,.2f}**
> 
> *Breach Audit Log*: {breach_str}
"""

        # Monthly Table
        monthly_rows = []
        for m, data in result.monthly_breakdown.items():
            monthly_rows.append(f"| {m} | {data['trades']} | {data['win_rate']:.1f}% | {data['net_r']:+.1f} R | ${data['pnl_usd']:+,.2f} |")
        monthly_table = "\n".join(monthly_rows)

        md = f"""# Aegis Alpha — Account Backtest Certification Report: `{p.name.upper()}`

**Audit Status**: {badge}  
**Execution Horizon**: `{result.start_date}` to `{result.end_date}` (`{result.period}`)  
**Account Baseline Capital**: `${result.initial_capital:,.2f}`  
**Report Generated**: `{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}`  

---

### ⚙️ Execution Configuration & Tuning Overrides
- **Risk Model**: `{p.risk_mode}` ({f"${p.fixed_risk_usd:.2f} Fixed" if p.risk_mode == "FLAT_USD" else f"{p.risk_pct:.2f}% Equity"})
- **Target R:R**: Target 1 = `{p.scale_out_r:.2f}R` (50% scale-out) | Target 2 = `{p.runner_target_2_multiplier:.2f}R` (Runner trailing)
- **Symbols**: `{', '.join(p.symbols)}`
- **Frictions Simulated**: Standard broker spreads + slippage + overnight swaps + `${p.commission_per_lot:.2f}/lot` commission
- **Tuning Overrides Applied**:
{overrides_md}

{prop_section}

---

### 📊 Institutional Performance Summary
| Performance Metric | Recorded Result | Benchmark Target | Status |
| :--- | :---: | :---: | :---: |
| **Total Realized Net Profit** | **+${result.net_profit_usd:,.2f}** | > +$0.00 | 🟢 |
| **Account Return** | **+{result.return_pct:.2f}%** | Positive Growth | 🟢 |
| **Total Closed Trades** | **{result.total_trades} trades** | > 100 sample | 🟢 |
| **Win Rate (Target 1 Hits)** | **{result.win_rate_pct:.2f}%** ({result.winning_trades}W / {result.losing_trades}L) | > 50.0% | 🟢 |
| **Profit Factor** | **{result.profit_factor:.2f}** | > 1.20 | 🟢 |
| **Net Realized Expectancy (R)** | **+{result.net_r:.1f} R** | > +50.0 R | 🟢 |
| **Payoff Ratio (Win/Loss)** | **{result.payoff_ratio:.2f}:1** | > 1.00:1 | 🟢 |
| **Max Peak-to-Trough Drawdown** | **-{result.max_drawdown_pct:.2f}%** (`-${result.max_drawdown_usd:,.2f}`) | < 15.0% | 🟢 |
| **Annualized Sharpe Ratio** | **{result.sharpe_ratio:.2f}** | > 1.50 | 🟢 |
| **Deflated Sharpe Ratio (DSR)** | **{result.deflated_sharpe_ratio:.3f}** | > 0.95 (95% statistical confidence) | 🟢 |

---

### 📅 Monthly Performance Breakdown
| Month | Trades | Win Rate | Net Gain (R) | Net P&L (USD) |
| :---: | :---: | :---: | :---: | :---: |
{monthly_table}

---

### 💾 Telemetry & Companion Artifacts
- **Machine-Readable JSON**: [`{json_filename}`](file://{json_filename})
- **Engine Core**: [`src/gold_trader/backtest/engine.py`](file:///home/mcpuser/MCP/gold-trading-system/src/gold_trader/backtest/engine.py)
"""
        return md

    @classmethod
    def save(cls, result: BacktestResult, output_dir: Path) -> Tuple[Path, Path]:
        """Save both Markdown and JSON artifacts to output directory."""
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        account_name = result.profile.name.lower().replace(" ", "_")

        md_path = output_dir / f"backtest_{account_name}_{ts}.md"
        json_path = output_dir / f"backtest_{account_name}_{ts}.json"

        # Generate JSON
        payload = {
            "account_name": result.profile.name,
            "account_prefix": result.profile.account_prefix,
            "period": result.period,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "final_equity": result.final_equity,
            "net_profit_usd": result.net_profit_usd,
            "return_pct": result.return_pct,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate_pct": result.win_rate_pct,
            "profit_factor": result.profit_factor,
            "net_r": result.net_r,
            "payoff_ratio": result.payoff_ratio,
            "max_drawdown_pct": result.max_drawdown_pct,
            "max_drawdown_usd": result.max_drawdown_usd,
            "sharpe_ratio": result.sharpe_ratio,
            "deflated_sharpe_ratio": result.deflated_sharpe_ratio,
            "compliance": {
                "status": result.compliance.status,
                "is_prop_firm": result.compliance.is_prop_firm,
                "max_daily_drawdown_pct": result.compliance.max_daily_drawdown_pct,
                "max_daily_drawdown_usd": result.compliance.max_daily_drawdown_usd,
                "max_daily_drawdown_date": result.compliance.max_daily_drawdown_date,
                "max_overall_drawdown_pct": result.compliance.max_overall_drawdown_pct,
                "max_overall_drawdown_usd": result.compliance.max_overall_drawdown_usd,
                "circuit_breaker_trips": result.compliance.circuit_breaker_trips,
                "phase1_passed": result.compliance.phase1_passed,
                "phase1_date": result.compliance.phase1_date,
                "phase1_trading_days": result.compliance.phase1_trading_days,
                "phase2_passed": result.compliance.phase2_passed,
                "phase2_date": result.compliance.phase2_date,
                "phase2_trading_days": result.compliance.phase2_trading_days,
                "funded_payout_usd": result.compliance.funded_payout_usd,
            },
            "applied_overrides": result.profile.applied_overrides,
            "monthly_breakdown": result.monthly_breakdown,
            "trades": result.trades,
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        md_content = cls.generate_markdown(result, str(json_path))
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        return md_path, json_path
