"""Options tests (§5/§8): Black–Scholes vs textbook values, greeks, IV round-trip, chain
analytics, and the volatility specialist. Plus IBKR derivative contract mapping (§17)."""

import math
from datetime import datetime, timezone

import pytest

from atp.brokers.ibkr import contract_spec
from atp.core.enums import AssetClass, Action, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.options import (
    OptionsEngine,
    black_scholes,
    build_chain,
    compute_features,
    greeks,
    implied_vol,
)
from atp.options.chain import OptionChain, OptionQuote
from atp.strategy.volatility import VolatilityStrategy

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- black-scholes
def test_atm_call_matches_textbook():
    # S=K=100, T=1, r=0, sigma=0.2 => call ≈ 7.9656, put equal (parity at r=0).
    call = black_scholes(100, 100, 1.0, 0.0, 0.2, "C")
    put = black_scholes(100, 100, 1.0, 0.0, 0.2, "P")
    assert call == pytest.approx(7.9656, abs=1e-3)
    assert put == pytest.approx(call, abs=1e-9)          # ATM, r=0


def test_put_call_parity():
    S, K, T, r, sig = 105, 100, 0.5, 0.03, 0.25
    c = black_scholes(S, K, T, r, sig, "C")
    p = black_scholes(S, K, T, r, sig, "P")
    # c - p = S - K*e^{-rT}
    assert (c - p) == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)


def test_intrinsic_at_expiry():
    assert black_scholes(120, 100, 0.0, 0.0, 0.2, "C") == pytest.approx(20.0)
    assert black_scholes(80, 100, 0.0, 0.0, 0.2, "P") == pytest.approx(20.0)
    assert black_scholes(80, 100, 0.0, 0.0, 0.2, "C") == pytest.approx(0.0)


def test_greeks_signs_and_atm_delta():
    g_call = greeks(100, 100, 1.0, 0.0, 0.2, "C")
    g_put = greeks(100, 100, 1.0, 0.0, 0.2, "P")
    assert g_call.delta == pytest.approx(0.5398, abs=1e-3)
    assert g_put.delta == pytest.approx(g_call.delta - 1.0, abs=1e-3)   # parity of deltas
    assert g_call.gamma > 0 and g_call.vega > 0
    assert g_call.theta < 0                                             # long option decays


def test_implied_vol_roundtrip():
    price = black_scholes(100, 110, 0.75, 0.02, 0.35, "C")
    iv = implied_vol(price, 100, 110, 0.75, 0.02, "C")
    assert iv == pytest.approx(0.35, abs=1e-4)


def test_implied_vol_rejects_below_intrinsic():
    assert implied_vol(0.5, 130, 100, 0.5, 0.0, "C") is None   # price << intrinsic (30)


# --------------------------------------------------------------------------- chain analytics
def test_chain_features_atm_and_put_skew():
    chain = build_chain("SPX:index", spot=100.0, T=0.25, base_iv=0.20, skew=0.5, oi_put=300, oi_call=100)
    f = compute_features(chain)
    assert f.atm_iv == pytest.approx(0.20, abs=1e-6)
    assert f.iv_skew > 0                                  # skew>0 => OTM puts richer than calls
    assert f.put_call_oi_ratio == pytest.approx(3.0)     # 300 put OI / 100 call OI
    assert f.atm_strike == pytest.approx(100.0)


def test_chain_no_skew_is_flat():
    f = compute_features(build_chain("X:index", 100.0, 0.25, base_iv=0.2, skew=0.0))
    assert f.iv_skew == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- options engine
def test_iv_rank_tracks_history():
    eng = OptionsEngine()
    for iv in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35):      # rising IV
        eng.update(build_chain("X:index", 100.0, 0.25, base_iv=iv))
    assert eng.iv_rank("X:index") == pytest.approx(1.0)  # current is the highest => rank 1
    assert eng.features("X:index").atm_iv == pytest.approx(0.35)


def test_iv_rank_none_before_enough_history():
    eng = OptionsEngine()
    eng.update(build_chain("X:index", 100.0, 0.25, base_iv=0.2))
    assert eng.iv_rank("X:index") is None


# --------------------------------------------------------------------------- volatility specialist
def _fs(instrument, **over):
    base = dict(instrument=instrument, ts=T0, price=100.0, n_bars=50, ready=True,
                sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                realized_vol=0.01, vol_percentile=0.5, rel_volume=1.0)
    base.update(over)
    return FeatureSet(**base)


def test_volatility_strategy_buys_extreme_fear():
    SPX = Instrument("SPX", AssetClass.INDEX)
    eng = OptionsEngine()
    # Build history that ends at a high IV with heavy put OI (fear).
    for iv in (0.10, 0.12, 0.14, 0.16, 0.18):
        eng.update(build_chain(SPX.key, 100.0, 0.25, base_iv=iv, oi_put=100, oi_call=100))
    eng.update(build_chain(SPX.key, 100.0, 0.25, base_iv=0.45, oi_put=300, oi_call=100))  # spike + put-heavy

    strat = VolatilityStrategy(eng, iv_rank_high=0.8, pc_high=1.3)
    sig = strat.generate(_fs(SPX), Regime.HIGH_VOLATILITY)
    assert sig is not None and sig.action is Action.BUY   # fade the panic


def test_volatility_strategy_silent_without_extreme():
    SPX = Instrument("SPX", AssetClass.INDEX)
    eng = OptionsEngine()
    for iv in (0.18, 0.19, 0.20, 0.21, 0.22, 0.20):
        eng.update(build_chain(SPX.key, 100.0, 0.25, base_iv=iv, oi_put=100, oi_call=100))
    strat = VolatilityStrategy(eng)
    assert strat.generate(_fs(SPX), Regime.RANGE) is None


# --------------------------------------------------------------------------- IBKR derivative mapping
def test_contract_spec_option():
    opt = Instrument("AAPL260116C00150000", AssetClass.OPTION, multiplier=100,
                     expiry="20260116", strike=150.0, right="C", underlying="AAPL")
    spec = contract_spec(opt)
    assert spec["secType"] == "OPT"
    assert spec["symbol"] == "AAPL" and spec["strike"] == 150.0 and spec["right"] == "C"
    assert spec["lastTradeDateOrContractMonth"] == "20260116"
    assert spec["multiplier"] == "100"


def test_contract_spec_future_requires_expiry():
    with pytest.raises(ValueError):
        contract_spec(Instrument("ES", AssetClass.FUTURE, multiplier=50))
    spec = contract_spec(Instrument("ES", AssetClass.FUTURE, multiplier=50, expiry="20260320"))
    assert spec["secType"] == "FUT" and spec["multiplier"] == "50"


def test_contract_spec_option_rejects_bad_right():
    with pytest.raises(ValueError):
        contract_spec(Instrument("X", AssetClass.OPTION, expiry="20260116", strike=100, right="Z"))


def test_option_instrument_key_is_unique():
    a = Instrument("AAPL", AssetClass.OPTION, expiry="20260116", strike=150, right="C", underlying="AAPL")
    b = Instrument("AAPL", AssetClass.OPTION, expiry="20260116", strike=155, right="C", underlying="AAPL")
    assert a.key != b.key
    assert a.underlying_key == "AAPL:equity"
