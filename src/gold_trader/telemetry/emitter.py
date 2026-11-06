"""
Aegis Alpha — Non-Blocking High-Velocity Telemetry Emitter
Pushes real-time trading events to the Telemetry Bus with zero thread stalling and fail-safe error isolation.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Dict, Optional

from gold_trader.telemetry.protocol import (
    AccountEvent,
    HeartbeatEvent,
    ScanEvent,
    TradeEvent,
)

logger = logging.getLogger(__name__)

# Channel Names
CHANNEL_HEARTBEAT = "aegis.telemetry.heartbeat"
CHANNEL_SCAN = "aegis.telemetry.scan"
CHANNEL_TRADE = "aegis.telemetry.trade"
CHANNEL_ACCOUNT = "aegis.telemetry.account"

# Key Namespaces
KEY_NODE_HEARTBEAT = "aegis:nodes"
KEY_LATEST_SCANS = "aegis:scans"
KEY_LATEST_ACCOUNTS = "aegis:accounts"
KEY_RECENT_TRADES = "aegis:trades"


def _format_resp_publish(channel: str, message: str) -> bytes:
    """Format a Redis RESP PUBLISH command without external dependencies."""
    cmd = f"*3\r\n$7\r\nPUBLISH\r\n${len(channel)}\r\n{channel}\r\n${len(message.encode('utf-8'))}\r\n{message}\r\n"
    return cmd.encode("utf-8")


def _format_resp_hset(key: str, field: str, value: str) -> bytes:
    """Format a Redis RESP HSET command without external dependencies."""
    cmd = (
        f"*4\r\n$4\r\nHSET\r\n${len(key)}\r\n{key}\r\n"
        f"${len(field.encode('utf-8'))}\r\n{field}\r\n"
        f"${len(value.encode('utf-8'))}\r\n{value}\r\n"
    )
    return cmd.encode("utf-8")


class TelemetryEmitter:
    """
    Lightweight, fail-safe telemetry emitter.
    Guarantees:
    - Never raises exceptions into the trading thread.
    - Non-blocking (short connection/write timeouts).
    - Degrades gracefully into a no-op if telemetry bus is unreachable.
    """

    _instance: Optional[TelemetryEmitter] = None

    def __init__(
        self,
        node_id: Optional[str] = None,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        enabled: Optional[bool] = None,
    ):
        self.node_id = (
            node_id
            or os.getenv("CONTAINER_NODE_ID")
            or os.getenv("NODE_ID")
            or "aegis-host"
        )
        self.redis_host = (
            redis_host
            or os.getenv("REDIS_HOST")
            or os.getenv("TELEMETRY_HOST")
            or ""
        )
        self.redis_port = int(
            redis_port
            or os.getenv("REDIS_PORT")
            or os.getenv("TELEMETRY_PORT")
            or 6379
        )
        self.enabled = (
            enabled
            if enabled is not None
            else bool(self.redis_host and os.getenv("TELEMETRY_ENABLED", "true").lower() != "false")
        )

        self._redis_client: Any = None
        self._has_redis_pkg = False
        self._warned_disconnected = False
        self._start_time = time.time()

        if self.enabled:
            self._init_client()

    @classmethod
    def get_instance(cls, **kwargs: Any) -> TelemetryEmitter:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _init_client(self) -> None:
        try:
            import redis  # type: ignore

            self._redis_client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                socket_timeout=0.1,
                socket_connect_timeout=0.2,
                decode_responses=True,
            )
            self._has_redis_pkg = True
            logger.info("TelemetryEmitter connected via redis-py to %s:%d (Node: %s)", self.redis_host, self.redis_port, self.node_id)
        except ImportError:
            self._has_redis_pkg = False
            self._redis_client = None
            logger.info("TelemetryEmitter operating in raw socket RESP mode to %s:%d (Node: %s)", self.redis_host, self.redis_port, self.node_id)
        except Exception as e:
            logger.debug("TelemetryEmitter init failed: %s", e)
            self._redis_client = None

    def _send_raw_socket(self, payload: bytes) -> bool:
        if not self.redis_host:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect((self.redis_host, self.redis_port))
                s.sendall(payload)
                return True
        except Exception as e:
            if not self._warned_disconnected:
                logger.debug("Telemetry bus connection failed: %s", e)
                self._warned_disconnected = True
            return False

    def _publish_and_cache(self, channel: str, cache_key: str, field: str, json_str: str) -> None:
        if not self.enabled or not self.redis_host:
            return

        try:
            if self._has_redis_pkg and self._redis_client:
                # Use redis-py pipeline for atomic publish + cache
                p = self._redis_client.pipeline(transaction=False)
                p.publish(channel, json_str)
                p.hset(cache_key, field, json_str)
                p.execute()
                self._warned_disconnected = False
            else:
                # Raw socket fallback
                packet = _format_resp_publish(channel, json_str) + _format_resp_hset(cache_key, field, json_str)
                self._send_raw_socket(packet)
        except Exception as e:
            if not self._warned_disconnected:
                logger.debug("Failed publishing telemetry to %s: %s", channel, e)
                self._warned_disconnected = True

    # -------------------------------------------------------------------------
    # Public Telemetry Emitter Hooks
    # -------------------------------------------------------------------------
    def emit_heartbeat(
        self,
        symbol: str,
        status: str = "RUNNING",
        model_id: str = "model_1",
        account_id: Optional[str] = None,
        in_trade: bool = False,
        open_positions: int = 0,
        cpu_pct: float = 0.0,
        memory_mb: float = 0.0,
    ) -> None:
        """Broadcast node heartbeat and liveness."""
        if not self.enabled:
            return
        uptime = round(time.time() - self._start_time, 1)
        event = HeartbeatEvent(
            node_id=self.node_id,
            symbol=symbol,
            model_id=model_id,
            account_id=account_id,
            status=status,
            uptime_seconds=uptime,
            in_trade=in_trade,
            open_positions=open_positions,
            cpu_pct=cpu_pct,
            memory_mb=memory_mb,
        )
        field = f"{self.node_id}:{symbol}"
        self._publish_and_cache(CHANNEL_HEARTBEAT, KEY_NODE_HEARTBEAT, field, event.to_json())

    def emit_scan(
        self,
        symbol: str,
        price: float,
        bias: str,
        decision: str,
        model_id: str = "model_1",
        setup_name: str = "FLAT_RISK_TOUCH",
        quality_score: float = 0.0,
        atr: Optional[float] = None,
        rsi: Optional[float] = None,
        supertrend_direction: Optional[int] = None,
        donchian_upper: Optional[float] = None,
        donchian_lower: Optional[float] = None,
        donchian_middle: Optional[float] = None,
        ema_slope_bullish: Optional[bool] = None,
        ema_slope_bearish: Optional[bool] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Broadcast market scan outcome and indicator state."""
        if not self.enabled:
            return
        event = ScanEvent(
            node_id=self.node_id,
            symbol=symbol,
            price=price,
            bias=bias,
            decision=decision,
            model_id=model_id,
            setup_name=setup_name,
            quality_score=quality_score,
            atr=atr,
            rsi=rsi,
            supertrend_direction=supertrend_direction,
            donchian_upper=donchian_upper,
            donchian_lower=donchian_lower,
            donchian_middle=donchian_middle,
            ema_slope_bullish=ema_slope_bullish,
            ema_slope_bearish=ema_slope_bearish,
            reason=reason,
            details=details or {},
        )
        field = f"{self.node_id}:{model_id}:{symbol}"
        self._publish_and_cache(CHANNEL_SCAN, KEY_LATEST_SCANS, field, event.to_json())

    def emit_trade(
        self,
        symbol: str,
        ticket: int,
        action: str,
        lots: float,
        entry_price: float,
        status: str,
        model_id: str = "model_1",
        account_id: Optional[str] = None,
        current_price: Optional[float] = None,
        close_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target_1: Optional[float] = None,
        target_2: Optional[float] = None,
        pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        comment: Optional[str] = None,
        tp1_taken: bool = False,
        peak_price: Optional[float] = None,
        close_time: Optional[str] = None,
    ) -> None:
        """Broadcast trade execution, trailing change, or position close."""
        if not self.enabled:
            return
        event = TradeEvent(
            node_id=self.node_id,
            symbol=symbol,
            ticket=ticket,
            action=action,
            lots=lots,
            entry_price=entry_price,
            status=status,
            model_id=model_id,
            account_id=account_id,
            current_price=current_price,
            close_price=close_price,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            pnl=pnl,
            unrealized_pnl=unrealized_pnl,
            comment=comment,
            tp1_taken=tp1_taken,
            peak_price=peak_price,
            close_time=close_time,
        )
        json_str = event.to_json()
        if self._has_redis_pkg and self._redis_client:
            try:
                p = self._redis_client.pipeline(transaction=False)
                p.publish(CHANNEL_TRADE, json_str)
                # Keep last 500 recent trade events in a Redis list
                p.lpush(KEY_RECENT_TRADES, json_str)
                p.ltrim(KEY_RECENT_TRADES, 0, 499)
                p.execute()
                self._warned_disconnected = False
            except Exception as e:
                if not self._warned_disconnected:
                    logger.debug("Failed publishing trade telemetry: %s", e)
                    self._warned_disconnected = True
        else:
            self._send_raw_socket(_format_resp_publish(CHANNEL_TRADE, json_str))

    def emit_account(
        self,
        account_id: str,
        broker: str,
        balance: float,
        equity: float,
        free_margin: float,
        margin: float,
        currency: str = "USD",
        model_id: str = "model_1",
        open_positions_count: int = 0,
        unrealized_pnl: float = 0.0,
        realized_today: float = 0.0,
        connected: bool = True,
    ) -> None:
        """Broadcast account equity and risk posture."""
        if not self.enabled:
            return
        event = AccountEvent(
            node_id=self.node_id,
            account_id=account_id,
            broker=broker,
            balance=balance,
            equity=equity,
            free_margin=free_margin,
            margin=margin,
            currency=currency,
            model_id=model_id,
            open_positions_count=open_positions_count,
            unrealized_pnl=unrealized_pnl,
            realized_today=realized_today,
            connected=connected,
        )
        field = f"{self.node_id}:{model_id}:{account_id}"
        self._publish_and_cache(CHANNEL_ACCOUNT, KEY_LATEST_ACCOUNTS, field, event.to_json())
