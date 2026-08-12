"""Reconciliation tests (§17): internal book vs broker truth, and halt-on-mismatch."""

import pytest

from atp.brokers.base import Position
from atp.brokers.reconcile import Reconciler, diff_positions
from atp.core.enums import AssetClass
from atp.core.events import Instrument
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

AAPL = Instrument("AAPL", AssetClass.EQUITY)
MSFT = Instrument("MSFT", AssetClass.EQUITY)


def _pos(inst, qty, avg=100.0, mark=100.0):
    return Position(instrument=inst, quantity=qty, avg_price=avg, market_price=mark)


def test_consistent_when_book_matches_broker():
    report = diff_positions(
        {"AAPL:equity": 100, "MSFT:equity": -5},
        {"AAPL:equity": _pos(AAPL, 100), "MSFT:equity": _pos(MSFT, -5)},
    )
    assert report.is_consistent
    assert report.broker_count == 2
    assert "consistent" in report.summary()


def test_quantity_mismatch_is_a_break():
    report = diff_positions({"AAPL:equity": 100}, {"AAPL:equity": _pos(AAPL, 90)})
    assert not report.is_consistent
    assert len(report.breaks) == 1
    b = report.breaks[0]
    assert b.internal_qty == 100 and b.broker_qty == 90 and b.diff == -10


def test_position_missing_at_broker_is_a_break():
    report = diff_positions({"AAPL:equity": 100}, {})
    assert not report.is_consistent
    assert report.breaks[0].broker_qty == 0.0


def test_unexpected_broker_position_is_a_break():
    report = diff_positions({}, {"AAPL:equity": _pos(AAPL, 25)})
    assert not report.is_consistent
    assert report.breaks[0].internal_qty == 0.0 and report.breaks[0].broker_qty == 25


def test_tolerance_absorbs_tiny_float_noise():
    report = diff_positions({"AAPL:equity": 100.0}, {"AAPL:equity": _pos(AAPL, 100.0 + 1e-9)})
    assert report.is_consistent


class _StubBroker:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


async def test_reconciler_halts_risk_on_mismatch():
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(100_000, 100_000))
    broker = _StubBroker({"AAPL:equity": _pos(AAPL, 90)})
    rec = Reconciler(broker, risk=risk)

    report = await rec.run(internal_book={"AAPL:equity": 100})

    assert not report.is_consistent
    assert risk.state.halted
    assert "reconciliation break" in risk.state.halt_reason


async def test_reconciler_does_not_halt_when_consistent():
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(100_000, 100_000))
    broker = _StubBroker({"AAPL:equity": _pos(AAPL, 100)})
    rec = Reconciler(broker, risk=risk)

    report = await rec.run(internal_book={"AAPL:equity": 100})

    assert report.is_consistent
    assert not risk.state.halted


# --------------------------------------------------------------------------- full reconciliation (§11)
from atp.brokers.reconcile import InternalState, Reconciler, reconcile_full


class _FullBroker:
    def __init__(self, positions, cash, realized_pnl, open_orders=None):
        self._positions = positions
        self._cash = cash
        self._realized = realized_pnl
        self._open = open_orders

    async def get_positions(self):
        return self._positions

    async def get_account(self):
        from atp.brokers.base import Account
        return Account(cash=self._cash, equity=self._cash, realized_pnl=self._realized,
                       unrealized_pnl=0.0, gross_exposure=0.0, net_exposure=0.0,
                       positions=self._positions)

    async def open_orders(self):
        return self._open or []


async def test_full_reconciliation_consistent():
    broker = _FullBroker({"AAPL:equity": _pos(AAPL, 100)}, cash=50_000.0, realized_pnl=250.0)
    state = InternalState(positions={"AAPL:equity": 100}, cash=50_000.0, realized_pnl=250.0)
    report = await reconcile_full(state, broker)
    assert report.is_consistent


async def test_full_reconciliation_flags_cash_and_pnl():
    broker = _FullBroker({"AAPL:equity": _pos(AAPL, 100)}, cash=49_000.0, realized_pnl=250.0)
    state = InternalState(positions={"AAPL:equity": 100}, cash=50_000.0, realized_pnl=100.0)
    report = await reconcile_full(state, broker)
    assert not report.is_consistent
    assert report.cash_break is not None and report.cash_break.diff == -1000.0
    assert report.pnl_break is not None


async def test_full_reconciliation_flags_open_order_mismatch():
    broker = _FullBroker({}, cash=100_000.0, realized_pnl=0.0,
                         open_orders=[{"instrument_key": "AAPL:equity", "action": "BUY", "quantity": 50}])
    state = InternalState(positions={}, cash=100_000.0, realized_pnl=0.0,
                          open_orders={"AAPL:equity": 100})   # desk thinks 100 working, broker 50
    report = await reconcile_full(state, broker)
    assert not report.is_consistent
    assert report.order_breaks and report.order_breaks[0].internal == 100 and report.order_breaks[0].broker == 50


async def test_reconciler_run_full_halts_risk_on_break():
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(100_000, 100_000))
    broker = _FullBroker({"AAPL:equity": _pos(AAPL, 90)}, cash=50_000.0, realized_pnl=0.0)
    rec = Reconciler(broker, risk=risk)
    report = await rec.run_full(InternalState(positions={"AAPL:equity": 100}, cash=50_000.0))
    assert not report.is_consistent
    assert risk.state.halted
