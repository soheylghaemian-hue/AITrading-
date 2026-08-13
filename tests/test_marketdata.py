"""Read-only IBKR market-data probe — sentinel handling, classification, unavailable state."""

from types import SimpleNamespace

from atp.brokers.base import Account
from atp.core.enums import AssetClass
from atp.dashboard.snapshot import build_snapshot
from atp.live.marketdata import _price, _size, probe_market_data, subscription_report
from atp.risk.engine import RiskEngine, RiskLimits, RiskState


def _risk():
    return RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0))


# --------------------------------------------------------------------------- sentinel handling
def test_price_sentinels_become_none():
    assert _price(-1) is None          # IBKR "no data" sentinel
    assert _price(0) is None
    assert _price(float("nan")) is None
    assert _price(None) is None
    assert _price(1.15234) == 1.15234
    assert _size(-1) is None
    assert _size(0) == 0.0
    assert _size(1_000_000) == 1_000_000.0


# --------------------------------------------------------------------------- probe (fake ib)
class _FakeEvent:
    def __iadd__(self, _fn):
        return self
    def __isub__(self, _fn):
        return self


class _FakeIB:
    def __init__(self, tickers):
        self._tickers = tickers
        self.errorEvent = _FakeEvent()
    def reqMarketDataType(self, _t):
        pass
    async def qualifyContractsAsync(self, c):
        return [c]
    def reqMktData(self, contract, *_a):
        return self._tickers[contract.symbol]


class _FakeFactory:
    def contract(self, inst):
        return SimpleNamespace(symbol=inst.symbol)


class _FakeBroker:
    def __init__(self, tickers):
        self._ib = _FakeIB(tickers)
        self._factory = _FakeFactory()
    def _require(self):
        return self._ib


async def test_probe_classifies_realtime_and_sentinels():
    tickers = {
        # EUR realtime with valid bid/ask; last is the -1 sentinel → must become None
        "EUR": SimpleNamespace(bid=1.15234, ask=1.15235, last=-1.0, bidSize=5_000_000, askSize=3_000_000, marketDataType=1),
        # AAPL: no quote at all → DATA_NOT_AVAILABLE
        "AAPL": SimpleNamespace(bid=-1.0, ask=-1.0, last=-1.0, bidSize=0, askSize=0, marketDataType=1),
    }
    broker = _FakeBroker(tickers)
    universe = [("EUR.USD", AssetClass.FX, "IDEALPRO"), ("AAPL", AssetClass.EQUITY, "NASDAQ")]
    rows = await probe_market_data(broker, universe, settle=0.0)
    eur = [r for r in rows if r["symbol"] == "EUR.USD"][0]
    aapl = [r for r in rows if r["symbol"] == "AAPL"][0]

    assert eur["status"] == "DATA_AVAILABLE" and eur["market_data_type"] == "REALTIME"
    assert eur["bid"] == 1.15234 and eur["ask"] == 1.15235
    assert eur["last"] is None                       # -1 sentinel never shown as a price
    assert eur["bid_size"] == 5_000_000

    assert aapl["status"] == "DATA_NOT_AVAILABLE"
    assert aapl["bid"] is None and aapl["ask"] is None and aapl["last"] is None


def test_subscription_report():
    md = [
        {"symbol": "EUR.USD", "asset_class": "fx", "exchange": "IDEALPRO", "status": "DATA_AVAILABLE", "error_code": None},
        {"symbol": "AAPL", "asset_class": "equity", "exchange": "NASDAQ", "status": "DATA_NOT_AVAILABLE", "error_code": 10089},
    ]
    rep = {r["instrument"]: r for r in subscription_report(md)}
    assert rep["EUR.USD"]["subscription_required"] is False
    assert rep["AAPL"]["subscription_required"] is True and rep["AAPL"]["ibkr_error"] == 10089


# --------------------------------------------------------------------------- IBKR DATA UNAVAILABLE
def test_snapshot_account_none_is_all_null_no_fake():
    snap = build_snapshot(account=None, risk=_risk(), mode="paper", connected=False).as_dict()
    a = snap["account"]
    assert a["equity"] is None and a["cash"] is None and a["realized_pnl"] is None
    assert a["buying_power"] is None
    assert snap["positions"] == []
    assert snap["connected"] is False


def test_snapshot_account_present_still_works():
    acct = Account(cash=1_000.0, equity=1_000.0, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions={})
    snap = build_snapshot(account=acct, risk=_risk(), mode="paper", connected=True).as_dict()
    assert snap["account"]["equity"] == 1_000.0 and snap["connected"] is True
