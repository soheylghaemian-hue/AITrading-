"""Phase G1.8 — read-only Dashboard read-model.

Verifies build_dashboard_read_model() assembles account / positions / risk / system / AI purely from
persisted PostgreSQL state (+ the broker read-model), never fabricates, and never leaks secrets. It is
a READ path only: it touches no execution / broker-order / risk-engine / IBKR / autonomous code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from atp.dashboard.readmodel import build_dashboard_read_model
from atp.store import D, FillRow, PositionRow, new_id, open_store

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-08-16"


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates the full schema


def _seed_position(store, symbol, qty, avg, realized):
    store.apply_fill_atomic(
        fill=FillRow(fill_id=new_id(), client_order_id=new_id(), instrument=symbol, side="BUY",
                     quantity=D(qty), price=D(avg), commission=D("0"), ts="2026-08-16T09:00:00Z"),
        compute=lambda _cur: PositionRow(symbol, D(qty), D(avg), D(realized), "2026-08-16T09:00:00Z"))


def test_full_composition_from_persisted_state(store):
    store.upsert_risk_config(capital=D("1000000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("3"))
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1010000"), halted=False, killed=False)
    store.upsert_daily_pnl(trade_date=TODAY, day_start_equity=D("1000000"),
                           realized_pnl=D("-5000"), unrealized_pnl=D("-3200"))
    _seed_position(store, "AAPL", "100", "150.25", "1234.50")
    store.insert_decision(decision_id="d1", ts="2026-08-16T10:00:00Z", instrument="NVDA",
                          final_decision="APPROVED",
                          payload_json=json.dumps({"action": "BUY", "confidence": 0.87,
                                                   "entry": 100.5, "stop": 98, "target": 104}))
    broker = {"connection": "CONNECTED", "equity": 1000000.0, "cash": 500000.0, "currency": "EUR"}

    rm = build_dashboard_read_model(store, broker, now=NOW)

    # account
    assert rm["account"]["equity"] == 1000000.0
    assert rm["account"]["cash"] == 500000.0
    assert rm["account"]["pnl"] == pytest.approx(-8200.0)      # realized + unrealized
    assert rm["account"]["connected"] is True
    # positions
    assert len(rm["positions"]) == 1
    p = rm["positions"][0]
    assert (p["symbol"], p["quantity"], p["avg_price"], p["pnl"]) == ("AAPL", 100.0, 150.25, 1234.5)
    # risk (capital + drawdown, computed from peak vs live equity — never fabricated)
    assert rm["risk"]["capital"] == 1000000.0
    assert rm["risk"]["max_daily_loss_pct"] == 0.03
    assert rm["risk"]["drawdown"] == pytest.approx((1010000 - 1000000) / 1010000)
    assert rm["risk"]["daily_loss_pct"] == pytest.approx(8200 / 1000000)
    # ai decisions — payload merged with row fields
    assert len(rm["ai"]["decisions"]) == 1
    d = rm["ai"]["decisions"][0]
    assert d["instrument"] == "NVDA" and d["action"] == "BUY" and d["final_decision"] == "APPROVED"
    assert d["confidence"] == 0.87


def test_missing_state_is_no_data_never_fabricated(store):
    rm = build_dashboard_read_model(store, None, now=NOW)     # empty store, no broker
    assert rm["account"]["equity"] is None
    assert rm["account"]["cash"] is None
    assert rm["account"]["pnl"] is None
    assert rm["account"]["connected"] is False
    assert rm["positions"] == []
    assert rm["risk"] is None
    assert rm["ai"]["decisions"] == []
    assert rm["system"]["recovery_state"] is None


def test_secrets_never_appear_in_the_read_model(store):
    store.upsert_risk_config(capital=D("1000000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("3"))
    # a broker dict polluted with secret-like keys → the read-model must copy NONE of them
    broker = {"connection": "CONNECTED", "equity": 1000.0, "cash": 100.0,
              "password": "hunter2", "token": "sekret", "session": "abc", "username": "trader"}
    rm = build_dashboard_read_model(store, broker, now=NOW)
    blob = json.dumps(rm).lower()
    for secret in ("password", "hunter2", "token", "sekret", "session", "username", "trader"):
        assert secret not in blob


def test_broker_disconnected_yields_no_equity(store):
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1010000"), halted=False, killed=False)
    # even if a stale snapshot still carries numbers, a non-CONNECTED broker must not surface equity/cash
    broker = {"connection": "DISCONNECTED", "equity": 999999.0, "cash": 888888.0}
    rm = build_dashboard_read_model(store, broker, now=NOW)
    assert rm["account"]["equity"] is None
    assert rm["account"]["cash"] is None
    assert rm["account"]["connected"] is False
    assert rm["risk"]["drawdown"] is None                     # no live equity → drawdown NO DATA
