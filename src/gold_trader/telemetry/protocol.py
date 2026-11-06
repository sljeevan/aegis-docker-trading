"""
Aegis Alpha — Telemetry Protocol & Event Schemas
Standardized data contracts for multi-node event streaming and decoupled UI telemetry.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HeartbeatEvent:
    """Periodic node health and liveness broadcast."""
    node_id: str
    symbol: str
    model_id: str = "model_1"
    account_id: Optional[str] = None
    status: str = "RUNNING"
    uptime_seconds: float = 0.0
    cpu_pct: float = 0.0
    memory_mb: float = 0.0
    in_trade: bool = False
    open_positions: int = 0
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HeartbeatEvent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ScanEvent:
    """Strategy cycle scan telemetry broadcast."""
    node_id: str
    symbol: str
    price: float
    bias: str
    decision: str
    model_id: str = "model_1"
    setup_name: str = "FLAT_RISK_TOUCH"
    quality_score: float = 0.0
    atr: Optional[float] = None
    rsi: Optional[float] = None
    supertrend_direction: Optional[int] = None
    donchian_upper: Optional[float] = None
    donchian_lower: Optional[float] = None
    donchian_middle: Optional[float] = None
    ema_slope_bullish: Optional[bool] = None
    ema_slope_bearish: Optional[bool] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ScanEvent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TradeEvent:
    """Order execution, trailing stop adjustment, or trade closure broadcast."""
    node_id: str
    symbol: str
    ticket: int
    action: str  # "BUY" or "SELL"
    lots: float
    entry_price: float
    status: str  # "OPEN", "CLOSED", "PARTIAL"
    model_id: str = "model_1"
    account_id: Optional[str] = None
    current_price: Optional[float] = None
    close_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    pnl: float = 0.0
    unrealized_pnl: float = 0.0
    comment: Optional[str] = None
    tp1_taken: bool = False
    peak_price: Optional[float] = None
    close_time: Optional[str] = None
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TradeEvent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AccountEvent:
    """Account balance, equity, and aggregate risk status broadcast."""
    node_id: str
    account_id: str
    broker: str
    balance: float
    equity: float
    free_margin: float
    margin: float
    currency: str = "USD"
    model_id: str = "model_1"
    open_positions_count: int = 0
    unrealized_pnl: float = 0.0
    realized_today: float = 0.0
    connected: bool = True
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AccountEvent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
