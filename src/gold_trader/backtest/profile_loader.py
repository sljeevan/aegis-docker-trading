"""
Aegis Alpha — Account Profile Loader
Parses account profiles from docker/configs/*.env or preset definitions,
and merges tuning overrides cleanly in memory without modifying source files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AccountProfile:
    name: str
    account_prefix: str
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD"])
    starting_balance: float = 1000.0
    risk_mode: str = "FLAT_USD"  # 'FLAT_USD' or 'PERCENT'
    fixed_risk_usd: float = 75.44
    risk_pct: float = 0.40  # In percent, e.g. 0.40 means 0.40% ($400 on $100k)
    min_rr: float = 1.25
    scale_out_r: float = 1.25
    runner_trail_buffer: float = 4.50
    runner_target_2_multiplier: float = 2.00
    max_positions: int = 2
    max_unhedged_positions: int = 1
    allow_runner_compounding: bool = True
    require_slope_alignment: bool = True
    enable_ml_filter: bool = False
    ml_threshold: float = 0.50
    enable_session_filter: bool = True
    commission_per_lot: float = 0.0

    # Prop Firm Compliance Parameters
    is_prop_firm: bool = False
    max_daily_loss_pct: float = 5.00  # Hard daily breach threshold (e.g. 5.0% for FTMO)
    daily_circuit_breaker_pct: float = 2.50  # Soft breaker halting new entries
    max_total_drawdown_pct: float = 10.00  # Hard total loss limit
    trailing_drawdown: bool = False  # False for FTMO (static), True for The5ers (trailing)
    profit_target_phase1_pct: float = 10.00  # Phase 1 Target
    profit_target_phase2_pct: float = 5.00   # Phase 2 Target
    profit_split_pct: float = 80.00  # Operator profit split (80%)

    # Tracking any tuning overrides passed for this test run
    applied_overrides: Dict[str, Any] = field(default_factory=dict)

    def calculate_trade_risk_usd(self, current_equity: float) -> float:
        """Calculate planned dollar risk based on profile mode and current equity."""
        if self.risk_mode == "PERCENT":
            # If current equity is tracked, risk is based on starting balance or equity
            base = current_equity if current_equity > 0 else self.starting_balance
            return round(base * (self.risk_pct / 100.0), 2)
        return float(self.fixed_risk_usd)


# Preset account archetypes for instant invocation
PROFILE_PRESETS: Dict[str, Dict[str, Any]] = {
    "xm_live": {
        "account_prefix": "XM_LIVE",
        "symbols": ["XAUUSD", "BTCUSD", "ETHUSD"],
        "starting_balance": 1000.0,
        "risk_mode": "FLAT_USD",
        "fixed_risk_usd": 75.44,
        "min_rr": 2.00,
        "scale_out_r": 1.25,
        "runner_trail_buffer": 4.50,
        "runner_target_2_multiplier": 2.00,
        "commission_per_lot": 0.0,
        "is_prop_firm": False,
        "max_daily_loss_pct": 25.0,  # $250 soft daily stop
        "daily_circuit_breaker_pct": 15.0,
        "max_total_drawdown_pct": 100.0,
        "trailing_drawdown": False,
    },
    "ftmo_100k": {
        "account_prefix": "FTMO",
        "symbols": ["XAUUSD", "BTCUSD", "ETHUSD"],
        "starting_balance": 100000.0,
        "risk_mode": "PERCENT",
        "risk_pct": 0.40,  # 0.40% = $400 / trade
        "min_rr": 1.25,
        "scale_out_r": 1.25,
        "runner_trail_buffer": 4.50,
        "runner_target_2_multiplier": 2.00,
        "commission_per_lot": 3.00,  # $3/lot round-turn prop commission
        "is_prop_firm": True,
        "max_daily_loss_pct": 5.00,  # $5,000 max daily loss
        "daily_circuit_breaker_pct": 2.50,  # $2,500 daily circuit breaker
        "max_total_drawdown_pct": 10.00,  # $10,000 max overall drawdown
        "trailing_drawdown": False,  # FTMO is static initial balance
        "profit_target_phase1_pct": 10.00,  # $10,000
        "profit_target_phase2_pct": 5.00,   # $5,000
        "profit_split_pct": 80.00,
    },
    "the5ers_100k": {
        "account_prefix": "THE5ERS",
        "symbols": ["XAUUSD", "BTCUSD", "ETHUSD"],
        "starting_balance": 100000.0,
        "risk_mode": "PERCENT",
        "risk_pct": 0.30,  # 0.30% = $300 / trade
        "min_rr": 1.25,
        "scale_out_r": 1.25,
        "runner_trail_buffer": 4.50,
        "runner_target_2_multiplier": 2.00,
        "commission_per_lot": 0.0,  # Spread-only
        "is_prop_firm": True,
        "max_daily_loss_pct": 3.00,  # $3,000 max daily loss limit
        "daily_circuit_breaker_pct": 2.00,  # $2,000 circuit breaker
        "max_total_drawdown_pct": 6.00,  # 6% max trailing drawdown
        "trailing_drawdown": True,  # The5ers trails high-water mark
        "profit_target_phase1_pct": 10.00,
        "profit_target_phase2_pct": 5.00,
        "profit_split_pct": 80.00,
    },
    "blueguardian_50k": {
        "account_prefix": "BLUEGUARDIAN",
        "symbols": ["XAUUSD", "BTCUSD", "ETHUSD"],
        "starting_balance": 50000.0,
        "risk_mode": "PERCENT",
        "risk_pct": 0.50,  # 0.50% = $250 / trade
        "min_rr": 1.25,
        "scale_out_r": 1.25,
        "runner_trail_buffer": 4.50,
        "runner_target_2_multiplier": 2.00,
        "commission_per_lot": 2.00,
        "is_prop_firm": True,
        "max_daily_loss_pct": 4.00,  # 4% daily loss
        "daily_circuit_breaker_pct": 2.00,
        "max_total_drawdown_pct": 8.00,  # 8% max loss
        "trailing_drawdown": False,
        "profit_target_phase1_pct": 8.00,
        "profit_target_phase2_pct": 4.00,
        "profit_split_pct": 85.00,
    },
    "sandbox_demo": {
        "account_prefix": "XM_DEMO",
        "symbols": ["XAUUSD"],
        "starting_balance": 1000.0,
        "risk_mode": "FLAT_USD",
        "fixed_risk_usd": 75.44,
        "min_rr": 1.25,
        "scale_out_r": 1.25,
        "runner_trail_buffer": 4.50,
        "runner_target_2_multiplier": 3.00,
        "commission_per_lot": 0.0,
        "is_prop_firm": False,
        "max_daily_loss_pct": 25.0,
        "daily_circuit_breaker_pct": 15.0,
        "max_total_drawdown_pct": 100.0,
        "trailing_drawdown": False,
    },
}


def _parse_env_file(filepath: Path) -> Dict[str, str]:
    """Parse key=value pairs from a .env file."""
    env_vars: Dict[str, str] = {}
    if not filepath.exists():
        return env_vars
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env_vars[k] = v
    return env_vars


def load_profile(
    profile_name_or_path: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> AccountProfile:
    """
    Load an account profile by name (e.g. 'ftmo_100k', 'xm_live') or path to .env file,
    merging any runtime tuning overrides without altering the disk file.
    """
    profile_key = profile_name_or_path.lower().strip()
    if profile_key.endswith(".env"):
        profile_key = Path(profile_key).stem.lower()

    # Base attributes from presets if available
    base_data: Dict[str, Any] = PROFILE_PRESETS.get(profile_key, {}).copy()

    # Check for corresponding .env file in docker/configs/
    potential_paths = [
        Path(profile_name_or_path),
        Path("docker/configs") / f"{profile_key}.env",
        Path("/home/mcpuser/MCP/gold-trading-system/docker/configs") / f"{profile_key}.env",
    ]
    env_file = None
    for p in potential_paths:
        if p.exists() and p.is_file():
            env_file = p
            break

    if env_file:
        env_vars = _parse_env_file(env_file)
        if "ACCOUNT_PREFIX" in env_vars:
            base_data["account_prefix"] = env_vars["ACCOUNT_PREFIX"]
        if "SYMBOL" in env_vars:
            base_data["symbols"] = [env_vars["SYMBOL"]]
        if "RISK_MODE" in env_vars:
            base_data["risk_mode"] = env_vars["RISK_MODE"].upper()
        if "FIXED_RISK_USD" in env_vars:
            base_data["fixed_risk_usd"] = float(env_vars["FIXED_RISK_USD"])
        if "RISK_PCT" in env_vars:
            base_data["risk_pct"] = float(env_vars["RISK_PCT"])
        if "MIN_RR" in env_vars:
            base_data["min_rr"] = float(env_vars["MIN_RR"])
        if "SCALE_OUT_R" in env_vars:
            base_data["scale_out_r"] = float(env_vars["SCALE_OUT_R"])
        if "RUNNER_TRAIL_BUFFER" in env_vars:
            base_data["runner_trail_buffer"] = float(env_vars["RUNNER_TRAIL_BUFFER"])
        if "RUNNER_TARGET_2_MULTIPLIER" in env_vars:
            base_data["runner_target_2_multiplier"] = float(env_vars["RUNNER_TARGET_2_MULTIPLIER"])
        if "MAX_POSITIONS" in env_vars:
            base_data["max_positions"] = int(env_vars["MAX_POSITIONS"])
        if "MAX_UNHEDGED_POSITIONS" in env_vars:
            base_data["max_unhedged_positions"] = int(env_vars["MAX_UNHEDGED_POSITIONS"])
        if "ALLOW_RUNNER_COMPOUNDING" in env_vars:
            base_data["allow_runner_compounding"] = env_vars["ALLOW_RUNNER_COMPOUNDING"].lower() == "true"
        if "REQUIRE_SLOPE_ALIGNMENT" in env_vars:
            base_data["require_slope_alignment"] = env_vars["REQUIRE_SLOPE_ALIGNMENT"].lower() == "true"
        if "ENABLE_ML_FILTER" in env_vars:
            base_data["enable_ml_filter"] = env_vars["ENABLE_ML_FILTER"].lower() == "true"
        if "ENABLE_SESSION_FILTER" in env_vars:
            base_data["enable_session_filter"] = env_vars["ENABLE_SESSION_FILTER"].lower() == "true"

    # Default fallback values if neither preset nor .env provided everything
    base_data.setdefault("name", profile_key)
    base_data.setdefault("account_prefix", profile_key.upper())

    # Build dataclass instance
    profile = AccountProfile(**base_data)

    # Apply runtime tuning overrides (without touching disk)
    if overrides:
        for k, v in overrides.items():
            if hasattr(profile, k) and v is not None:
                setattr(profile, k, v)
                profile.applied_overrides[k] = v

    return profile


class ProfileLoader:
    """Convenience class wrapper for loading account profiles."""
    load_profile = staticmethod(load_profile)

