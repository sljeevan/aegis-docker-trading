"""
Aegis Alpha — Telemetry In-Memory Store & Redis Consumer
Maintains live, zero-lock portfolio state in RAM and powers the decoupled API & UI layer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gold_trader.telemetry.emitter import (
    CHANNEL_ACCOUNT,
    CHANNEL_HEARTBEAT,
    CHANNEL_SCAN,
    CHANNEL_TRADE,
    KEY_LATEST_ACCOUNTS,
    KEY_LATEST_SCANS,
    KEY_NODE_HEARTBEAT,
    KEY_RECENT_TRADES,
)
from gold_trader.telemetry.protocol import (
    AccountEvent,
    HeartbeatEvent,
    ScanEvent,
    TradeEvent,
)

logger = logging.getLogger(__name__)


class TelemetryStore:
    """
    Central in-memory state store for the decoupled UI / API layer.
    Subscribes to Redis telemetry bus and serves instant, zero-lock reads to the frontend.
    """

    _instance: Optional[TelemetryStore] = None

    def __init__(
        self,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        max_trades_history: int = 500,
    ):
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
        self.max_trades_history = max_trades_history

        self._lock = threading.RLock()
        self._nodes: Dict[str, HeartbeatEvent] = {}
        self._scans: Dict[str, ScanEvent] = {}
        self._accounts: Dict[str, AccountEvent] = {}
        self._trades: deque = deque(maxlen=max_trades_history)

        self._subscriber_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._redis_client: Any = None
        self._connected = False

        if self.redis_host:
            self._init_redis()
            self._warmup_cache()
            self.start_subscriber()

    @classmethod
    def get_instance(cls, **kwargs: Any) -> TelemetryStore:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        if cls._instance:
            cls._instance.stop_subscriber()
        cls._instance = None

    def _init_redis(self) -> None:
        try:
            import redis  # type: ignore

            self._redis_client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                socket_timeout=1.0,
                decode_responses=True,
            )
            self._redis_client.ping()
            self._connected = True
            logger.info("TelemetryStore connected to Redis at %s:%d", self.redis_host, self.redis_port)
        except Exception as e:
            logger.warning("TelemetryStore Redis connection failed at %s:%d: %s", self.redis_host, self.redis_port, e)
            self._connected = False

    def _warmup_cache(self) -> None:
        """Pre-populate state from Redis hashes so state is available immediately upon boot."""
        if not self._connected or not self._redis_client:
            return

        try:
            # 1. Warm up heartbeats
            nodes_data = self._redis_client.hgetall(KEY_NODE_HEARTBEAT)
            with self._lock:
                for k, v in nodes_data.items():
                    try:
                        self._nodes[k] = HeartbeatEvent.from_dict(json.loads(v))
                    except Exception:
                        pass

            # 2. Warm up scans
            scans_data = self._redis_client.hgetall(KEY_LATEST_SCANS)
            with self._lock:
                for k, v in scans_data.items():
                    try:
                        self._scans[k] = ScanEvent.from_dict(json.loads(v))
                    except Exception:
                        pass

            # 3. Warm up accounts
            accounts_data = self._redis_client.hgetall(KEY_LATEST_ACCOUNTS)
            with self._lock:
                for k, v in accounts_data.items():
                    try:
                        self._accounts[k] = AccountEvent.from_dict(json.loads(v))
                    except Exception:
                        pass

            # 4. Warm up recent trades
            trades_raw = self._redis_client.lrange(KEY_RECENT_TRADES, 0, self.max_trades_history - 1)
            with self._lock:
                for v in reversed(trades_raw):
                    try:
                        self._trades.append(TradeEvent.from_dict(json.loads(v)))
                    except Exception:
                        pass

            logger.info(
                "TelemetryStore cache warmup complete: %d nodes, %d scans, %d accounts, %d trades",
                len(self._nodes),
                len(self._scans),
                len(self._accounts),
                len(self._trades),
            )
        except Exception as e:
            logger.warning("TelemetryStore warmup failed: %s", e)

    def start_subscriber(self) -> None:
        """Starts background thread to subscribe to telemetry pub/sub channels."""
        if not self._connected or not self._redis_client:
            return
        if self._subscriber_thread and self._subscriber_thread.is_alive():
            return

        self._stop_event.clear()
        self._subscriber_thread = threading.Thread(
            target=self._run_subscriber_loop,
            name="AegisTelemetrySubscriber",
            daemon=True,
        )
        self._subscriber_thread.start()

    def stop_subscriber(self) -> None:
        self._stop_event.set()
        if self._subscriber_thread and self._subscriber_thread.is_alive():
            self._subscriber_thread.join(timeout=1.0)
            self._subscriber_thread = None

    def _run_subscriber_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                pubsub = self._redis_client.pubsub()
                pubsub.psubscribe("aegis.telemetry.*")
                logger.info("TelemetryStore subscribed to aegis.telemetry.*")

                while not self._stop_event.is_set():
                    msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                    if msg and msg.get("data"):
                        channel = msg.get("channel", "")
                        data_str = msg.get("data")
                        self._process_message(channel, data_str)

            except Exception as e:
                logger.warning("Telemetry subscription error (will reconnect in 2s): %s", e)
                time.sleep(2.0)

    def _process_message(self, channel: str, data_str: str) -> None:
        try:
            data = json.loads(data_str)
        except Exception:
            return

        with self._lock:
            if channel == CHANNEL_HEARTBEAT:
                ev = HeartbeatEvent.from_dict(data)
                key = f"{ev.node_id}:{ev.symbol}"
                self._nodes[key] = ev
            elif channel == CHANNEL_SCAN:
                sc = ScanEvent.from_dict(data)
                key = f"{sc.node_id}:{sc.model_id}:{sc.symbol}"
                self._scans[key] = sc
            elif channel == CHANNEL_ACCOUNT:
                acc = AccountEvent.from_dict(data)
                key = f"{acc.node_id}:{acc.model_id}:{acc.account_id}"
                self._accounts[key] = acc
            elif channel == CHANNEL_TRADE:
                tr = TradeEvent.from_dict(data)
                self._trades.appendleft(tr)

    # -------------------------------------------------------------------------
    # Zero-Lock Query Methods for API & UI
    # -------------------------------------------------------------------------
    def is_telemetry_active(self, max_stale_seconds: int = 120) -> bool:
        """Returns True if at least one node is reporting active heartbeats."""
        if not self._nodes:
            return False
        now = datetime.now(timezone.utc)
        with self._lock:
            for n in self._nodes.values():
                try:
                    ts = datetime.fromisoformat(n.timestamp)
                    if (now - ts).total_seconds() < max_stale_seconds:
                        return True
                except Exception:
                    pass
        return False

    def get_nodes(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [n.to_dict() for n in self._nodes.values()]

    def get_scans(self) -> Dict[str, Dict[str, Any]]:
        """Returns scans organized by model_id and symbol."""
        result: Dict[str, Dict[str, Any]] = {"model_1": {}, "model_2": {}}
        with self._lock:
            for sc in self._scans.values():
                m_id = sc.model_id if sc.model_id in result else "model_1"
                result[m_id][sc.symbol] = {
                    "price": sc.price,
                    "bias": sc.bias,
                    "decision": sc.decision,
                    "atr": sc.atr,
                    "rsi": sc.rsi,
                    "supertrend": sc.supertrend_direction,
                    "donchian_upper": sc.donchian_upper,
                    "donchian_lower": sc.donchian_lower,
                    "donchian_middle": sc.donchian_middle,
                    "reason": sc.reason or sc.decision,
                    "timestamp": sc.timestamp[:19].replace("T", " "),
                }
        return result

    def get_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [acc.to_dict() for acc in self._accounts.values()]

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._trades)[:limit]
            return [t.to_dict() for t in items]

    def record_local_event(self, channel: str, event_obj: Any) -> None:
        """Allows in-process injection without Redis (e.g. testing or local fallback)."""
        self._process_message(channel, event_obj.to_json())
