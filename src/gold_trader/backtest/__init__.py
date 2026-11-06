"""
Aegis Alpha — Decoupled Account & Prop Firm Backtesting Engine
Provides institutional backtesting, parameter tuning sweeps, and prop firm compliance audits.
"""

from gold_trader.backtest.profile_loader import AccountProfile, load_profile
from gold_trader.backtest.compliance import PropFirmAuditor, ComplianceResult
from gold_trader.backtest.engine import AccountBacktestEngine, BacktestResult
from gold_trader.backtest.reporter import BacktestReporter

__all__ = [
    "AccountProfile",
    "load_profile",
    "PropFirmAuditor",
    "ComplianceResult",
    "AccountBacktestEngine",
    "BacktestResult",
    "BacktestReporter",
]
