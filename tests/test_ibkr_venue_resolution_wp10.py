"""§ WP10 — IBKR venue & contract resolution hardening: deterministic OFFLINE regression tests.

Covers the exact faults the qualification canary hit (error 200 "destination or exchange selected is
Invalid" and "Invalid value in field # 541"), the ISIN-based discovery query for all seven asset classes
(equity, ETF, fund, bond, future, option, warrant), the fail-closed MIC→IBKR venue registry, the
verification venue-match across the MIC↔IBKR namespaces, and the ERROR_RETRYABLE (budget-neutral) reuse.

No network, no ib_async install required: ib_async is monkeypatched for _build_contract, and a fake IB with
an errorEvent stub drives the client. Runnable under pytest OR standalone (python test_..._wp10.py).
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from atp.instruments import ibkr_venue as venue
from atp.instruments.ibkr_catalog import contract_detail_to_global
from atp.instruments.qualification import (
    IbkrQualificationClient,
    QualificationStatus,
    VenueResolutionError,
    _qualify_one,
    build_request_spec,
    classify_contract_query_error,
    match_contract,
)


# ------------------------------------------------------------------ fakes / helpers
class _Event:
    """Minimal ib_async-style Event supporting += / -= / emit."""

    def __init__(self):
        self._subs = []

    def __iadd__(self, fn):
        self._subs.append(fn)
        return self

    def __isub__(self, fn):
        if fn in self._subs:
            self._subs.remove(fn)
        return self

    def emit(self, *a):
        for fn in list(self._subs):
            fn(*a)


class FakeIB:
    def __init__(self, script, connected=True):
        self.script = script
        self._connected = connected
        self.calls = []
        self.errorEvent = _Event()

    def isConnected(self):
        return self._connected

    async def reqContractDetailsAsync(self, contract):
        key = getattr(contract, "isin", None) or getattr(contract, "symbol", None)
        self.calls.append(key)
        b = self.script.get(key)
        if b == "venue200":
            self.errorEvent.emit(1, 200, "The destination or exchange selected is Invalid")
            return []
        if b == "field541":
            self.errorEvent.emit(1, 200, "Invalid value in field # 541")
            return []
        if b == "nosecdef":
            self.errorEvent.emit(1, 200, "No security definition has been found for the request")
            return []
        return b or []

    def placeOrder(self, *a, **k):
        raise AssertionError("placeOrder must never be called")

    def reqMktData(self, *a, **k):
        raise AssertionError("reqMktData must never be called")

    def reqPositions(self, *a, **k):
        raise AssertionError("reqPositions must never be called")


def _inst(asset_class="equity", exchange="XPAR", currency="EUR", isin="FR0000131104",
          expiry="", strike=None, option_right="", con_id=None, multiplier="1"):
    return SimpleNamespace(symbol=isin, asset_class=asset_class, exchange=exchange,
                           primary_exchange=exchange, trading_currency=currency, isin=isin,
                           expiry=expiry, strike=strike, option_right=option_right, con_id=con_id,
                           local_symbol=isin, multiplier=multiplier)


def _detail(con_id, *, primary="SBF", exchange="SMART", currency="EUR", sec_type="STK",
            expiry="", strike=0.0, right="", multiplier="1"):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol="X", localSymbol="X", secType=sec_type,
                                 exchange=exchange, primaryExchange=primary, currency=currency,
                                 lastTradeDateOrContractMonth=expiry, strike=strike, right=right,
                                 multiplier=multiplier, underConId=0),
        longName="X", minTick=0.01, stockType="", country="FR")


def _fake_ib_async():
    m = types.ModuleType("ib_async")
    captured: dict = {}

    class Contract:
        def __init__(self, **kw):
            captured.clear()
            captured.update(kw)

    m.Contract = Contract
    return m, captured


def _build(inst, monkey_target=sys):
    """Build a contract via the real _build_contract with a monkeypatched ib_async; return captured kwargs."""
    m, captured = _fake_ib_async()
    saved = sys.modules.get("ib_async")
    sys.modules["ib_async"] = m
    try:
        IbkrQualificationClient._build_contract(build_request_spec(inst))
    finally:
        if saved is not None:
            sys.modules["ib_async"] = saved
        else:
            sys.modules.pop("ib_async", None)
    return captured


# ------------------------------------------------------------------ registry (fail-closed, provenance)
def test_registry_fail_closed_and_provenance():
    assert venue.resolve_ibkr_exchanges("XPAR") == ("SBF",)          # REPO-grounded
    assert venue.resolve_ibkr_exchanges("XETR") == ("IBIS",)
    assert venue.resolve_ibkr_exchanges("AFSO") == ()               # unmapped → fail-closed
    assert venue.resolve_ibkr_exchanges(None) == ()
    assert venue.is_ibkr_exchange("SBF") and not venue.is_ibkr_exchange("XPAR")
    for m in venue._SEED:                                            # every entry carries provenance
        assert m.provenance and m.confidence in ("high", "medium")


# ------------------------------------------------------------------ error-200 classification (the canary)
def test_error200_invalid_destination_is_venue_resolution_not_not_tradable():
    err = classify_contract_query_error([(200, "The destination or exchange selected is Invalid")])
    assert isinstance(err, VenueResolutionError) and err.code == "venue_unresolved"


def test_error200_field_541_is_venue_resolution():
    err = classify_contract_query_error([(200, "Invalid value in field # 541")])
    assert isinstance(err, VenueResolutionError)


def test_error200_no_security_definition_stays_not_tradable():
    nosec = classify_contract_query_error([(200, "No security definition has been found for the request")])
    assert nosec is None                                            # a genuine not-found → NOT_TRADABLE
    assert classify_contract_query_error([]) is None                # no error captured → empty → NOT_TRADABLE


# ------------------------------------------------------------------ ISIN-based discovery for all 7 classes
def test_cash_classes_query_by_isin_never_raw_mic_never_symbol_isin():
    # § WP11 — BONDs now use a distinct query (ISIN in Contract.symbol, secType=BOND); see the WP11 test
    # module. The ISIN-in-secIdType discovery below covers the remaining cash classes.
    for ac in ("equity", "etf", "fund", "warrant"):
        kw = _build(_inst(asset_class=ac, exchange="XPAR"))
        assert kw.get("secIdType") == "ISIN" and kw.get("secId") == "FR0000131104"
        assert kw.get("exchange") == "SMART"                        # SMART for search/routing only
        assert kw.get("primaryExchange") == "SBF"                   # mapped IBKR code, never the MIC
        assert "symbol" not in kw                                   # never ISIN-in-symbol
        assert kw.get("primaryExchange") != "XPAR" and kw.get("exchange") != "XPAR"


def test_cash_unmapped_mic_has_no_primary_exchange_and_never_sends_mic():
    kw = _build(_inst(asset_class="equity", exchange="AFSO"))       # unmapped MIC (a canary venue)
    assert kw.get("exchange") == "SMART" and "primaryExchange" not in kw
    assert "AFSO" not in kw.values()


def test_derivatives_mapped_use_ibkr_exchange_with_full_identity():
    kw = _build(_inst(asset_class="future", exchange="XEUR", isin="DE000TESTFUT1", expiry="20261218"))
    assert kw.get("exchange") == "EUREX" and kw.get("secId") == "DE000TESTFUT1"
    assert kw.get("lastTradeDateOrContractMonth") == "20261218"
    kw2 = _build(_inst(asset_class="option", exchange="XEUR", isin="DE000TESTOPT1",
                       expiry="20261218", strike=100.0, option_right="C"))
    assert kw2.get("exchange") == "EUREX" and kw2.get("strike") == 100.0 and kw2.get("right") == "C"


def test_derivative_unmapped_mic_raises_venue_resolution_not_query():
    try:
        _build(_inst(asset_class="future", exchange="DKFI", isin="SE0030063152", expiry="20261229"))
    except VenueResolutionError as e:
        assert e.code == "venue_unresolved"
    else:
        raise AssertionError("expected VenueResolutionError for an unmapped derivative venue")


# ------------------------------------------------------------------ verification across MIC↔IBKR namespaces
def test_verification_matches_after_mic_to_ibkr_translation():
    inst = _inst(asset_class="equity", exchange="XPAR", currency="EUR")
    cand = contract_detail_to_global(_detail(101, primary="SBF", currency="EUR", sec_type="STK"))
    out = match_contract(inst, [cand])
    assert out.status is QualificationStatus.VERIFIED and out.matched.con_id == 101


def test_verification_fail_closed_on_unmapped_mic():
    inst = _inst(asset_class="equity", exchange="AFSO", currency="EUR")     # unmapped → cannot assert venue
    cand = contract_detail_to_global(_detail(101, primary="SBF", currency="EUR", sec_type="STK"))
    out = match_contract(inst, [cand])
    assert out.status is not QualificationStatus.VERIFIED   # never verify a venue we cannot confirm


def test_verification_rejects_wrong_venue():
    inst = _inst(asset_class="equity", exchange="XPAR", currency="EUR")     # expects SBF
    cand = contract_detail_to_global(_detail(101, primary="IBIS", currency="EUR", sec_type="STK"))  # Xetra
    assert match_contract(inst, [cand]).status is not QualificationStatus.VERIFIED


# ------------------------------------------------------------------ _qualify_one classification + budget
async def test_qualify_one_venue200_is_error_retryable_budget_neutral():
    client = IbkrQualificationClient(FakeIB({"FR0000131104": "venue200"}), contract_factory=lambda r: r,
                                     request_timeout=None)
    status, _matched, _reason, _n, conn_lost, count_attempt = await _qualify_one(client, _inst(), 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE          # NOT NOT_TRADABLE
    assert conn_lost is False                                     # per-instrument, run is NOT aborted
    assert count_attempt is False   # budget-neutral: never escalates to ERROR_PERMANENT


async def test_qualify_one_field541_is_error_retryable_not_not_tradable():
    client = IbkrQualificationClient(FakeIB({"DE000TESTFUT1": "field541"}), contract_factory=lambda r: r,
                                     request_timeout=None)
    inst = _inst(asset_class="future", exchange="XEUR", isin="DE000TESTFUT1", expiry="20261218")
    status, *_rest, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False


async def test_qualify_one_no_security_definition_is_not_tradable():
    client = IbkrQualificationClient(FakeIB({"FR0000131104": "nosecdef"}), contract_factory=lambda r: r,
                                     request_timeout=None)
    status, _matched, _reason, _n, _conn_lost, count_attempt = await _qualify_one(client, _inst(), 0, 3)
    assert status is QualificationStatus.NOT_TRADABLE            # a genuine, well-formed not-found
    assert count_attempt is True


async def test_qualify_one_unmapped_mic_with_returned_contract_is_error_retryable_not_not_tradable():
    # § WP10 regression (adversarial finding): a valid cash instrument on an UNMAPPED FIRDS MIC whose ISIN
    # discovery SUCCEEDS (a real contract is returned) must NOT be marked terminal NOT_TRADABLE just because
    # the venue registry lacks the MIC — it is a venue-resolution gap → re-queryable, budget-neutral.
    fib = FakeIB({"FR0000131104": [_detail(101, primary="SBF", currency="EUR", sec_type="STK")]})
    client = IbkrQualificationClient(fib, contract_factory=lambda r: r, request_timeout=None)
    inst = _inst(asset_class="equity", exchange="AFSO", currency="EUR")   # AFSO is unmapped
    status, _m, _r, _n, _conn_lost, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE          # NOT a false terminal NOT_TRADABLE
    assert count_attempt is False


async def test_qualify_one_unmapped_mic_genuinely_empty_stays_not_tradable():
    # By contrast, an unmapped MIC whose well-formed ISIN query returns NOTHING is a real not-found.
    client = IbkrQualificationClient(FakeIB({"FR0000131104": []}), contract_factory=lambda r: r,
                                     request_timeout=None)
    inst = _inst(asset_class="equity", exchange="AFSO", currency="EUR")
    status, _m, _r, _n, _conn_lost, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.NOT_TRADABLE and count_attempt is True


async def test_qualify_one_unmapped_derivative_is_error_retryable_before_any_request():
    fib = FakeIB({})
    client = IbkrQualificationClient(fib, request_timeout=None)   # real _build_contract raises pre-request
    inst = _inst(asset_class="future", exchange="DKFI", isin="SE0030063152", expiry="20261229")
    status, *_rest, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False
    assert fib.calls == []                                        # no IBKR request was ever issued


async def test_qualify_one_connection_lost_aborts_and_is_budget_neutral():
    class Dead(FakeIB):
        def isConnected(self):
            return False
    client = IbkrQualificationClient(Dead({}), contract_factory=lambda r: r, request_timeout=None)
    status, _matched, _reason, _n, conn_lost, count_attempt = await _qualify_one(client, _inst(), 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and conn_lost is True and count_attempt is False


async def test_qualify_one_unique_match_verifies_with_conid_from_reply():
    fib = FakeIB({"FR0000131104": [_detail(101, primary="SBF", currency="EUR", sec_type="STK")]})
    client = IbkrQualificationClient(fib, contract_factory=lambda r: r, request_timeout=None)
    status, matched, _reason, _n, _conn_lost, _count_attempt = await _qualify_one(client, _inst(), 0, 3)
    assert status is QualificationStatus.VERIFIED and matched.con_id == 101
    assert fib.calls == ["FR0000131104"]   # exactly one reqContractDetails call, nothing else


async def test_client_only_calls_reqcontractdetails():
    fib = FakeIB({"FR0000131104": [_detail(101)]})
    client = IbkrQualificationClient(fib, contract_factory=lambda r: r, request_timeout=None)
    await client.fetch_contract_details(build_request_spec(_inst()))
    assert fib.calls == ["FR0000131104"]                          # no placeOrder/reqMktData/reqPositions


# ------------------------------------------------------------------ standalone runner
def _run_all():
    import inspect
    ok = 0
    fns = {n: f for n, f in globals().items() if n.startswith("test_") and callable(f)}
    for name in sorted(fns):
        fn = fns[name]
        try:
            if inspect.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"PASS {name}")
            ok += 1
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            raise
    print(f"ALL {ok}/{len(fns)} WP10 TESTS PASS")


if __name__ == "__main__":
    _run_all()
