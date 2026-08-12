"""Macro tests (§5/§8): rates table (carry, trend) and the FX-carry & macro specialists."""

from datetime import datetime, timezone

import pytest

from atp.core.enums import Action, AssetClass, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.macro import RatesTable
from atp.strategy.fx_carry import FXCarryStrategy
from atp.strategy.macro import MacroStrategy

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
AUDUSD = Instrument("AUD", AssetClass.FX, currency="USD")   # base AUD, quote USD
SPX = Instrument("SPX", AssetClass.INDEX, currency="USD")


def _fs(instrument, **over):
    base = dict(instrument=instrument, ts=T0, price=100.0, n_bars=50, ready=True,
                sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0)
    base.update(over)
    return FeatureSet(**base)


# --------------------------------------------------------------------------- rates table
def test_carry_is_rate_differential():
    r = RatesTable()
    r.set_rate("AUD", 0.045)
    r.set_rate("USD", 0.020)
    assert r.carry("AUD", "USD") == pytest.approx(0.025)   # +2.5% carry holding AUD vs USD
    assert r.carry("USD", "AUD") == pytest.approx(-0.025)
    assert r.carry("AUD", "JPY") is None           # missing leg


def test_rate_trend_sign():
    r = RatesTable()
    for x in (0.02, 0.03, 0.04):
        r.set_rate("USD", x)                       # hiking
    assert r.trend("USD") > 0
    r2 = RatesTable()
    for x in (0.05, 0.04, 0.03):
        r2.set_rate("EUR", x)                      # cutting
    assert r2.trend("EUR") < 0
    assert RatesTable().trend("GBP") == 0.0        # unknown => flat


# --------------------------------------------------------------------------- fx carry
def test_fx_carry_longs_positive_carry():
    r = RatesTable()
    r.set_rate("AUD", 0.045)
    r.set_rate("USD", 0.010)                       # +3.5% carry
    s = FXCarryStrategy(r, min_carry=0.005)
    sig = s.generate(_fs(AUDUSD), Regime.RANGE)
    assert sig is not None and sig.action is Action.BUY


def test_fx_carry_shorts_negative_carry():
    r = RatesTable()
    r.set_rate("AUD", 0.005)
    r.set_rate("USD", 0.05)                        # negative carry holding AUD
    s = FXCarryStrategy(r, min_carry=0.005)
    assert s.generate(_fs(AUDUSD), Regime.RANGE).action is Action.SELL


def test_fx_carry_trend_gate_blocks_fighting_a_downtrend():
    r = RatesTable()
    r.set_rate("AUD", 0.045)
    r.set_rate("USD", 0.010)                       # positive carry ...
    s = FXCarryStrategy(r, min_carry=0.005, trend_block=0.5)
    # ... but price is trending hard against a long => no signal.
    assert s.generate(_fs(AUDUSD, trend=-1.0), Regime.RANGE) is None


def test_fx_carry_ignores_non_fx():
    r = RatesTable()
    r.set_rate("SPX", 0.04)
    assert FXCarryStrategy(r).generate(_fs(SPX), Regime.RANGE) is None


def test_fx_carry_stands_aside_in_panic():
    r = RatesTable()
    r.set_rate("AUD", 0.045)
    r.set_rate("USD", 0.010)
    assert FXCarryStrategy(r).generate(_fs(AUDUSD), Regime.PANIC) is None


# --------------------------------------------------------------------------- macro
def test_macro_longs_easing_cycle():
    r = RatesTable()
    for x in (0.05, 0.04, 0.03):                   # USD easing
        r.set_rate("USD", x)
    s = MacroStrategy(r, trend_threshold=0.005)
    sig = s.generate(_fs(SPX), Regime.RANGE)
    assert sig is not None and sig.action is Action.BUY   # easing => risk-on


def test_macro_exits_tightening_cycle():
    r = RatesTable()
    for x in (0.01, 0.02, 0.03):                   # USD hiking
        r.set_rate("USD", x)
    s = MacroStrategy(r, trend_threshold=0.005, allow_short=False)
    assert s.generate(_fs(SPX), Regime.RANGE).action is Action.CLOSE


def test_macro_silent_when_rates_flat():
    r = RatesTable()
    for _ in range(3):
        r.set_rate("USD", 0.03)                    # unchanged
    assert MacroStrategy(r).generate(_fs(SPX), Regime.RANGE) is None
