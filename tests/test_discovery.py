"""Strategy Discovery tests (§12/§13): feature predicates, rule strategy behavior, and the
validation gauntlet's accept/reject logic (OOS + walk-forward + Monte-Carlo)."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from atp.core.enums import AssetClass, Action, Regime
from atp.core.events import Bar, Instrument
from atp.discovery import (
    DiscoveryCriteria,
    FeaturePredicate,
    RuleStrategy,
    SearchSpace,
    StrategyDiscovery,
)
from atp.features.engine import FeatureSet
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier

INST = Instrument("X", AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fs(**over) -> FeatureSet:
    base = dict(
        instrument=INST, ts=T0, price=100.0, n_bars=50, ready=True,
        sma_fast=101.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
        realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0,
    )
    base.update(over)
    return FeatureSet(**base)


# --------------------------------------------------------------------------- predicates
def test_predicate_holds_and_value():
    p = FeaturePredicate("trend", ">", 0.3)
    assert p.holds(_fs(trend=0.5))
    assert not p.holds(_fs(trend=0.1))
    assert p.value(_fs(trend=0.5)) == 0.5
    assert str(p) == "trend>0.3"


def test_predicate_zscore_derived():
    p = FeaturePredicate("zscore", ">", 1.0)
    # zscore = (price - sma_slow)/close_std = (103-100)/1 = 3
    assert p.holds(_fs(price=103.0, sma_slow=100.0, close_std=1.0))


def test_predicate_rejects_unknown_feature_or_op():
    with pytest.raises(ValueError):
        FeaturePredicate("nope", ">", 1.0)
    with pytest.raises(ValueError):
        FeaturePredicate("trend", "!!", 1.0)


# --------------------------------------------------------------------------- rule strategy
def test_rule_fires_on_band_crossing_only():
    s = RuleStrategy(signal_feature="trend", entry_threshold=0.3)
    sig = s.generate(_fs(trend=0.5), Regime.TRENDING_UP)
    assert sig is not None and sig.action is Action.BUY
    # Same side again => no fresh crossing.
    assert s.generate(_fs(trend=0.6), Regime.TRENDING_UP) is None
    # Cross to the other side => SELL.
    sig2 = s.generate(_fs(trend=-0.5), Regime.TRENDING_DOWN)
    assert sig2.action is Action.SELL


def test_rule_filters_gate_signal():
    s = RuleStrategy(
        signal_feature="trend", entry_threshold=0.3,
        filters=(FeaturePredicate("vol_percentile", "<", 0.5),),
    )
    # Strong trend but the filter fails (vol_percentile high) => no signal.
    assert s.generate(_fs(trend=0.9, vol_percentile=0.8), Regime.TRENDING_UP) is None
    # Filter passes => signal.
    assert s.generate(_fs(trend=0.9, vol_percentile=0.2), Regime.TRENDING_UP) is not None


def test_rule_long_only_exits_instead_of_shorting():
    s = RuleStrategy(signal_feature="trend", entry_threshold=0.3, allow_short=False)
    s.generate(_fs(trend=0.5), Regime.TRENDING_UP)                 # BUY
    sig = s.generate(_fs(trend=-0.5), Regime.TRENDING_DOWN)
    assert sig.action is Action.CLOSE                              # exit, not short


def test_rule_params_and_reset():
    s = RuleStrategy(signal_feature="momentum", entry_threshold=0.01)
    assert s.params["signal_feature"] == "momentum"
    assert s.params["entry_threshold"] == 0.01
    s.generate(_fs(sma_fast=102, sma_slow=100, trend=0.0), Regime.RANGE)
    s.reset()
    assert s._prev_sign == {}


# --------------------------------------------------------------------------- gauntlet
def _oscillating(n=220):
    bars = []
    for i in range(n):
        p = 100 + 4 * math.sin(i / 6.0) + 0.05 * i
        bars.append(Bar(INST, p, p * 1.002, p * 0.998, p, 1000 + i, T0 + timedelta(minutes=i)))
    return bars


def _discovery(criteria):
    return StrategyDiscovery(
        policy=TradingPolicy(capital=100_000.0),
        criteria=criteria,
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        mc_runs=300,
    )


async def test_gauntlet_reports_all_stages():
    disc = _discovery(DiscoveryCriteria())
    rep = await disc.validate(RuleStrategy(signal_feature="trend", entry_threshold=0.3), _oscillating())
    # Every stage produced numbers.
    assert rep.in_sample.n_periods > 0
    assert rep.wf_windows > 0
    assert 0.0 <= rep.mc_prob_loss <= 1.0
    assert rep.passed == (len(rep.failures) == 0)


async def test_lenient_criteria_accept_a_trading_candidate():
    lenient = DiscoveryCriteria(
        min_trades=1, min_oos_sharpe=-1e9, min_oos_profit_factor=0.0,
        min_oos_return=-1.0, max_mc_prob_loss=1.0, min_wf_win_fraction=0.0,
    )
    disc = _discovery(lenient)
    rep = await disc.validate(RuleStrategy(signal_feature="trend", entry_threshold=0.3), _oscillating())
    assert rep.oos.n_trades > 0
    assert rep.passed


async def test_strict_sharpe_criteria_reject():
    strict = DiscoveryCriteria(min_trades=1, min_oos_sharpe=1e9, min_wf_win_fraction=0.0,
                               min_oos_profit_factor=0.0, max_mc_prob_loss=1.0)
    disc = _discovery(strict)
    rep = await disc.validate(RuleStrategy(signal_feature="trend", entry_threshold=0.3), _oscillating())
    assert not rep.passed
    assert any("oos_sharpe" in f for f in rep.failures)


async def test_candidate_that_never_trades_fails_on_trades():
    disc = _discovery(DiscoveryCriteria(min_trades=10))
    # Threshold far beyond any trend value => zero signals => zero trades.
    rep = await disc.validate(RuleStrategy(signal_feature="trend", entry_threshold=1000.0), _oscillating())
    assert rep.oos.n_trades == 0
    assert any("trades" in f for f in rep.failures)


async def test_validate_is_deterministic_across_reuse():
    """Reusing a stateful candidate across runs must be independent (Strategy.reset)."""
    disc = _discovery(DiscoveryCriteria())
    cand = RuleStrategy(signal_feature="trend", entry_threshold=0.3)
    bars = _oscillating()
    r1 = await disc.validate(cand, bars)
    r2 = await disc.validate(cand, bars)  # same instance, run again
    assert r1.oos.total_return == r2.oos.total_return
    assert r1.in_sample.n_trades == r2.in_sample.n_trades


def test_search_space_candidate_count():
    space = SearchSpace(feature_grid={"trend": [0.2, 0.3], "zscore": [1.0, 1.5, 2.0]})
    cands = space.candidates()
    assert len(cands) == 2 + 3  # thresholds summed across features (one empty filter-set)
    assert all(isinstance(c, RuleStrategy) for c in cands)


async def test_discover_aggregates_and_notes_selection_bias():
    disc = _discovery(DiscoveryCriteria(min_trades=1, min_oos_sharpe=-1e9,
                                        min_oos_profit_factor=0.0, min_oos_return=-1.0,
                                        max_mc_prob_loss=1.0, min_wf_win_fraction=0.0))
    space = SearchSpace(feature_grid={"trend": [0.25, 0.4]})
    result = await disc.discover(_oscillating(), space)
    assert result.n_candidates == 2
    assert len(result.reports) == 2
    assert "multiple testing" in result.selection_note
    # best is the top passed report (or None).
    if result.passed:
        assert result.best is result.passed[0]
