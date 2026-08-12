"""Multi-leg option execution tests (§16/§5): combo construction, net debit/credit, combined
greeks, leg-by-leg execution against the paper broker, and cash settlement at expiry."""

from datetime import datetime, timezone

import pytest

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.options import (
    black_scholes,
    execute_combo,
    settle_expiration,
    straddle,
    vertical_call_spread,
)
from atp.options.execution import option

EXP = "20260116"
TS = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- combos
def test_vertical_spread_structure():
    combo = vertical_call_spread("AAPL", EXP, 100, 110)
    assert len(combo.legs) == 2
    long_leg, short_leg = combo.legs
    assert long_leg.side is Side.BUY and long_leg.instrument.strike == 100
    assert short_leg.side is Side.SELL and short_leg.instrument.strike == 110
    assert all(l.instrument.is_option for l in combo.legs)


def test_vertical_spread_is_a_debit():
    combo = vertical_call_spread("AAPL", EXP, 100, 110)
    debit = combo.net_debit(spot=105, T=0.03, r=0.0, iv=0.3)
    # Long lower-strike call costs more than the short higher-strike => net debit (> 0).
    assert debit > 0
    # And bounded by the strike width × multiplier.
    assert debit < (110 - 100) * 100


def test_straddle_net_debit_matches_two_legs():
    combo = straddle("SPX", EXP, 100)
    debit = combo.net_debit(spot=100, T=0.05, r=0.0, iv=0.2)
    c = black_scholes(100, 100, 0.05, 0.0, 0.2, "C") * 100
    p = black_scholes(100, 100, 0.05, 0.0, 0.2, "P") * 100
    assert debit == pytest.approx(c + p)


def test_long_straddle_greeks_are_long_gamma_vega():
    g = straddle("SPX", EXP, 100).greeks(spot=100, T=0.05, r=0.0, iv=0.2)
    assert g.gamma > 0 and g.vega > 0            # long options => long gamma/vega
    # ATM straddle is ~delta-neutral: net delta tiny vs a single leg's ~±50 (100 multiplier).
    assert abs(g.delta) < 5.0


def test_short_straddle_is_short_gamma():
    g = straddle("SPX", EXP, 100, long=False).greeks(spot=100, T=0.05, r=0.0, iv=0.2)
    assert g.gamma < 0 and g.vega < 0


# --------------------------------------------------------------------------- execution
async def _broker_with_quotes(legs_prices):
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    for inst, px in legs_prices:
        broker.set_quote(QuoteEvent(inst, px, px, TS))
    return broker


async def test_execute_combo_places_both_legs_and_nets_debit():
    combo = vertical_call_spread("AAPL", EXP, 100, 110)
    long_leg, short_leg = combo.legs
    broker = await _broker_with_quotes([(long_leg.instrument, 6.0), (short_leg.instrument, 2.0)])

    result = await execute_combo(broker, combo, quantity=1)
    assert result.filled
    # Net cash = -(6*100) [buy] + (2*100) [sell] = -400 debit.
    assert result.net_cash == pytest.approx(-400.0)

    positions = await broker.get_positions()
    assert positions[long_leg.instrument.key].quantity == 1     # long the lower strike
    assert positions[short_leg.instrument.key].quantity == -1   # short the higher strike


# --------------------------------------------------------------------------- settlement
async def test_settlement_cash_settles_to_intrinsic():
    # Long 1 call, strike 100, bought for 3.00. Underlying settles at 108 => intrinsic 8.
    call = option("XYZ", EXP, 100, "C")
    broker = await _broker_with_quotes([(call, 3.0)])
    await broker.place_order(Order(call, Side.BUY, 1))
    assert (await broker.get_positions())[call.key].quantity == 1

    settled = await settle_expiration(broker, {"XYZ": 108.0}, datetime(2026, 1, 16, tzinfo=timezone.utc))
    assert len(settled) == 1 and settled[0].intrinsic == 8.0
    assert not await broker.get_positions()                     # position closed
    # Realized P&L = (8 - 3) * 1 * 100 = +500.
    assert broker.realized_pnl == pytest.approx(500.0)


