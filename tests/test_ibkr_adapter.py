"""IBKR adapter tests (§17).

We can't reach a live IB Gateway in the offline suite, so a `FakeIB` mirrors exactly the
small `ib_insync` surface the adapter uses. These tests cover the parts that carry real risk
of being wrong: the atp<->IB order/contract mapping and the parsing of IB account, position
and fill objects back into atp types. What is intentionally *not* covered (a real socket,
live market data) is documented in docs/DECISIONS.md ADR-6.
"""

from types import SimpleNamespace

import pytest

from atp.brokers.base import Order
from atp.brokers.ibkr import IBKRBroker, IBKRConfig, describe_order
from atp.core.enums import AssetClass, OrderStatus, OrderType, Side
from atp.core.events import Instrument

AAPL = Instrument("AAPL", AssetClass.EQUITY, currency="USD")


# --------------------------------------------------------------------------- fakes
class FakeTrade:
    def __init__(self, status="Filled", filled=0.0, avg=0.0, fills=None):
        self.orderStatus = SimpleNamespace(status=status, filled=filled, avgFillPrice=avg)
        self.fills = fills or []

    def isDone(self):
        return self.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")


class FakeIB:
    """Mirrors the ib_insync surface IBKRBroker touches."""

    def __init__(self, *, portfolio=None, account_values=None, trade=None):
        self._portfolio = portfolio or []
        self._account_values = account_values or []
        self._trade = trade
        self._connected = False
        self.placed = []  # (contract, order) tuples actually sent

    async def connectAsync(self, host, port, clientId=1, readonly=False):
        self._connected = True

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def portfolio(self):
        return self._portfolio

    def accountValues(self, account=None):
        return self._account_values

    async def qualifyContractsAsync(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return self._trade

    # --- extra surfaces for historical/executions/open-orders/health ------
    def __post_extra__(self):
        pass

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        return getattr(self, "_historical", [])

    async def reqExecutionsAsync(self):
        return getattr(self, "_executions", [])

    async def reqOpenOrdersAsync(self):
        return getattr(self, "_open_trades", [])


class RecordingFactory:
    """Fake contract/order factory — records requests, returns plain namespaces."""

    def __init__(self):
        self.contracts = []
        self.orders = []

    def contract(self, instrument):
        c = SimpleNamespace(symbol=instrument.symbol, secType="STK", currency=instrument.currency)
        self.contracts.append(instrument)
        return c

    def order(self, o):
        spec = describe_order(o)
        self.orders.append(spec)
        return SimpleNamespace(spec=spec)


def _av(tag, value, currency="USD"):
    return SimpleNamespace(tag=tag, value=value, currency=currency)


def _pf(symbol, position, avg_cost, market_price, sec_type="STK", currency="USD", multiplier=""):
    contract = SimpleNamespace(
        symbol=symbol, secType=sec_type, currency=currency, multiplier=multiplier, localSymbol=""
    )
    return SimpleNamespace(
        contract=contract, position=position, averageCost=avg_cost, marketPrice=market_price
    )


# --------------------------------------------------------------------------- mapping
def test_describe_order_maps_side_and_type():
    spec = describe_order(Order(AAPL, Side.BUY, 10, OrderType.MARKET, client_id="abc"))
    assert spec.action == "BUY" and spec.kind is OrderType.MARKET and spec.quantity == 10
    assert spec.order_ref == "abc"

    spec = describe_order(Order(AAPL, Side.SELL, 5, OrderType.LIMIT, limit_price=190.0))
    assert spec.action == "SELL" and spec.limit_price == 190.0

    spec = describe_order(Order(AAPL, Side.SELL, 5, OrderType.STOP_LIMIT, limit_price=1, stop_price=2))
    assert spec.stop_price == 2 and spec.limit_price == 1


def test_instrument_from_contract_reads_multiplier_and_secType():
    c = SimpleNamespace(symbol="ES", secType="FUT", currency="USD", multiplier="50", localSymbol="ESZ6")
    inst = IBKRBroker.instrument_from_contract(c)
    assert inst.symbol == "ESZ6"  # prefers localSymbol
    assert inst.asset_class is AssetClass.FUTURE
    assert inst.multiplier == 50.0


# --------------------------------------------------------------------------- parsing
async def test_get_positions_parses_signed_qty_and_avg_price():
    ib = FakeIB(portfolio=[
        _pf("AAPL", 100, avg_cost=150.0, market_price=155.0),
        _pf("MSFT", -20, avg_cost=400.0, market_price=390.0),
        _pf("ZERO", 0, avg_cost=10.0, market_price=10.0),  # flat -> dropped
    ])
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    positions = await broker.get_positions()

    assert set(positions) == {"AAPL:equity", "MSFT:equity"}
    assert positions["AAPL:equity"].quantity == 100
    assert positions["AAPL:equity"].avg_price == 150.0
    assert positions["MSFT:equity"].quantity == -20  # short stays signed


async def test_get_positions_normalizes_futures_avg_cost_by_multiplier():
    # IB reports averageCost per *contract* (incl multiplier); adapter stores a per-unit price.
    ib = FakeIB(portfolio=[_pf("ES", 2, avg_cost=250_000.0, market_price=5010.0, sec_type="FUT", multiplier="50")])
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    positions = await broker.get_positions()
    assert positions["ES:future"].avg_price == pytest.approx(5000.0)  # 250000 / 50


async def test_get_account_parses_tags_and_computes_exposure():
    ib = FakeIB(
        account_values=[
            _av("NetLiquidation", "105000"),
            _av("TotalCashValue", "50000"),
            _av("GrossPositionValue", "55000"),
            _av("UnrealizedPnL", "1200"),
            _av("RealizedPnL", "-300"),
            _av("IgnoreMe", "999"),
        ],
        portfolio=[_pf("AAPL", 100, 150.0, 155.0)],
    )
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    acc = await broker.get_account()
    assert acc.equity == 105000.0
    assert acc.cash == 50000.0
    assert acc.unrealized_pnl == 1200.0
    assert acc.realized_pnl == -300.0
    assert acc.net_exposure == pytest.approx(100 * 155.0)


# --------------------------------------------------------------------------- execution
async def test_place_order_returns_fill_with_summed_commission():
    fills = [
        SimpleNamespace(commissionReport=SimpleNamespace(commission=1.0), time=None),
        SimpleNamespace(commissionReport=SimpleNamespace(commission=0.5), time=None),
    ]
    trade = FakeTrade(status="Filled", filled=100, avg=155.25, fills=fills)
    ib = FakeIB(trade=trade)
    factory = RecordingFactory()
    broker = IBKRBroker(IBKRConfig(), ib=ib, factory=factory)

    result = await broker.place_order(Order(AAPL, Side.BUY, 100, OrderType.MARKET))

    assert result.status is OrderStatus.FILLED
    assert result.filled
    assert result.fill.price == 155.25
    assert result.fill.quantity == 100
    assert result.fill.commission == pytest.approx(1.5)
    # The adapter actually routed a mapped order to the broker.
    assert factory.orders[0].action == "BUY"
    assert len(ib.placed) == 1


async def test_place_order_partial_fill_status():
    # "Submitted" never reaches isDone(); the adapter should poll to timeout then report the
    # partial. Use a short timeout so the test doesn't actually wait 30s.
    trade = FakeTrade(status="Submitted", filled=40, avg=155.0,
                      fills=[SimpleNamespace(commissionReport=SimpleNamespace(commission=1.0), time=None)])
    cfg = IBKRConfig(order_timeout=0.05, poll_interval=0.01)
    broker = IBKRBroker(cfg, ib=FakeIB(trade=trade), factory=RecordingFactory())
    result = await broker.place_order(Order(AAPL, Side.BUY, 100, OrderType.MARKET))
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.fill.quantity == 40


async def test_place_order_rejected_when_not_filled():
    trade = FakeTrade(status="Inactive", filled=0, avg=0.0, fills=[])
    broker = IBKRBroker(IBKRConfig(), ib=FakeIB(trade=trade), factory=RecordingFactory())
    result = await broker.place_order(Order(AAPL, Side.BUY, 100, OrderType.MARKET))
    assert result.status is OrderStatus.REJECTED
    assert result.fill is None
    assert not result.filled


async def test_readonly_session_never_places():
    broker = IBKRBroker(IBKRConfig(readonly=True), ib=FakeIB(trade=FakeTrade()))
    result = await broker.place_order(Order(AAPL, Side.BUY, 1, OrderType.MARKET))
    assert result.status is OrderStatus.REJECTED
    assert "readonly" in result.reason


async def test_connect_and_disconnect_lifecycle():
    ib = FakeIB()
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    await broker.connect()
    assert ib.isConnected()
    await broker.disconnect()
    assert not ib.isConnected()


def test_use_before_connect_raises():
    import asyncio
    broker = IBKRBroker(IBKRConfig(), ib=None)
    with pytest.raises(RuntimeError):
        asyncio.run(broker.get_positions())


# --------------------------------------------------------------------------- historical / executions / health
def test_bar_from_ib_historical_maps_fields():
    from atp.brokers.ibkr import bar_from_ib_historical
    from datetime import datetime, timezone
    ibbar = SimpleNamespace(date=datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
                            open=100.0, high=101.0, low=99.0, close=100.5, volume=1234)
    bar = bar_from_ib_historical(ibbar, AAPL)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100.0, 101.0, 99.0, 100.5, 1234.0)
    assert bar.instrument is AAPL and bar.ts.tzinfo is not None


