"""Phase F1 — Broker Connector read-only guard + reconciliation fixtures (no live IBKR needed).

Proves, deterministically: execution is disabled and rejected BEFORE any IBKR order method; the broker
module contains no placeOrder/cancelOrder/modifyOrder call; and the reconciliation logic detects every
required mismatch class (which drives RECOVERY_REQUIRED / HALTED — never auto-repair).
"""
import inspect

import pytest

from atp.store import D
import atp.services.broker as brokermod
from atp.services.broker import BrokerConnector, ExecutionDisabled, execution_enabled, reconcile_state


# ---------------------------------------------------------------- read-only hard guard
def test_execution_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BROKER_EXECUTION_ENABLED", raising=False)
    assert execution_enabled() is False


def test_submit_order_rejected_before_ibkr(monkeypatch):
    monkeypatch.delenv("BROKER_EXECUTION_ENABLED", raising=False)
    with pytest.raises(ExecutionDisabled):
        BrokerConnector.submit_order(instrument="AAPL", side="BUY", quantity=D("1"))


def test_no_order_methods_in_source():
    src = inspect.getsource(brokermod)
    for forbidden in ("placeOrder", "cancelOrder", "modifyOrder"):
        assert forbidden not in src, f"{forbidden} must never appear in the read-only broker connector"


# ---------------------------------------------------------------- reconciliation fixtures
def test_reconcile_broker_equals_db_pass():
    ok, breaks = reconcile_state({"AAPL": D("10")}, {"AAPL": D("10")}, 0, 0)
    assert ok and breaks == []


def test_reconcile_empty_paper_pass():
    ok, breaks = reconcile_state({}, {}, 0, 0)
    assert ok and breaks == []


def test_reconcile_position_mismatch_is_break():
    ok, breaks = reconcile_state({"AAPL": D("10")}, {"AAPL": D("5")}, 0, 0)
    assert not ok and any("position AAPL" in b for b in breaks)


def test_reconcile_unknown_broker_position_is_break():
    ok, breaks = reconcile_state({}, {"NVDA": D("3")}, 0, 0)
    assert not ok


def test_reconcile_db_position_absent_at_broker_is_break():
    ok, breaks = reconcile_state({"SPY": D("2")}, {}, 0, 0)
    assert not ok


def test_reconcile_open_order_count_mismatch_is_break():
    ok, breaks = reconcile_state({}, {}, 1, 0)          # a DB order the broker doesn't have
    assert not ok and any("open orders" in b for b in breaks)
