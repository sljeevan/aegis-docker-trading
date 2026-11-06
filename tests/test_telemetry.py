"""
Unit Tests for Aegis Alpha Decoupled Telemetry Layer
Verifies:
1. Protocol event serialization and data contract schemas.
2. Non-blocking TelemetryEmitter with fail-safe error isolation when offline.
3. TelemetryStore in-memory zero-lock caching and query performance.
4. FastAPI telemetry endpoints (/api/telemetry/nodes, /api/telemetry/overview).
"""

import json
import pytest
from datetime import datetime, timezone

from gold_trader.telemetry.protocol import (
    AccountEvent,
    HeartbeatEvent,
    ScanEvent,
    TradeEvent,
)
from gold_trader.telemetry.emitter import TelemetryEmitter, _format_resp_publish, _format_resp_hset
from gold_trader.telemetry.store import TelemetryStore


def test_protocol_events_serialization():
    # 1. HeartbeatEvent
    hb = HeartbeatEvent(
        node_id="aegis-xm-live",
        symbol="XAUUSD",
        model_id="model_1",
        account_id="168987477",
        status="RUNNING",
        uptime_seconds=3600.5,
        in_trade=True,
        open_positions=1,
    )
    hb_json = hb.to_json()
    hb_recovered = HeartbeatEvent.from_dict(json.loads(hb_json))
    assert hb_recovered.node_id == "aegis-xm-live"
    assert hb_recovered.open_positions == 1
    assert hb_recovered.in_trade is True

    # 2. ScanEvent
    scan = ScanEvent(
        node_id="aegis-ftmo-100k",
        symbol="BTCUSD",
        price=68500.0,
        bias="BULLISH",
        decision="ARMED_DONCHIAN_TOUCH",
        atr=520.0,
        rsi=62.4,
        supertrend_direction=1,
        donchian_upper=68550.0,
        donchian_lower=67200.0,
        donchian_middle=67875.0,
    )
    scan_json = scan.to_json()
    scan_recovered = ScanEvent.from_dict(json.loads(scan_json))
    assert scan_recovered.price == 68500.0
    assert scan_recovered.supertrend_direction == 1
    assert scan_recovered.atr == 520.0

    # 3. TradeEvent
    trade = TradeEvent(
        node_id="aegis-xm-live",
        symbol="ETHUSD",
        ticket=2366233000,
        action="BUY",
        lots=1.93,
        entry_price=2450.0,
        status="CLOSED",
        pnl=83.18,
        comment="TP1_AND_TRAIL",
    )
    trade_json = trade.to_json()
    trade_recovered = TradeEvent.from_dict(json.loads(trade_json))
    assert trade_recovered.ticket == 2366233000
    assert trade_recovered.pnl == 83.18
    assert trade_recovered.status == "CLOSED"

    # 4. AccountEvent
    acc = AccountEvent(
        node_id="aegis-xm-live",
        account_id="168987477",
        broker="XM Global",
        balance=1000.0,
        equity=1083.18,
        free_margin=950.0,
        margin=50.0,
        unrealized_pnl=0.0,
        realized_today=7.28,
    )
    acc_json = acc.to_json()
    acc_recovered = AccountEvent.from_dict(json.loads(acc_json))
    assert acc_recovered.equity == 1083.18
    assert acc_recovered.realized_today == 7.28


def test_resp_formatting():
    # Test Redis RESP publish formatting
    pub_bytes = _format_resp_publish("test.chan", '{"hello":"world"}')
    assert b"*3\r\n$7\r\nPUBLISH\r\n$9\r\ntest.chan\r\n$17\r\n{\"hello\":\"world\"}\r\n" == pub_bytes

    # Test Redis RESP hset formatting
    hset_bytes = _format_resp_hset("test:key", "field1", "val1")
    assert b"*4\r\n$4\r\nHSET\r\n$8\r\ntest:key\r\n$6\r\nfield1\r\n$4\r\nval1\r\n" == hset_bytes


