"""Corporate actions / rollover / expiry tests (§3): value-neutral splits, dividend cash,
futures roll (flatten + reopen), and expiry discovery. All data is test-supplied — no invented
market/reference data."""

from datetime import date, datetime, timezone

import pytest

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.corpactions import (
    CorporateActionsBook,
    CorporateActionsProcessor,
    Dividend,
    FuturesRoll,
    FuturesRollProcessor,
    RollCalendar,
    Split,
    apply_split_to_position,
    dividend_cash,
    options_expiring_on,
)

AAPL = Instrument("AAPL", AssetClass.EQUITY)
TS = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- pure maths
def test_split_is_value_neutral():
    nq, na = apply_split_to_position(100, 400.0, Split("AAPL:equity", "2026-06-01", 4, 1))
    assert nq == 400 and na == 100.0                 # 4:1 => 4x shares, 1/4 basis
    assert nq * na == pytest.approx(100 * 400.0)     # position value unchanged


def test_dividend_cash_signed_by_side():
    d = Dividend("AAPL:equity", "2026-05-01", 0.25)
    assert dividend_cash(100, d) == 25.0             # long receives
    assert dividend_cash(-100, d) == -25.0           # short pays


# --------------------------------------------------------------------------- processor: splits & dividends
async def _broker_with_long(qty=100, avg=400.0):
    b = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await b.connect()
    b.set_quote(QuoteEvent(AAPL, avg, avg, TS))
    await b.place_order(Order(AAPL, Side.BUY, qty))
    return b


async def test_split_applied_to_position():
    broker = await _broker_with_long(100, 400.0)
    book = CorporateActionsBook()
    book.add_split(Split(AAPL.key, "2026-06-01", 4, 1))
    applied = await CorporateActionsProcessor(book).process(broker, date(2026, 6, 1))
    assert applied[0]["type"] == "split"
    pos = (await broker.get_positions())[AAPL.key]
    assert pos.quantity == 400 and pos.avg_price == pytest.approx(100.0)


async def test_dividend_credits_cash():
    broker = await _broker_with_long(100, 400.0)
    cash_before = (await broker.get_account()).cash
    book = CorporateActionsBook()
    book.add_dividend(Dividend(AAPL.key, "2026-05-01", 0.50))
    await CorporateActionsProcessor(book).process(broker, date(2026, 5, 1))
    assert (await broker.get_account()).cash == pytest.approx(cash_before + 100 * 0.50)


async def test_action_not_applied_on_other_date():
    broker = await _broker_with_long(100, 400.0)
    book = CorporateActionsBook()
    book.add_split(Split(AAPL.key, "2026-06-01", 2, 1))
    applied = await CorporateActionsProcessor(book).process(broker, date(2026, 6, 2))  # wrong day
    assert applied == []
    assert (await broker.get_positions())[AAPL.key].quantity == 100


# --------------------------------------------------------------------------- futures rollover
async def test_futures_roll_flattens_and_reopens():
    front = Instrument("ESH6", AssetClass.FUTURE, multiplier=50, expiry="20260320")
    back = Instrument("ESM6", AssetClass.FUTURE, multiplier=50, expiry="20260619")
    broker = PaperBroker(5_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    broker.set_quote(QuoteEvent(front, 5000.0, 5000.0, TS))
    broker.set_quote(QuoteEvent(back, 5010.0, 5010.0, TS))
    await broker.place_order(Order(front, Side.BUY, 2))         # long 2 front

    cal = RollCalendar()
    cal.add(FuturesRoll("SPX", front.key, back, "2026-03-13"))
    applied = await FuturesRollProcessor(cal).process(broker, date(2026, 3, 13))

    assert applied[0]["quantity"] == 2
    positions = await broker.get_positions()
    assert front.key not in positions                          # front flattened
    assert positions[back.key].quantity == 2                    # rolled into back month


# --------------------------------------------------------------------------- expiry discovery
def test_options_expiring_on():
    from atp.brokers.base import Position
    opt_exp = Instrument("O1", AssetClass.OPTION, expiry="20260116", strike=100, right="C", underlying="X")
    opt_future = Instrument("O2", AssetClass.OPTION, expiry="20260220", strike=100, right="C", underlying="X")
    positions = {
        opt_exp.key: Position(opt_exp, 1, 3.0, 3.0),
        opt_future.key: Position(opt_future, 1, 3.0, 3.0),
        AAPL.key: Position(AAPL, 100, 400, 400),               # not an option
    }
    expiring = options_expiring_on(positions, date(2026, 1, 16))
    assert [p.instrument.symbol for p in expiring] == ["O1"]    # only the 16 Jan option
