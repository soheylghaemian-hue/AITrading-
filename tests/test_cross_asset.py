"""Cross-asset intelligence tests (§6/§8): correlation & divergence math, and the specialist
trading divergence reversion when the relationship still holds."""

from datetime import datetime, timedelta, timezone

import pytest

from atp.core.enums import Action, AssetClass, Regime
from atp.core.events import Bar, Instrument
from atp.cross_asset import CrossAssetEngine, CrossAssetView, Relationship
from atp.features.engine import FeatureSet
from atp.strategy.cross_asset import CrossAssetStrategy

LEAD = Instrument("USD", AssetClass.FX)
FOLL = Instrument("GOLD", AssetClass.COMMODITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# A leader return pattern with real variance (so correlation is well-defined).
PATTERN = [0.02, -0.01, 0.03, 0.00, 0.015]


def _feed(engine, lead_rets, foll_rets):
    lc = fc = 100.0
    engine.update_bar(Bar(LEAD, lc, lc, lc, lc, 1000, T0))
    engine.update_bar(Bar(FOLL, fc, fc, fc, fc, 1000, T0))
    for i, (rl, rf) in enumerate(zip(lead_rets, foll_rets), start=1):
        lc *= 1 + rl
        fc *= 1 + rf
        ts = T0 + timedelta(minutes=i)
        engine.update_bar(Bar(LEAD, lc, lc, lc, lc, 1000, ts))
        engine.update_bar(Bar(FOLL, fc, fc, fc, fc, 1000, ts))


def _fs(instrument=FOLL, **over):
    base = dict(instrument=instrument, ts=T0, price=100.0, n_bars=50, ready=True,
                sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0)
    base.update(over)
    return FeatureSet(**base)


# --------------------------------------------------------------------------- engine
def test_relationship_validates_sign():
    with pytest.raises(ValueError):
        Relationship("A", "B", 2)


def test_confirming_when_follower_tracks_leader():
    eng = CrossAssetEngine([Relationship(LEAD.key, FOLL.key, +1)], window=25, min_window=15)
    rets = PATTERN * 5
    _feed(eng, rets, rets)  # follower == leader
    view = eng.assessment(FOLL)
    assert view is not None and view.ready
    assert view.correlation > 0.9
    assert abs(view.divergence_z) < 0.5   # tracking => little divergence
    assert view.confirming


def test_divergence_when_follower_lags():
    eng = CrossAssetEngine([Relationship(LEAD.key, FOLL.key, +1)], window=25, min_window=15)
    lead = PATTERN * 5
    foll = list(lead)
    for i in range(len(foll) - 6, len(foll)):   # follower pulls back late
        foll[i] -= 0.02
    _feed(eng, lead, foll)
    view = eng.assessment(FOLL)
    assert view is not None
    assert view.correlation > 0.3            # relationship still intact
    assert view.divergence_z < 0             # follower underperformed what leader implies


def test_assessment_none_before_min_window():
    eng = CrossAssetEngine([Relationship(LEAD.key, FOLL.key, +1)], window=25, min_window=15)
    _feed(eng, PATTERN, PATTERN)             # only 5 returns < min_window
    assert eng.assessment(FOLL) is None
    assert eng.assessment(LEAD) is None      # leader isn't a follower in any relationship


# --------------------------------------------------------------------------- strategy (unit, fake view)
class _FakeEngine:
    def __init__(self, view):
        self._view = view

    def assessment(self, instrument):
        return self._view


def _view(z, corr=0.8, n=20):
    return CrossAssetView(follower=FOLL.key, leader=LEAD.key, n=n, correlation=corr,
                          leader_cum=0.1, follower_cum=0.05, implied=0.1,
                          divergence=-0.05, divergence_z=z, confirming=False)


def test_strategy_buys_lagging_follower_then_no_repeat():
    s = CrossAssetStrategy(_FakeEngine(_view(-2.0)), entry_z=1.5)
    sig = s.generate(_fs(), Regime.RANGE)
    assert sig is not None and sig.action is Action.BUY
    assert s.generate(_fs(), Regime.RANGE) is None       # same band => no fresh signal


def test_strategy_sells_when_follower_ran_ahead():
    s = CrossAssetStrategy(_FakeEngine(_view(+2.0)), entry_z=1.5)
    assert s.generate(_fs(), Regime.RANGE).action is Action.SELL


def test_strategy_skips_when_correlation_broken():
    s = CrossAssetStrategy(_FakeEngine(_view(-2.0, corr=0.1)), entry_z=1.5, corr_min=0.3)
    assert s.generate(_fs(), Regime.RANGE) is None       # divergence but link is broken


def test_strategy_stands_aside_in_panic():
    s = CrossAssetStrategy(_FakeEngine(_view(-2.0)), entry_z=1.5)
    assert s.generate(_fs(), Regime.PANIC) is None


def test_strategy_below_threshold_is_silent():
    s = CrossAssetStrategy(_FakeEngine(_view(-0.5)), entry_z=1.5)
    assert s.generate(_fs(), Regime.RANGE) is None


# --------------------------------------------------------------------------- integration (real engine + strategy)
def test_engine_plus_strategy_signals_on_real_divergence():
    eng = CrossAssetEngine([Relationship(LEAD.key, FOLL.key, +1)], window=25, min_window=15)
    lead = PATTERN * 5
    foll = list(lead)
    for i in range(len(foll) - 6, len(foll)):
        foll[i] -= 0.02
    _feed(eng, lead, foll)

    strat = CrossAssetStrategy(eng, entry_z=1.0)
    sig = strat.generate(_fs(instrument=FOLL), Regime.RANGE)
    assert sig is not None and sig.action is Action.BUY   # lagging follower => buy to converge
