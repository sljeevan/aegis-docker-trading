"""
Aegis Alpha — Prop Firm Compliance Auditor
Audits account drawdown against institutional prop firm rules:
- Daily Starting Balance tracking (resets at 00:00 UTC)
- Soft Daily Circuit Breaker enforcement (halts entries during adverse days)
- Hard Daily Drawdown Breach detection (FTMO -5%, The5ers -3%, BlueGuardian -4%)
- Max Overall Trailing or Static Drawdown limits
- Evaluation Target certification (Phase 1, Phase 2) and operator payout estimation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from gold_trader.backtest.profile_loader import AccountProfile


@dataclass
class ComplianceResult:
    status: str  # 'CERTIFIED_PASS', 'BREACH_FAIL', 'TRIP_WARNING'
    is_prop_firm: bool
    max_daily_drawdown_pct: float
    max_daily_drawdown_usd: float
    max_daily_drawdown_date: str
    max_overall_drawdown_pct: float
    max_overall_drawdown_usd: float
    circuit_breaker_trips: int
    breach_reasons: List[str] = field(default_factory=list)
    phase1_passed: bool = False
    phase1_date: Optional[str] = None
    phase1_trading_days: Optional[int] = None
    phase2_passed: bool = False
    phase2_date: Optional[str] = None
    phase2_trading_days: Optional[int] = None
    funded_payout_usd: float = 0.0


class PropFirmAuditor:
    def __init__(self, profile: AccountProfile):
        self.profile = profile
        self.initial_balance = profile.starting_balance
        self.peak_equity = profile.starting_balance

        # Daily tracking
        self.current_day: Optional[str] = None
        self.day_start_equity = profile.starting_balance
        self.day_min_equity = profile.starting_balance
        self.daily_circuit_breaker_tripped = False
        self.total_circuit_breaker_trips = 0

        # Drawdown records
        self.max_daily_dd_pct = 0.0
        self.max_daily_dd_usd = 0.0
        self.max_daily_dd_date = ""

        self.max_overall_dd_pct = 0.0
        self.max_overall_dd_usd = 0.0

        # Breaches
        self.breaches: List[str] = []

        # Milestones
        self.phase1_passed = False
        self.phase1_date: Optional[str] = None
        self.phase1_day_count: Optional[int] = None

        self.phase2_passed = False
        self.phase2_date: Optional[str] = None
        self.phase2_day_count: Optional[int] = None

        self.seen_dates = set()

    def process_bar(self, date_str: str, current_equity: float):
        """Called on every bar to track equity curve and daily boundaries."""
        # Extract day string YYYY-MM-DD
        day = date_str[:10]
        self.seen_dates.add(day)

        if self.current_day != day:
            # New calendar day boundary
            self.current_day = day
            self.day_start_equity = current_equity
            self.day_min_equity = current_equity
            self.daily_circuit_breaker_tripped = False

        if current_equity < self.day_min_equity:
            self.day_min_equity = current_equity

        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        # 1. Check Daily Drawdown
        # Formula: (Day Start - Current Equity) / Day Start
        if self.day_start_equity > 0:
            daily_loss_usd = self.day_start_equity - current_equity
            daily_loss_pct = (daily_loss_usd / self.day_start_equity) * 100.0

            if daily_loss_pct > self.max_daily_dd_pct:
                self.max_daily_dd_pct = daily_loss_pct
                self.max_daily_dd_usd = daily_loss_usd
                self.max_daily_dd_date = date_str

            # Check soft circuit breaker
            if (
                self.profile.is_prop_firm
                and daily_loss_pct >= self.profile.daily_circuit_breaker_pct
                and not self.daily_circuit_breaker_tripped
            ):
                self.daily_circuit_breaker_tripped = True
                self.total_circuit_breaker_trips += 1

            # Check hard daily loss limit breach
            if (
                self.profile.is_prop_firm
                and daily_loss_pct >= self.profile.max_daily_loss_pct
            ):
                msg = f"Daily Loss Breach on {date_str}: -{daily_loss_pct:.2f}% (Limit: -{self.profile.max_daily_loss_pct:.2f}%)"
                if msg not in self.breaches:
                    self.breaches.append(msg)

        # 2. Check Overall Drawdown
        if self.profile.trailing_drawdown:
            # Trailing from peak equity (e.g. The5ers)
            overall_loss_usd = self.peak_equity - current_equity
            overall_loss_pct = (overall_loss_usd / self.peak_equity) * 100.0 if self.peak_equity > 0 else 0.0
        else:
            # Static from initial starting capital (e.g. FTMO)
            overall_loss_usd = self.initial_balance - current_equity
            overall_loss_pct = (overall_loss_usd / self.initial_balance) * 100.0

        if overall_loss_pct > self.max_overall_dd_pct:
            self.max_overall_dd_pct = overall_loss_pct
            self.max_overall_dd_usd = overall_loss_usd

        if (
            self.profile.is_prop_firm
            and overall_loss_pct >= self.profile.max_total_drawdown_pct
        ):
            msg = f"Max Overall Drawdown Breach on {date_str}: -{overall_loss_pct:.2f}% (Limit: -{self.profile.max_total_drawdown_pct:.2f}%)"
            if msg not in self.breaches:
                self.breaches.append(msg)

        # 3. Check Profit Targets
        if self.profile.is_prop_firm:
            net_gain_pct = ((current_equity - self.initial_balance) / self.initial_balance) * 100.0
            if not self.phase1_passed and net_gain_pct >= self.profile.profit_target_phase1_pct:
                self.phase1_passed = True
                self.phase1_date = date_str
                self.phase1_day_count = len(self.seen_dates)

            if (
                self.phase1_passed
                and not self.phase2_passed
                and net_gain_pct >= (self.profile.profit_target_phase1_pct + self.profile.profit_target_phase2_pct)
            ):
                self.phase2_passed = True
                self.phase2_date = date_str
                self.phase2_day_count = len(self.seen_dates)

    def is_trading_halted_today(self) -> bool:
        """Returns True if today's daily circuit breaker has been tripped."""
        return self.daily_circuit_breaker_tripped

    def finalize(self, final_equity: float) -> ComplianceResult:
        """Compute final compliance status and operator payout."""
        if not self.profile.is_prop_firm:
            status = "CERTIFIED_PASS"
        elif len(self.breaches) > 0:
            status = "BREACH_FAIL"
        elif self.total_circuit_breaker_trips > 0:
            status = "CERTIFIED_PASS"  # Breakers did their job preventing hard breaches
        else:
            status = "CERTIFIED_PASS"

        # Calculate operator payout if profitable
        net_profit = max(0.0, final_equity - self.initial_balance)
        payout = net_profit * (self.profile.profit_split_pct / 100.0)

        return ComplianceResult(
            status=status,
            is_prop_firm=self.profile.is_prop_firm,
            max_daily_drawdown_pct=round(self.max_daily_dd_pct, 2),
            max_daily_drawdown_usd=round(self.max_daily_dd_usd, 2),
            max_daily_drawdown_date=self.max_daily_dd_date,
            max_overall_drawdown_pct=round(self.max_overall_dd_pct, 2),
            max_overall_drawdown_usd=round(self.max_overall_dd_usd, 2),
            circuit_breaker_trips=self.total_circuit_breaker_trips,
            breach_reasons=self.breaches,
            phase1_passed=self.phase1_passed,
            phase1_date=self.phase1_date,
            phase1_trading_days=self.phase1_day_count,
            phase2_passed=self.phase2_passed,
            phase2_date=self.phase2_date,
            phase2_trading_days=self.phase2_day_count,
            funded_payout_usd=round(payout, 2),
        )
