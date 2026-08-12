"""Cost model tests (§20): commission, slippage, financing, borrow, FX — and their (optional)
wiring into the PaperBroker."""

from datetime import datetime, timezone

import pytest

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.costs import (
    FixedBpsSlippage,
    FlatBorrow,
    FlatFinancing,
    FXConverter,
    PerContractCommission,
    PercentCommission,
    PerInstrumentBorrow,
    PerShareCommission,
    RateTableFinancing,
    SpreadSlippage,
    TableFXRates,
    VolumeSlippage,
)
from atp.macro import RatesTable

INST = Instrument("X", AssetClass.EQUITY)
TS = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- commission
def test_per_share_commission_matches_legacy():
    m = PerShareCommission(per_unit=0.005, minimum=1.0)
    assert m.commission(quantity=100, price=50) == pytest.approx(1.0)   # min binds
    assert m.commission(quantity=1000, price=50) == pytest.approx(5.0)


def test_per_contract_and_percent_commission():
    assert PerContractCommission(per_contract=0.85).commission(quantity=3, price=5000) == pytest.approx(2.55)
    assert PercentCommission(rate=0.0005).commission(quantity=100, price=50, multiplier=1) == pytest.approx(2.5)


# --------------------------------------------------------------------------- slippage
def test_slippage_models():
    assert FixedBpsSlippage(2.0).slippage_bps(quantity=1, price=100) == 2.0
    assert SpreadSlippage(0.5).slippage_bps(quantity=1, price=100, spread_bps=4.0) == 2.0
    vs = VolumeSlippage(eta_bps=10, exponent=0.5)
    assert vs.slippage_bps(quantity=250, price=100, adv=1000) == pytest.approx(5.0)  # sqrt(0.25)*10
    assert vs.slippage_bps(quantity=100, price=100, adv=None) == 0.0                  # no adv => 0


# --------------------------------------------------------------------------- financing / borrow
def test_flat_financing_and_ratetable():
    assert FlatFinancing(0.05).financing_cost(notional=100_000, days=1) == pytest.approx(100_000 * 0.05 / 365)
    rates = RatesTable()
    rates.set_rate("USD", 0.04)
    fc = RateTableFinancing(rates, spread=0.01).financing_cost(notional=100_000, days=1, currency="USD")
    assert fc == pytest.approx(100_000 * 0.05 / 365)     # 4% + 1% spread


def test_borrow_models():
    assert FlatBorrow(0.005).borrow_cost(short_notional=-100_000, days=1) == pytest.approx(100_000 * 0.005 / 365)
    b = PerInstrumentBorrow(rates={"HTB:equity": 0.25}, default_rate=0.005)
    assert b.borrow_cost(short_notional=-10_000, days=1, instrument_key="HTB:equity") == pytest.approx(10_000 * 0.25 / 365)
    assert b.borrow_cost(short_notional=-10_000, days=1, instrument_key="OTHER") == pytest.approx(10_000 * 0.005 / 365)


# --------------------------------------------------------------------------- FX
def test_fx_converts_and_never_invents_missing_rate():
    fx = FXConverter(TableFXRates({("EUR", "USD"): 1.10}), conversion_cost_bps=2.0)
    assert fx.convert(100, "EUR", "USD") == pytest.approx(110.0)
    assert fx.convert(110, "USD", "EUR") == pytest.approx(100.0)     # inverse pair
    assert fx.convert(100, "EUR", "EUR") == 100.0                    # identity
    assert fx.convert(100, "GBP", "USD") is None                     # unknown => None (no invented rate)


def test_fx_conversion_cost():
    fx = FXConverter(TableFXRates(), conversion_cost_bps=2.0)
    assert fx.conversion_cost(100_000, "USD", "USD") == 0.0
    assert fx.conversion_cost(100_000, "EUR", "USD") == pytest.approx(20.0)   # 2 bps


# --------------------------------------------------------------------------- broker wiring
async def test_paper_broker_uses_commission_model():
    broker = PaperBroker(1_000_000, slippage_bps=0, commission_model=PerContractCommission(per_contract=2.0))
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))
    res = await broker.place_order(Order(INST, Side.BUY, 5))
    assert res.fill.commission == pytest.approx(10.0)   # 5 contracts * $2


async def test_paper_broker_uses_slippage_model():
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0,
                         slippage_model=SpreadSlippage(0.5))
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 99.0, 101.0, TS))   # 200 bps spread => 100 bps slippage
    res = await broker.place_order(Order(INST, Side.BUY, 1))
    # BUY fills at ask 101 + 100 bps = 101 * 1.01 = 102.01
    assert res.fill.price == pytest.approx(101.0 * 1.01)


async def test_paper_broker_defaults_unchanged():
    broker = PaperBroker(1_000_000, commission_per_unit=0.005, min_commission=1.0, slippage_bps=1.0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, TS))
    res = await broker.place_order(Order(INST, Side.BUY, 100))
    assert res.fill.commission == pytest.approx(1.0)               # min binds (legacy)
    assert res.fill.price == pytest.approx(100.0 * 1.0001)         # 1 bp slippage
