"""
Unit & Integration Tests for Aegis Alpha Account & Prop Firm Backtesting Engine
Verifies:
1. Profile loading from presets and .env files with in-memory tuning overrides.
2. Zero impact / file modification on live .env configuration files.
3. PropFirmAuditor daily drawdown tracking, circuit breaker trips, and breach detection.
4. End-to-end backtest simulation and report artifact generation.
"""

import json
from pathlib import Path
import pytest

from gold_trader.backtest.compliance import PropFirmAuditor
from gold_trader.backtest.engine import AccountBacktestEngine, BacktestResult
from gold_trader.backtest.profile_loader import AccountProfile, load_profile
from gold_trader.backtest.reporter import BacktestReporter


def test_profile_loading_presets():
    """Verify loading preconfigured account profiles."""
    # 1. XM Live
    xm = load_profile("xm_live")
    assert xm.account_prefix in ("XM_LIVE", "XM_YIELDA")
    assert xm.starting_balance == 1000.0
    assert xm.risk_mode == "FLAT_USD"
    assert xm.fixed_risk_usd == 75.44
    assert xm.is_prop_firm is False

    # 2. FTMO 100k
    ftmo = load_profile("ftmo_100k")
    assert ftmo.account_prefix == "FTMO"
    assert ftmo.starting_balance == 100000.0
    assert ftmo.risk_mode == "PERCENT"
    assert ftmo.risk_pct == 0.40
    assert ftmo.is_prop_firm is True
    assert ftmo.max_daily_loss_pct == 5.00
    assert ftmo.daily_circuit_breaker_pct == 2.50
    assert ftmo.max_total_drawdown_pct == 10.00
    assert ftmo.commission_per_lot == 3.00

    # 3. The5ers 100k
    the5ers = load_profile("the5ers_100k")
    assert the5ers.account_prefix == "THE5ERS"
    assert the5ers.trailing_drawdown is True
    assert the5ers.max_daily_loss_pct == 3.00


def test_tuning_overrides_isolation(tmp_path):
    """Verify tuning overrides apply in-memory without altering live .env files."""
    env_file = Path("docker/configs/ftmo_100k.env")
    initial_content = env_file.read_text(encoding="utf-8") if env_file.exists() else None

    # Load with aggressive tuning overrides
    overrides = {
        "risk_pct": 0.25,
        "min_rr": 1.75,
        "runner_target_2_multiplier": 3.50,
        "runner_trail_buffer": 6.00,
    }
    tuned_profile = load_profile("ftmo_100k", overrides=overrides)

    # Check that in-memory profile has tuned values
    assert tuned_profile.risk_pct == 0.25
    assert tuned_profile.min_rr == 1.75
    assert tuned_profile.runner_target_2_multiplier == 3.50
    assert tuned_profile.runner_trail_buffer == 6.00
    assert tuned_profile.applied_overrides == overrides

    # Verify that the physical file on disk was NOT touched
    if env_file.exists():
        post_content = env_file.read_text(encoding="utf-8")
        assert initial_content == post_content, "CRITICAL: Live .env file was modified by backtesting!"


def test_prop_firm_auditor_compliance_lifecycle():
    """Verify daily drawdown, circuit breaker trip, and breach detection."""
    profile = AccountProfile(
        name="test_prop",
        account_prefix="TEST",
        starting_balance=100000.0,
        is_prop_firm=True,
        max_daily_loss_pct=5.0,  # $5,000 max daily loss
        daily_circuit_breaker_pct=2.5,  # $2,500 daily breaker
        max_total_drawdown_pct=10.0,  # $10,000 max total loss
    )
    auditor = PropFirmAuditor(profile)

    # Day 1: Normal profit
    auditor.process_bar("2026-01-01 01:00", 100000.0)
    auditor.process_bar("2026-01-01 05:00", 101000.0)
    assert not auditor.is_trading_halted_today()

    # Day 2: Drawdown reaching 2.6% (Breaker tripped)
    auditor.process_bar("2026-01-02 00:00", 101000.0)
    # Day start equity is 101,000. Drawdown to 98,300 is (101000 - 98300)/101000 = 2.67%
    auditor.process_bar("2026-01-02 08:00", 98300.0)
    assert auditor.is_trading_halted_today()
    assert auditor.total_circuit_breaker_trips == 1

    # Day 3: Next day boundary resets the circuit breaker
    auditor.process_bar("2026-01-03 00:00", 98300.0)
    assert not auditor.is_trading_halted_today()

    # Day 4: Severe Drawdown reaching 5.2% (Hard Breach)
    auditor.process_bar("2026-01-04 00:00", 98300.0)
    # Day start equity is 98,300. Loss of $5,200 (equity 93,100) -> 5.29% daily loss
    auditor.process_bar("2026-01-04 10:00", 93100.0)
    res = auditor.finalize(final_equity=93100.0)

    assert res.status == "BREACH_FAIL"
    assert len(res.breach_reasons) > 0
    assert res.max_daily_drawdown_pct >= 5.0


def test_end_to_end_backtest_and_report_generation(tmp_path):
    """Verify end-to-end simulation on a single asset and report export."""
    profile = AccountProfile(
        name="test_sandbox",
        account_prefix="SANDBOX",
        symbols=["XAUUSD"],
        starting_balance=1000.0,
        risk_mode="FLAT_USD",
        fixed_risk_usd=75.44,
        min_rr=1.25,
        scale_out_r=1.25,
        runner_trail_buffer=4.50,
        runner_target_2_multiplier=2.00,
        is_prop_firm=False,
    )

    engine = AccountBacktestEngine(profile)
    # Run over 6-month horizon for rapid test execution
    result = engine.run(period="6mo")

    assert result.total_trades > 0
    assert result.win_rate_pct > 0.0
    assert result.initial_capital == 1000.0
    assert len(result.trades) == result.total_trades
    assert result.compliance.status == "CERTIFIED_PASS"

    # Test report writer
    md_path, json_path = BacktestReporter.save(result, tmp_path)
    assert md_path.exists()
    assert json_path.exists()

    # Validate JSON payload structure
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["account_name"] == "test_sandbox"
        assert data["total_trades"] == result.total_trades
        assert "compliance" in data
        assert "monthly_breakdown" in data