async def test_settlement_worthless_option_expires_at_zero():
    call = option("XYZ", EXP, 100, "C")
    broker = await _broker_with_quotes([(call, 3.0)])
    await broker.place_order(Order(call, Side.BUY, 1))
    # Settles below strike => intrinsic 0 => lose the premium.
    await settle_expiration(broker, {"XYZ": 90.0}, datetime(2026, 1, 16, tzinfo=timezone.utc))
    assert broker.realized_pnl == pytest.approx(-300.0)          # -3 * 100


async def test_settlement_skips_unexpired_options():
    call = option("XYZ", EXP, 100, "C")
    broker = await _broker_with_quotes([(call, 3.0)])
    await broker.place_order(Order(call, Side.BUY, 1))
    # "now" is before expiry => nothing settles.
    settled = await settle_expiration(broker, {"XYZ": 108.0}, datetime(2026, 1, 10, tzinfo=timezone.utc))
    assert settled == []
    assert (await broker.get_positions())[call.key].quantity == 1


# --------------------------------------------------------------------------- physical assignment
async def test_physical_exercise_long_call_becomes_long_stock():
    call = option("XYZ", EXP, 100, "C")
    broker = await _broker_with_quotes([(call, 3.0)])
    await broker.place_order(Order(call, Side.BUY, 1))          # long 1 call @ 3.00

    settled = await settle_expiration(broker, {"XYZ": 110.0},
                                      datetime(2026, 1, 16, tzinfo=timezone.utc), style="physical")
    assert settled[0].shares == 100
    positions = await broker.get_positions()
    assert call.key not in positions                            # option exercised
    stock = positions["XYZ:equity"]
    assert stock.quantity == 100 and stock.avg_price == 110.0   # long 100 shares at spot
    # Option realized its P&L: (10 intrinsic − 3 premium) × 100 = +700; stock unrealized 0.
    assert broker.realized_pnl == pytest.approx(700.0)
    acct = await broker.get_account()
    assert acct.equity == pytest.approx(1_000_700.0)            # book stays consistent


async def test_physical_long_put_becomes_short_stock():
    put = option("XYZ", EXP, 100, "P")
    broker = await _broker_with_quotes([(put, 3.0)])
    await broker.place_order(Order(put, Side.BUY, 1))
    await settle_expiration(broker, {"XYZ": 90.0},
                            datetime(2026, 1, 16, tzinfo=timezone.utc), style="physical")
    stock = (await broker.get_positions())["XYZ:equity"]
    assert stock.quantity == -100                              # exercised put => short stock


async def test_physical_short_put_assigned_becomes_long_stock():
    put = option("XYZ", EXP, 100, "P")
    broker = await _broker_with_quotes([(put, 4.0)])
    await broker.place_order(Order(put, Side.SELL, 1))          # short 1 put, collect 4.00
    settled = await settle_expiration(broker, {"XYZ": 90.0},
                                      datetime(2026, 1, 16, tzinfo=timezone.utc), style="physical")
    assert settled[0].shares == 100
    stock = (await broker.get_positions())["XYZ:equity"]
    assert stock.quantity == 100                               # assigned => long stock
    # Short put realized (4 premium − 10 intrinsic) × 100 = −600.
    assert broker.realized_pnl == pytest.approx(-600.0)


async def test_physical_otm_option_expires_worthless_no_stock():
    call = option("XYZ", EXP, 100, "C")
    broker = await _broker_with_quotes([(call, 3.0)])
    await broker.place_order(Order(call, Side.BUY, 1))
    settled = await settle_expiration(broker, {"XYZ": 95.0},   # OTM
                                      datetime(2026, 1, 16, tzinfo=timezone.utc), style="physical")
    assert settled[0].shares == 0
    assert "XYZ:equity" not in await broker.get_positions()    # no assignment when OTM
    assert broker.realized_pnl == pytest.approx(-300.0)        # lost the premium
