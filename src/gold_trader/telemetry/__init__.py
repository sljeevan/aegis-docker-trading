"""
Aegis Alpha — Telemetry Package
Decoupled multi-node telemetry streaming, push-based state synchronization, and UI offloading.
"""

from gold_trader.telemetry.emitter import TelemetryEmitter
from gold_trader.telemetry.protocol import (
    AccountEvent,
    HeartbeatEvent,
    ScanEvent,
    TradeEvent,
)
from gold_trader.telemetry.store import TelemetryStore

__all__ = [
    "TelemetryEmitter",
    "TelemetryStore",
    "HeartbeatEvent",
    "ScanEvent",
    "TradeEvent",
    "AccountEvent",
]