async def test_historical_bars_maps_all():
    ib = FakeIB()
    from datetime import datetime, timezone
    ib._historical = [
        SimpleNamespace(date=datetime(2026, 1, 5, 15, i, tzinfo=timezone.utc),
                        open=100 + i, high=100 + i, low=100 + i, close=100 + i, volume=10)
        for i in range(3)
    ]
    broker = IBKRBroker(IBKRConfig(), ib=ib, factory=RecordingFactory())
    bars = await broker.historical_bars(AAPL, duration="1 D", bar_size="1 min")
    assert len(bars) == 3 and bars[0].close == 100.0 and bars[2].close == 102.0


async def test_list_executions_parses_fills():
    ex = SimpleNamespace(side="BOT", shares=100, price=155.0, time=None)
    contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD", multiplier="", localSymbol="")
    ib = FakeIB()
    ib._executions = [SimpleNamespace(execution=ex, contract=contract,
                                      commissionReport=SimpleNamespace(commission=1.5))]
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    fills = await broker.list_executions()
    assert len(fills) == 1 and fills[0].quantity == 100 and fills[0].commission == 1.5
    assert fills[0].side is Side.BUY


async def test_open_orders_parses():
    contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD", multiplier="", localSymbol="")
    trade = SimpleNamespace(contract=contract,
                            order=SimpleNamespace(action="BUY", totalQuantity=50, orderType="LMT"),
                            orderStatus=SimpleNamespace(status="Submitted", filled=10))
    ib = FakeIB()
    ib._open_trades = [trade]
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    orders = await broker.open_orders()
    assert orders[0]["quantity"] == 50 and orders[0]["filled"] == 10 and orders[0]["status"] == "Submitted"


async def test_ensure_connected_reconnects():
    ib = FakeIB()  # starts disconnected
    broker = IBKRBroker(IBKRConfig(), ib=ib)
    assert not broker.is_connected()
    assert await broker.ensure_connected() is True
    assert broker.is_connected()
