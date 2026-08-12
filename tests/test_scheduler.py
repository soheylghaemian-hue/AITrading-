"""Execution scheduler tests (§16): TWAP/VWAP slice splitting and time-sliced release across
ticks, with in-flight tracking and risk-abort."""

from datetime import datetime, timezone

import pytest

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.execution.engine import ExecutionEngine
from atp.execution.scheduler import ExecutionScheduler, split_quantity
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

INST = Instrument("X", AssetClass.EQUITY)
TS = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- splitting
def test_twap_split_is_equal():
    assert split_quantity(100, [1, 1, 1, 1]) == [25, 25, 25, 25]


def test_vwap_split_follows_profile():
    assert split_quantity(100, [1, 2, 1]) == [25, 50, 25]


def test_split_remainder_goes_to_last_slice():
    children = split_quantity(100, [1, 1, 1])   # 33,33,34
    assert sum(children) == 100
    assert children == [33, 33, 34]


def test_split_drops_zero_slices():
    assert split_quantity(2, [1, 1, 1, 1]) == [2.0]   # 0,0,0,2 => keep the non-zero


# --------------------------------------------------------------------------- release over ticks
async def _stack(*, halted=False):
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))
    state = RiskState(1_000_000, 1_000_000)
    if halted:
        state.halted = True
        state.halt_reason = "test"
    risk = RiskEngine(limits=RiskLimits(max_position_pct=1.0, max_gross_leverage=5.0), state=state)
    execution = ExecutionEngine(broker, risk, autonomous=True)
    return broker, ExecutionScheduler(execution, slices=4)


async def test_twap_releases_one_slice_per_tick():
    broker, sched = await _stack()
    sched.submit_parent(Order(INST, Side.BUY, 100), price=100.0)
    assert sched.working_qty(INST.key) == 100.0

    price_fn = lambda k: 100.0  # noqa: E731
    for expected_remaining in (75.0, 50.0, 25.0, 0.0):
        account = await broker.get_account()
        results = await sched.tick(account, price_fn=price_fn)
        assert len(results) == 1 and results[0][0].filled
        assert sched.working_qty(INST.key) == expected_remaining

    assert not sched.has_work()
    positions = await broker.get_positions()
    assert positions[INST.key].quantity == 100.0   # fully worked


async def test_context_is_carried_to_fills():
    broker, sched = await _stack()
    sched.submit_parent(Order(INST, Side.BUY, 40), price=100.0, context={"strategy": "twap-test"})
    account = await broker.get_account()
    (result, ctx), = await sched.tick(account, price_fn=lambda k: 100.0)
    assert ctx == {"strategy": "twap-test"}


async def test_risk_veto_aborts_the_working_order():
    broker, sched = await _stack(halted=True)   # risk halted => new risk blocked
    sched.submit_parent(Order(INST, Side.BUY, 100), price=100.0)
    account = await broker.get_account()
    results = await sched.tick(account, price_fn=lambda k: 100.0)
    assert not results[0][0].filled
    assert not sched.has_work()                 # aborted, not retried forever


async def test_vwap_profile_over_ticks():
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))
    risk = RiskEngine(limits=RiskLimits(max_position_pct=1.0, max_gross_leverage=5.0),
                      state=RiskState(1_000_000, 1_000_000))
    sched = ExecutionScheduler(ExecutionEngine(broker, risk), volume_profile=[1, 3, 1])
    sched.submit_parent(Order(INST, Side.BUY, 100), price=100.0)   # => 20, 60, 20

    filled = []
    for _ in range(3):
        account = await broker.get_account()
        (res, _), = await sched.tick(account, price_fn=lambda k: 100.0)
        filled.append(res.result.fill.quantity)
    assert filled == [20, 60, 20]


# --------------------------------------------------------------------------- desk integration
async def test_desk_works_entry_over_bars_via_scheduler():
    """With execution_slices set, an entry is worked across bars rather than in one shot; the
    position builds up gradually and the journal still reconciles with the broker."""
    import math
    from datetime import timedelta

    from atp.backtest import Backtester
    from atp.core.events import Bar
    from atp.journal import InMemoryJournal
    from atp.policy import TradingPolicy
    from atp.regime.classifier import RegimeClassifier
    from atp.strategy.momentum import MomentumStrategy

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                1000, start + timedelta(minutes=i)) for i in range(200)]

    journal = InMemoryJournal()
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        commission_per_unit=0.0, min_commission=0.0,
        execution_slices=4, journal=journal,
    )
    res = await bt.run(bars)

    assert res.n_executed > 0                       # slices did execute over the run
    # More executions than round trips => entries were sliced across bars (worked over time).
    assert res.n_executed > len(journal)
    # Journal P&L still reconciles with the broker's realized-trade P&L (no leakage).
    jpnl = sum(t.realized_pnl for t in journal.all())
    assert math.isclose(jpnl, sum(res.trade_pnls), rel_tol=1e-6, abs_tol=1e-6)
