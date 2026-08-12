"""Instrument Master + calendar tests (§5): full reference model, underlying relationships,
tick/quantity rules, and trading-hours/holiday logic."""

from datetime import date, datetime, time, timezone

import pytest

from atp.core.enums import AssetClass
from atp.instruments import (
    InstrumentMaster,
    InstrumentSpec,
    LiquidityTier,
    MarketCalendar,
    ProductType,
    SettlementType,
)


def _gold_family() -> InstrumentMaster:
    m = InstrumentMaster()
    m.register(InstrumentSpec("XAU.SPOT", "XAUUSD", AssetClass.COMMODITY, underlying="GOLD",
                              product_type=ProductType.SPOT, liquidity_tier=LiquidityTier.TIER_1))
    m.register(InstrumentSpec("GC.FUT.202606", "GC", AssetClass.FUTURE, underlying="GOLD",
                              exchange="COMEX", multiplier=100, contract_size=100,
                              expiration="20260626", product_type=ProductType.FUTURE))
    m.register(InstrumentSpec("GLD.ETF", "GLD", AssetClass.ETF, underlying="GOLD",
                              exchange="ARCA", product_type=ProductType.ETF))
    m.register(InstrumentSpec("GC.OPT.C2000", "GC260626C2000", AssetClass.OPTION, underlying="GOLD",
                              multiplier=100, expiration="20260626", option_strike=2000, option_type="C",
                              settlement=SettlementType.PHYSICAL, product_type=ProductType.OPTION))
    return m


# --------------------------------------------------------------------------- master
def test_underlying_relationships_link_the_family():
    m = _gold_family()
    fam = m.related("GOLD")
    assert len(fam) == 4
    kinds = {s.product_type for s in fam}
    assert kinds == {ProductType.SPOT, ProductType.FUTURE, ProductType.ETF, ProductType.OPTION}


def test_lookup_by_id_and_key():
    m = _gold_family()
    spec = m.get("GC.FUT.202606")
    assert spec is not None and spec.exchange == "COMEX"
    assert m.get(spec.key) is spec               # also indexed by tradeable key


def test_spec_projects_to_tradeable_instrument():
    spec = _gold_family().get("GC.OPT.C2000")
    inst = spec.to_instrument()
    assert inst.is_option and inst.strike == 2000 and inst.right == "C"
    assert inst.underlying_key == "GOLD:equity"


def test_by_asset_class():
    m = _gold_family()
    assert len(m.by_asset_class(AssetClass.FUTURE)) == 1
    assert len(m.by_asset_class(AssetClass.ETF)) == 1


def test_tick_rounding_and_quantity_rules():
    spec = InstrumentSpec("X", "X", AssetClass.EQUITY, tick_size=0.05, min_quantity=10, lot_size=10)
    assert spec.round_price(100.02) == pytest.approx(100.0)
    assert spec.round_price(100.03) == pytest.approx(100.05)
    assert spec.valid_quantity(20) and not spec.valid_quantity(5) and not spec.valid_quantity(15)


# --------------------------------------------------------------------------- calendar
def test_calendar_trading_days_and_holidays():
    cal = MarketCalendar(name="t", trading_days=(0, 1, 2, 3, 4), holidays=frozenset({"2026-01-01"}))
    assert cal.is_trading_day(date(2026, 1, 2))       # Friday
    assert not cal.is_trading_day(date(2026, 1, 3))   # Saturday
    assert not cal.is_trading_day(date(2026, 1, 1))   # holiday (a Thursday)


def test_calendar_session_hours():
    cal = MarketCalendar(name="rth", session_open=time(14, 30), session_close=time(21, 0))
    monday = date(2026, 1, 5)
    assert cal.is_open(datetime.combine(monday, time(15, 0), tzinfo=timezone.utc))
    assert not cal.is_open(datetime.combine(monday, time(13, 0), tzinfo=timezone.utc))  # pre-open