def test_emitter_fail_safe_when_offline():
    # Reset singleton to test offline host
    TelemetryEmitter.reset_instance()
    # Configure an unreachable IP/port with short timeout
    emitter = TelemetryEmitter(
        node_id="test-node",
        redis_host="127.0.0.1",
        redis_port=59999,
        enabled=True,
    )

    # All emit methods must execute without raising any exceptions
    emitter.emit_heartbeat(symbol="XAUUSD", status="RUNNING")
    emitter.emit_scan(
        symbol="XAUUSD",
        price=2750.0,
        bias="NEUTRAL",
        decision="SCANNING",
    )
    emitter.emit_trade(
        symbol="XAUUSD",
        ticket=12345,
        action="BUY",
        lots=0.05,
        entry_price=2750.0,
        status="OPEN",
    )
    emitter.emit_account(
        account_id="123",
        broker="TestBroker",
        balance=1000.0,
        equity=1000.0,
        free_margin=1000.0,
        margin=0.0,
    )

    # When disabled, it is a fast no-op
    emitter.enabled = False
    emitter.emit_heartbeat(symbol="XAUUSD")


def test_telemetry_store_in_memory():
    TelemetryStore.reset_instance()
    # Initialize store with no Redis (pure in-memory mode)
    store = TelemetryStore(redis_host="", redis_port=0)

    # Inject simulated events directly
    hb = HeartbeatEvent(
        node_id="node-1",
        symbol="XAUUSD",
        model_id="model_1",
        status="RUNNING",
    )
    store.record_local_event("aegis.telemetry.heartbeat", hb)

    scan = ScanEvent(
        node_id="node-1",
        symbol="XAUUSD",
        price=2740.0,
        bias="BULLISH",
        decision="ARMED_TOUCH",
        model_id="model_1",
        atr=10.5,
        rsi=55.0,
    )
    store.record_local_event("aegis.telemetry.scan", scan)

    trade = TradeEvent(
        node_id="node-1",
        symbol="XAUUSD",
        ticket=99999,
        action="BUY",
        lots=0.05,
        entry_price=2740.0,
        status="CLOSED",
        pnl=94.50,
    )
    store.record_local_event("aegis.telemetry.trade", trade)

    # Verify query methods
    assert store.is_telemetry_active() is True
    nodes = store.get_nodes()
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "node-1"

    scans = store.get_scans()
    assert "XAUUSD" in scans["model_1"]
    assert scans["model_1"]["XAUUSD"]["price"] == 2740.0
    assert scans["model_1"]["XAUUSD"]["atr"] == 10.5

    trades = store.get_recent_trades(limit=10)
    assert len(trades) == 1
    assert trades[0]["ticket"] == 99999
    assert trades[0]["pnl"] == 94.50


def test_dashboard_api_telemetry_endpoints():
    from fastapi.testclient import TestClient
    from gold_trader.api.docker_dashboard_api import app

    client = TestClient(app)

    # Ensure store has data
    store = TelemetryStore.get_instance()
    hb = HeartbeatEvent(
        node_id="api-test-node",
        symbol="BTCUSD",
        model_id="model_1",
        status="RUNNING",
    )
    store.record_local_event("aegis.telemetry.heartbeat", hb)

    # 1. Test /api/telemetry/nodes
    resp_nodes = client.get("/api/telemetry/nodes")
    assert resp_nodes.status_code == 200
    data_nodes = resp_nodes.json()
    assert data_nodes["status"] == "SUCCESS"
    assert data_nodes["total_nodes"] >= 1

    # 2. Test /api/telemetry/overview
    resp_ov = client.get("/api/telemetry/overview")
    assert resp_ov.status_code == 200
    data_ov = resp_ov.json()
    assert data_ov["status"] == "SUCCESS"
    assert data_ov["active"] is True
    assert "nodes" in data_ov
    assert "scans" in data_ov
