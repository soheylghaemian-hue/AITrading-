"""§ WP3 hotfix — IbkrQualificationClient on ib_async (deterministic, OFFLINE, no broker SDK).

Proves the read-only qualification client binds to `ib_async` (installed), calls ONLY
`reqContractDetailsAsync`, and maps connection / timeout / disconnect faults to the right typed errors.
Every test uses an injected fake IB (no ib_async needed) or a monkeypatched fake `ib_async` module — so it
runs with ZERO network and no real broker SDK. Safety: no orders, no market data, no subscriptions.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.instruments.qualification import (
    ConnectionUnavailableError,
    IbkrQualificationClient,
    QualificationConfig,
    QualificationRequest,
    RetryableQualificationError,
    qualify_instruments,
)
from atp.store import open_store


def _req(symbol="AAPL", sec_type="STK", exchange="NASDAQ", currency="USD", **kw):
    return QualificationRequest(symbol=symbol, sec_type=sec_type, exchange=exchange,
                                primary_exchange=kw.pop("primary_exchange", exchange), currency=currency, **kw)


def _detail(con_id, symbol, *, primary="NASDAQ", exchange="SMART", currency="USD", sec_type="STK"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol, secType=sec_type,
                                 exchange=exchange, primaryExchange=primary, currency=currency,
                                 lastTradeDateOrContractMonth="", strike=0, right="", multiplier="1",
                                 underConId=0),
        longName=symbol, minTick=0.01, stockType="", country="US")


class FakeIB:
    """A minimal ib_async.IB stand-in. Records every method call; forbidden methods hard-fail if ever hit."""

    def __init__(self, script: dict, connected=True):
        self.script = script          # symbol -> list[detail] | Exception | "hang" | "drop"
        self._connected = connected
        self.calls: list = []

    def isConnected(self):
        return self._connected

    async def reqContractDetailsAsync(self, contract):
        sym = getattr(contract, "symbol", contract)
        self.calls.append(("reqContractDetailsAsync", sym))
        b = self.script.get(sym)
        if isinstance(b, Exception):
            raise b
        if b == "hang":
            await asyncio.sleep(30)      # will be cancelled by the client's wait_for timeout
            return []
        if b == "drop":                  # connection dies mid-request
            self._connected = False
            raise RuntimeError("socket closed")
        return b or []

    # methods that MUST NEVER be called by a read-only qualifier
    def placeOrder(self, *a, **k):
        self.calls.append(("placeOrder",)); raise AssertionError("placeOrder must never be called")

    def reqMktData(self, *a, **k):
        self.calls.append(("reqMktData",)); raise AssertionError("reqMktData must never be called")

    def reqScannerSubscription(self, *a, **k):
        self.calls.append(("scanner",)); raise AssertionError("scanner must never be called")


async def _noop():
    return None


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


# --------------------------------------------------------------------- import stays SDK-free
def test_module_import_pulls_no_broker_sdk():
    # importing the qualification module (done at the top of this file) must NOT pull any IBKR SDK
    assert "ib_insync" not in sys.modules
    assert not any(m == "ib_async" or m.startswith("ib_async.") for m in sys.modules)
    assert not any(m == "ibapi" or m.startswith("ibapi.") for m in sys.modules)


# --------------------------------------------------------------------- _build_contract binds to ib_async
def test_build_contract_uses_ib_async_and_maps_fields(monkeypatch):
    captured: dict = {}

    class Contract:
        def __init__(self, **kw):
            captured.clear(); captured.update(kw)

    fake = types.ModuleType("ib_async")
    fake.Contract = Contract
    monkeypatch.setitem(sys.modules, "ib_async", fake)

    IbkrQualificationClient._build_contract(_req("AAPL"))
    # § WP10: a real ticker on an already-IBKR venue (US listing source) keeps the symbol query.
    assert captured["symbol"] == "AAPL" and captured["secType"] == "STK" and captured["currency"] == "USD"
    assert captured["exchange"] == "SMART" and captured["primaryExchange"] == "NASDAQ"  # STK → SMART routing
    assert "secId" not in captured          # a genuine ticker query needs no ISIN
    assert "ib_insync" not in sys.modules   # the fix removed ib_insync entirely

    # § WP10: a FIRDS-style derivative — ISIN discovery (secIdType/secId), the FIRDS MIC (XEUR) translated to
    # the IBKR exchange code (EUREX), full derivative identity, and NEVER the raw MIC or symbol==ISIN.
    IbkrQualificationClient._build_contract(_req(
        "DE000TESTOPT1", sec_type="OPT", exchange="XEUR", currency="EUR", isin="DE000TESTOPT1",
        expiry="20261218", strike=100.0, right="C"))
    assert captured["secIdType"] == "ISIN" and captured["secId"] == "DE000TESTOPT1"
    assert captured["exchange"] == "EUREX" and "symbol" not in captured   # MIC translated; never symbol==ISIN
    assert captured["lastTradeDateOrContractMonth"] == "20261218"
    assert captured["strike"] == 100.0 and captured["right"] == "C"


# --------------------------------------------------------------------- connection / disconnect / timeout
async def test_disconnected_client_raises_connection_unavailable():
    c = IbkrQualificationClient(FakeIB({}, connected=False), contract_factory=lambda r: r)
    with pytest.raises(ConnectionUnavailableError):
        await c.fetch_contract_details(_req("A"))


async def test_connection_error_during_request_maps_to_connection_unavailable():
    c = IbkrQualificationClient(FakeIB({"A": ConnectionResetError("reset by peer")}), contract_factory=lambda r: r)
    with pytest.raises(ConnectionUnavailableError) as ei:
        await c.fetch_contract_details(_req("A"))
    assert ei.value.code == "connection_lost"


async def test_socket_drop_midrequest_maps_to_connection_unavailable():
    c = IbkrQualificationClient(FakeIB({"A": "drop"}), contract_factory=lambda r: r)
    with pytest.raises(ConnectionUnavailableError):     # isConnected() flips False → treated as a disconnect
        await c.fetch_contract_details(_req("A"))


async def test_request_timeout_maps_to_retryable():
    c = IbkrQualificationClient(FakeIB({"A": "hang"}), contract_factory=lambda r: r, request_timeout=0.05)
    with pytest.raises(RetryableQualificationError) as ei:
        await c.fetch_contract_details(_req("A"))
    assert ei.value.code == "timeout"


async def test_generic_fault_while_connected_is_retryable_not_connection_loss():
    c = IbkrQualificationClient(FakeIB({"A": ValueError("odd payload")}), contract_factory=lambda r: r)
    with pytest.raises(RetryableQualificationError):
        await c.fetch_contract_details(_req("A"))


async def test_only_reqcontractdetails_is_ever_called():
    fib = FakeIB({"A": [_detail(1, "A")]})
    c = IbkrQualificationClient(fib, contract_factory=lambda r: r)
    out = await c.fetch_contract_details(_req("A"))
    assert len(out) == 1
    assert fib.calls == [("reqContractDetailsAsync", "A")]   # nothing else touched — read-only by construction


# --------------------------------------------------------------------- end-to-end through qualify_instruments
def _upsert(store, symbol, exchange="NASDAQ"):
    rec = InstrumentRecord(symbol=symbol, asset_class=AssetClass.EQUITY, exchange=exchange, trading_currency="USD",
                           region="AMERICAS", country="US", timezone="America/New_York",
                           trading_calendar="us_equity", multiplier="1", primary_exchange=exchange, source="t")
    store.im_upsert_instrument(rec.as_record())
    return rec.instrument_id


async def test_unique_match_verifies_and_two_conids_are_ambiguous_via_ib_async_client():
    store = _store()
    aid = _upsert(store, "AAPL")
    bid = _upsert(store, "AMB")
    fib = FakeIB({"AAPL": [_detail(101, "AAPL", primary="NASDAQ")],           # unique real-venue match
                  "AMB": [_detail(201, "AMB"), _detail(202, "AMB")]})          # two plausible → ambiguous
    client = IbkrQualificationClient(fib, contract_factory=lambda r: r)        # bypass ib_async Contract build
    summary = await qualify_instruments(store, client, run_label="e2e",
                                        config=QualificationConfig(pause_seconds=0.0), sleep=lambda _: _noop())
    assert summary.status == "COMPLETED"
    a = store.im_get_instrument(aid)
    assert a.qualification_status == "VERIFIED" and a.con_id == 101           # con_id ONLY from the IBKR reply
    b = store.im_get_instrument(bid)
    assert b.qualification_status == "AMBIGUOUS" and b.con_id is None          # ambiguous never assigns a conId
    assert ("placeOrder",) not in fib.calls and ("reqMktData",) not in fib.calls   # no order/market-data ever
