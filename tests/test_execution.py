"""Smart execution tests (§16): market-impact model, slicing algo, size-dependent broker
fills, and that slicing a large order reduces total impact cost."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.execution.algo import ExecutionAlgo, ImmediateAlgo, SlicingAlgo
from atp.execution.engine import ExecutionEngine
from atp.execution.impact import MarketImpactModel
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

INST = Instrument("X", AssetClass.EQUITY)
TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- impact model
def test_impact_zero_without_liquidity_or_size():
    m = MarketImpactModel()
    assert m.impact_bps(0, 1000) == 0.0
    assert m.impact_bps(100, 0) == 0.0


def test_impact_grows_with_size_sqrt_law():
    m = MarketImpactModel(eta_bps=50, exponent=0.5)
    # 100% participation -> eta_bps; 25% -> half (sqrt(0.25)=0.5).
    assert m.impact_bps(1000, 1000) == pytest.approx(50.0)
    assert m.impact_bps(250, 1000) == pytest.approx(25.0)
    assert m.impact_bps(200, 1000) < m.impact_bps(400, 1000)


def test_slicing_reduces_average_impact_by_inverse_sqrt():
    m = MarketImpactModel(eta_bps=50, exponent=0.5)
    one = m.cost_bps_for_slices(1000, 1000, n_slices=1)
    four = m.cost_bps_for_slices(1000, 1000, n_slices=4)
    assert four == pytest.approx(one / math.sqrt(4))   # 1/sqrt(n) scaling


# --------------------------------------------------------------------------- slicing algo
def test_immediate_algo_is_single_order():
    plan = ImmediateAlgo().plan(Order(INST, Side.BUY, 500))
    assert len(plan) == 1 and plan[0].quantity == 500


def test_slicing_splits_large_order_within_participation_cap():
    algo = SlicingAlgo(participation_cap=0.1, max_slices=10)
    # 500 units vs adv 1000 => 50% participation, cap 10% => 5 slices.
    plan = algo.plan(Order(INST, Side.BUY, 500), adv=1000)
    assert len(plan) == 5
    assert sum(o.quantity for o in plan) == pytest.approx(500)     # children sum to parent
    assert all(o.quantity == pytest.approx(100) for o in plan)


def test_slicing_leaves_small_order_whole():
    algo = SlicingAlgo(participation_cap=0.1)
    plan = algo.plan(Order(INST, Side.BUY, 50), adv=1000)          # 5% < cap
    assert len(plan) == 1


def test_slicing_urgent_or_reduce_only_executes_immediately():
    algo = SlicingAlgo(participation_cap=0.1)
    assert len(algo.plan(Order(INST, Side.BUY, 500), adv=1000, urgency="high")) == 1
    assert len(algo.plan(Order(INST, Side.SELL, 500, reduce_only=True), adv=1000)) == 1


def test_slicing_no_adv_leaves_order_whole():
    assert len(SlicingAlgo().plan(Order(INST, Side.BUY, 5000), adv=None)) == 1


# --------------------------------------------------------------------------- broker impact
async def _fill_price(qty, *, impact=True, adv=1000.0):
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0,
                         impact_model=MarketImpactModel(eta_bps=100) if impact else None)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))  # zero spread => isolate impact
    if impact:
        broker.set_liquidity(INST.key, adv)
    res = await broker.place_order(Order(INST, Side.BUY, qty))
    return res.fill.price


async def test_broker_impact_worsens_larger_orders():
    small = await _fill_price(100)     # 10% participation
    large = await _fill_price(400)     # 40% participation
    assert small > 100.0 and large > small            # buys fill above mid, bigger = worse


async def test_broker_no_impact_when_model_absent():
    assert await _fill_price(400, impact=False) == 100.0   # only spread/slippage (both zero)


async def test_execution_rejects_nonconserving_child_plan():
    class _ExpandingAlgo(ExecutionAlgo):
        def plan(self, order, *, adv=None, urgency="normal"):
            return [Order(order.instrument, order.side, order.quantity * 2)]

    broker = PaperBroker(100_000.0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))
    risk = RiskEngine(
        limits=RiskLimits(max_position_pct=1.0, max_gross_leverage=2.0),
        state=RiskState(day_start_equity=100_000.0, peak_equity=100_000.0),
    )
    execution = ExecutionEngine(broker, risk, algo=_ExpandingAlgo())
    result = await execution.submit(
        Order(INST, Side.BUY, 100),
        await broker.get_account(),
        price=100.0,
        current_qty=0.0,
    )
    assert not result.approved and "child plan" in result.reason
    assert await broker.get_positions() == {}


# --------------------------------------------------------------------------- integration
async def test_slicing_beats_immediate_in_a_full_backtest():
    """Same signals, same impact model — slicing large entries should end with higher equity
    than firing them in one shot, because impact is convex in size (§16)."""
    from atp.backtest import Backtester
    from atp.core.events import Bar
    from atp.execution.impact import MarketImpactModel
    from atp.policy import TradingPolicy
    from atp.regime.classifier import RegimeClassifier
    from atp.strategy.momentum import MomentumStrategy

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday, minute bars
    data = [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                800, start + timedelta(minutes=i)) for i in range(200)]

    impact = MarketImpactModel(eta_bps=200)

    def make(algo):
        return Backtester(
            policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
            regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
            impact_model=impact, execution_algo=algo,
        )

    immediate = await make(ImmediateAlgo()).run(data)
    sliced = await make(SlicingAlgo(participation_cap=0.05, max_slices=8)).run(data)

    assert immediate.n_executed > 0 and sliced.n_executed > 0
    assert sliced.ending_equity > immediate.ending_equity   # less impact paid on entries
