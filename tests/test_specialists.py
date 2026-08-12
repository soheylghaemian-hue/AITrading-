"""New specialist tests (§8): the Breakout strategy and the StatArb pairs engine + two-leg
pairs strategy."""

from datetime import datetime, timedelta, timezone

import pytest

from atp.core.enums import Action, AssetClass, Regime
from atp.core.events import Bar, Instrument
from atp.features.engine import FeatureSet
from atp.stat_arb import Pair, StatArbEngine
from atp.strategy.breakout import BreakoutStrategy
from atp.strategy.stat_arb import StatArbStrategy

A = Instrument("A", AssetClass.EQUITY)
B = Instrument("B", AssetClass.EQUITY)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _fs(instrument=A, **over):
    base = dict(instrument=instrument, ts=T0, price=100.0, n_bars=50, ready=True,
                sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0)
    base.update(over)
    return FeatureSet(**base)


# --------------------------------------------------------------------------- breakout
def test_breakout_fires_on_high_z_with_volume():
    s = BreakoutStrategy(entry_z=1.5, volume_mult=1.3)
    sig = s.generate(_fs(price=104, sma_slow=100, close_std=1.0, rel_volume=2.0), Regime.BREAKOUT)
    assert sig is not None and sig.action is Action.BUY


def test_breakout_needs_volume_confirmation():
    s = BreakoutStrategy(entry_z=1.5, volume_mult=1.5)
    # Big move but thin volume => no breakout.
    assert s.generate(_fs(price=104, sma_slow=100, close_std=1.0, rel_volume=1.0), Regime.BREAKOUT) is None


def test_breakout_inactive_in_range_regime():
    s = BreakoutStrategy()
    assert s.generate(_fs(price=104, sma_slow=100, rel_volume=3.0), Regime.RANGE) is None


def test_breakout_shorts_downside_break():
    s = BreakoutStrategy(entry_z=1.5, volume_mult=1.3)
    sig = s.generate(_fs(price=96, sma_slow=100, close_std=1.0, rel_volume=2.0), Regime.HIGH_VOLATILITY)
    assert sig.action is Action.SELL


# --------------------------------------------------------------------------- statarb engine
def _feed_pair(engine, a_prices, b_prices):
    for i, (pa, pb) in enumerate(zip(a_prices, b_prices)):
        ts = T0 + timedelta(minutes=i)
        engine.update_bar(Bar(A, pa, pa, pa, pa, 1000, ts))
        engine.update_bar(Bar(B, pb, pb, pb, pb, 1000, ts))


def _comoving(n=30):
    """A cointegrated pair: both legs driven by a common, *varying* factor (beta ~2)."""
    import math
    a, b = [100.0], [50.0]
    for i in range(1, n):
        d = 0.4 * math.sin(i / 3.0) + 0.15          # varying increment => non-degenerate returns
        a.append(a[-1] + 2 * d)
        b.append(b[-1] + 1 * d)
    return a, b


def test_statarb_zero_z_when_spread_stable():
    eng = StatArbEngine([Pair(A.key, B.key)], window=40, min_window=20)
    a, b = _comoving(30)                              # move together => spread ~ constant
    _feed_pair(eng, a, b)
    v = eng.assessment(A.key)
    assert v is not None and abs(v.z) < 1.0
    assert v.correlation > 0.9


def test_statarb_detects_stretched_spread_and_leg_signs_oppose():
    eng = StatArbEngine([Pair(A.key, B.key)], window=40, min_window=20)
    a, b = _comoving(30)
    for j in range(1, 6):                             # A jumps up late => spread stretches
        a[-j] += 6.0
    _feed_pair(eng, a, b)

    va = eng.assessment(A.key)
    vb = eng.assessment(B.key)
    assert va is not None and vb is not None
    assert va.z > 0                                   # A rich
    # The two legs trade in opposite directions (market neutral).
    assert va.leg_sign(1.0) == -vb.leg_sign(1.0)
    assert va.leg_sign(1.0) == -1                     # sell the rich leg A


def test_statarb_none_before_min_window():
    eng = StatArbEngine([Pair(A.key, B.key)], window=40, min_window=20)
    _feed_pair(eng, [100, 101, 102], [50, 51, 52])
    assert eng.assessment(A.key) is None


# --------------------------------------------------------------------------- statarb strategy
def test_statarb_strategy_emits_opposite_legs():
    eng = StatArbEngine([Pair(A.key, B.key)], window=40, min_window=20)
    a, b = _comoving(30)
    for j in range(1, 6):
        a[-j] += 6.0
    _feed_pair(eng, a, b)

    strat = StatArbStrategy(eng, entry_z=1.0, corr_min=0.3)
    sig_a = strat.generate(_fs(instrument=A), Regime.RANGE)
    sig_b = strat.generate(_fs(instrument=B), Regime.RANGE)
    assert sig_a is not None and sig_b is not None
    assert sig_a.action is Action.SELL and sig_b.action is Action.BUY   # sell rich A, buy cheap B


def test_statarb_hedged_sizing_is_market_neutral_to_shared_move():
    """β-weighted legs (qty_b = β·qty_a from a common reference price) neutralize a shared move:
    if both prices move by the hedge-consistent amount, the pair P&L is ~zero."""
    from atp.opportunity.sizing import PositionSizer
    from atp.policy import TradingPolicy

    sizer = PositionSizer(neutral_notional_pct=0.10)
    policy = TradingPolicy(capital=100_000.0)
    equity, beta, pa, pb = 100_000.0, 1.25, 100.0, 80.0

    qty_a = sizer.target_units(price=pa, stop_distance=0, equity=equity, policy=policy,
                               sizing="hedged", hedge_factor=1.0, ref_price=pa)
    qty_b = sizer.target_units(price=pb, stop_distance=0, equity=equity, policy=policy,
                               sizing="hedged", hedge_factor=beta, ref_price=pa)
    # Leg B holds β times leg A's units (before whole-unit flooring, exactly β·qty_a).
    assert qty_b == pytest.approx(beta * qty_a, rel=0.02)

    # Shared move: dP_a = beta * dP_b. Short A / long B nets to ~0.
    d_pb = 2.0
    d_pa = beta * d_pb
    pnl = -qty_a * d_pa + qty_b * d_pb          # short A, long B
    assert abs(pnl) < 0.02 * abs(qty_a * d_pa)  # within 2% of a single leg's move => neutral


def test_statarb_strategy_stands_aside_in_panic():
    eng = StatArbEngine([Pair(A.key, B.key)])
    strat = StatArbStrategy(eng)
    assert strat.generate(_fs(instrument=A), Regime.PANIC) is None


async def test_statarb_wires_through_desk_observers():
    """The desk feeds the pairs engine via observers, so the specialist can trade a pair."""
    import math
    from atp.backtest import Backtester
    from atp.policy import TradingPolicy
    from atp.regime.classifier import RegimeClassifier

    eng = StatArbEngine([Pair(A.key, B.key)], window=40, min_window=20)
    # Interleaved, mostly-cointegrated pair with an oscillating spread => round trips.
    bars = []
    for i in range(160):
        pa = 100 + 0.05 * i + 3 * math.sin(i / 9.0)
        pb = 60 + 0.03 * i + 3 * math.sin(i / 9.0 + 0.3)   # tracks A with a wobble
        ts = T0 + timedelta(minutes=i)
        bars.append(Bar(A, pa, pa * 1.001, pa * 0.999, pa, 1000, ts))
        bars.append(Bar(B, pb, pb * 1.001, pb * 0.999, pb, 1000, ts + timedelta(seconds=30)))

    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[StatArbStrategy(eng, entry_z=1.0, corr_min=0.3)],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        observers=[eng],
    )
    res = await bt.run(bars)
    # The engine saw data through the desk and the specialist acted on both legs.
    assert eng.assessment(A.key) is not None
    assert res.n_executed > 0
