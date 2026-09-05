"""§ WP11 — Canonical Venue & Instrument Identity Resolution: deterministic OFFLINE regression tests.

Covers the ISIN-anchored cash verification that WP11 introduces (verify on IBKR's ECHOED ISIN + a real
returned venue, never the FIRDS MIC, never the ISIN search key alone), the fail-closed edge cases the
adversarial design review flagged (echo absent + unmapped MIC → never verify; echo mismatch; SMART-only;
currency deviation; multiple listings; conId collision), the BOND query fix (ISIN in Contract.symbol) and its
non-terminal not-found, complete/incomplete derivative identity, and the venue-category metadata that records
a FIRDS MIC as a non-primary MTF/SI/OTF.

No network, no ib_async install required: ib_async is monkeypatched for _build_contract, a fake IB with an
errorEvent stub drives the client, and store-backed tests use a temp SQLite store.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

from atp.core.enums import AssetClass
from atp.instruments import ibkr_venue as venue
from atp.instruments.ibkr_catalog import contract_detail_to_global
from atp.instruments.model import InstrumentRecord
from atp.instruments.qualification import (
    _QUALIFICATION_DETAIL_KINDS,
    IbkrQualificationClient,
    QualificationConfig,
    QualificationStatus,
    _qualification_detail,
    _qualify_one,
    build_request_spec,
    match_contract,
    qualify_instruments,
)
from atp.store import open_store


# ------------------------------------------------------------------ fakes / helpers
class _Event:
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
        self.script = script          # key(isin|symbol) -> list[detail] | tag
        self._connected = connected
        self.calls = []
        self.errorEvent = _Event()

    def isConnected(self):
        return self._connected

    async def reqContractDetailsAsync(self, contract):
        key = getattr(contract, "isin", None) or getattr(contract, "symbol", None)
        self.calls.append(key)
        b = self.script.get(key)
        if b == "nosecdef":
            self.errorEvent.emit(1, 200, "No security definition has been found for the request")
            return []
        if b == "ambiguous":
            self.errorEvent.emit(1, 200, "The contract description specified for X is ambiguous")
            return []
        return b or []

    def placeOrder(self, *a, **k):
        raise AssertionError("placeOrder must never be called")

    def reqMktData(self, *a, **k):
        raise AssertionError("reqMktData must never be called")


def _inst(asset_class="equity", exchange="AFSO", currency="EUR", isin="FR0000131104",
          expiry="", strike=None, option_right="", con_id=None, multiplier="1"):
    return SimpleNamespace(symbol=isin, asset_class=asset_class, exchange=exchange,
                           primary_exchange=exchange, trading_currency=currency, isin=isin,
                           expiry=expiry, strike=strike, option_right=option_right, con_id=con_id,
                           local_symbol=isin, multiplier=multiplier)


def _detail(con_id, *, isin="", primary="SBF", exchange="SMART", currency="EUR", sec_type="STK",
            expiry="", strike=0.0, right="", multiplier="1", isin_tag="ISIN"):
    """A ContractDetails stub. `isin` populates secIdList (as IBKR echoes it); `isin_tag` lets a test vary
    the tag casing. An empty isin means IBKR returned no ISIN echo."""
    sec_id_list = [SimpleNamespace(tag=isin_tag, value=isin)] if isin else []
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, symbol="X", localSymbol="X", secType=sec_type,
                                 exchange=exchange, primaryExchange=primary, currency=currency,
                                 lastTradeDateOrContractMonth=expiry, strike=strike, right=right,
                                 multiplier=multiplier, underConId=0),
        secIdList=sec_id_list, longName="X", minTick=0.01, stockType="", country="FR")


def _fake_ib_async():
    m = types.ModuleType("ib_async")
    captured: dict = {}

    class Contract:
        def __init__(self, **kw):
            captured.clear()
            captured.update(kw)

    m.Contract = Contract
    return m, captured


def _build(inst):
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


def _client(script):
    return IbkrQualificationClient(FakeIB(script), contract_factory=lambda r: r, request_timeout=None)


# ------------------------------------------------------------------ secIdList capture
def test_contract_detail_captures_isin_echo_case_insensitive():
    assert contract_detail_to_global(_detail(1, isin="FR0000131104")).isin == "FR0000131104"
    assert contract_detail_to_global(_detail(1, isin="fr0000131104", isin_tag="Isin")).isin == "FR0000131104"
    assert contract_detail_to_global(_detail(1)).isin == ""                       # no echo → fail-closed ""


# ------------------------------------------------------------------ D1: ISIN-anchored cash verification
def test_cash_verifies_via_isin_echo_on_unmapped_mic():
    # THE WP11 capability: an unmapped MTF/SI MIC (AFSO) whose ISIN IBKR echoes back, in the requested
    # currency, with a real returned venue → VERIFIED, recording IBKR's returned conId (venue-of-record SBF).
    inst = _inst(exchange="AFSO", currency="EUR", isin="FR0000131104")
    cand = contract_detail_to_global(_detail(101, isin="FR0000131104", primary="SBF", currency="EUR"))
    out = match_contract(inst, [cand])
    assert out.status is QualificationStatus.VERIFIED and out.matched.con_id == 101


def test_cash_isin_echo_mismatch_never_verifies():
    inst = _inst(exchange="AFSO", isin="FR0000131104")
    cand = contract_detail_to_global(_detail(101, isin="DE000OTHER001", primary="SBF"))   # different ISIN
    assert match_contract(inst, [cand]).status is not QualificationStatus.VERIFIED


def test_cash_isin_echo_mismatch_not_rescued_by_venue_match():
    # The dangerous case: a MAPPED MIC (XPAR→SBF) whose returned venue matches (anchor B) BUT whose echoed
    # ISIN differs. The echo-mismatch hard-disqualifier must veto the venue anchor → never VERIFIED. (Without
    # the guard, _identity_anchor_ok would fall through to a venue match and falsely verify the wrong line.)
    inst = _inst(exchange="XPAR", currency="EUR", isin="FR0000131104")
    cand = contract_detail_to_global(_detail(101, isin="DE000OTHER001", primary="SBF", currency="EUR"))
    assert match_contract(inst, [cand]).status is not QualificationStatus.VERIFIED


async def test_cash_no_echo_and_unmapped_mic_never_verifies():
    # "no false verification": unmapped MIC AND no ISIN echo → the proof would be the search key alone →
    # NEVER verified; a returned-but-unconfirmable contract is the re-queryable venue-unresolved gap.
    inst = _inst(exchange="AFSO", isin="FR0000131104")
    client = _client({"FR0000131104": [_detail(101, isin="", primary="SBF", currency="EUR")]})
    status, _m, reason, _n, _cl, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False
    assert "venue_unresolved" in reason


def test_cash_venue_match_still_verifies_without_echo():
    # WP10 anchor (B) retained: a MAPPED MIC (XPAR→SBF) whose returned venue matches verifies with no echo.
    inst = _inst(exchange="XPAR", currency="EUR", isin="FR0000131104")
    cand = contract_detail_to_global(_detail(101, isin="", primary="SBF", currency="EUR"))
    assert match_contract(inst, [cand]).status is QualificationStatus.VERIFIED


def test_cash_smart_only_no_real_venue_never_verifies():
    # echo present + currency ok, but IBKR returned only SMART (no real primaryExchange) → area-3 requires a
    # real returned venue → NOT verified (fail-closed; we never store SMART as the venue).
    inst = _inst(exchange="AFSO", isin="FR0000131104")
    cand = contract_detail_to_global(_detail(101, isin="FR0000131104", primary="SMART", exchange="SMART"))
    assert match_contract(inst, [cand]).status is not QualificationStatus.VERIFIED


def test_cash_multiple_listings_same_currency_is_ambiguous():
    inst = _inst(exchange="AFSO", currency="EUR", isin="FR0000131104")
    cands = [contract_detail_to_global(_detail(101, isin="FR0000131104", primary="SBF", currency="EUR")),
             contract_detail_to_global(_detail(202, isin="FR0000131104", primary="IBIS", currency="EUR"))]
    assert match_contract(inst, cands).status is QualificationStatus.AMBIGUOUS


async def test_cash_currency_deviation_is_error_retryable_not_not_tradable():
    inst = _inst(exchange="AFSO", currency="EUR", isin="FR0000131104")     # want EUR
    client = _client({"FR0000131104": [_detail(101, isin="FR0000131104", primary="SBF", currency="USD")]})
    status, _m, reason, _n, _cl, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False
    assert "currency_conflict" in reason


async def test_cash_genuine_not_found_stays_not_tradable():
    inst = _inst(exchange="XPAR", currency="EUR", isin="FR0000131104")     # mapped MIC, empty result
    client = _client({"FR0000131104": "nosecdef"})
    status, _m, _r, _n, _cl, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.NOT_TRADABLE and count_attempt is True


async def test_ambiguous_error_200_maps_to_ambiguous_not_venue_unresolved():
    inst = _inst(exchange="XPAR", currency="EUR", isin="FR0000131104")
    client = _client({"FR0000131104": "ambiguous"})
    status, _m, _r, _n, _cl, _ca = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.AMBIGUOUS


# ------------------------------------------------------------------ D3: BONDS
def test_bond_query_puts_isin_in_symbol_not_secidtype():
    kw = _build(_inst(asset_class="bond", exchange="AURO", isin="FR001400Q3S1"))
    assert kw.get("secType") == "BOND" and kw.get("symbol") == "FR001400Q3S1"
    assert kw.get("exchange") == "SMART" and "secIdType" not in kw and "secId" not in kw
    assert "AURO" not in kw.values()                                       # never the raw MIC


async def test_bond_not_found_is_error_retryable_not_not_tradable():
    inst = _inst(asset_class="bond", exchange="AURO", currency="EUR", isin="FR001400Q3S1")
    client = _client({"FR001400Q3S1": "nosecdef"})
    status, _m, reason, _n, _cl, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False
    assert "bond_not_found" in reason


# ------------------------------------------------------------------ D2: DERIVATIVES
def test_derivative_full_identity_verifies_on_mapped_venue():
    inst = _inst(asset_class="option", exchange="XEUR", currency="EUR", isin="DE000TESTOPT1",
                 expiry="20261218", strike=100.0, option_right="C", multiplier="100")
    cand = contract_detail_to_global(_detail(501, isin="DE000TESTOPT1", primary="EUREX", exchange="EUREX",
                                             currency="EUR", sec_type="OPT", expiry="20261218", strike=100.0,
                                             right="C", multiplier="100"))
    assert match_contract(inst, [cand]).status is QualificationStatus.VERIFIED


def test_derivative_incomplete_identity_not_verified():
    inst = _inst(asset_class="option", exchange="XEUR", currency="EUR", isin="DE000TESTOPT1",
                 expiry="20261218", strike=100.0, option_right="C", multiplier="100")
    cand = contract_detail_to_global(_detail(501, isin="DE000TESTOPT1", primary="EUREX", exchange="EUREX",
                                             currency="EUR", sec_type="OPT", expiry="20270115",   # wrong exp
                                             strike=100.0, right="C", multiplier="100"))
    assert match_contract(inst, [cand]).status is not QualificationStatus.VERIFIED


async def test_derivative_unmapped_venue_stays_error_retryable_no_query():
    fib = FakeIB({})
    client = IbkrQualificationClient(fib, request_timeout=None)   # real _build_contract raises pre-request
    inst = _inst(asset_class="future", exchange="DKFI", isin="SE0030063152", expiry="20261229")
    status, matched, reason, _n, _cl, count_attempt = await _qualify_one(client, inst, 0, 3)
    assert status is QualificationStatus.ERROR_RETRYABLE and count_attempt is False
    assert fib.calls == []                                        # no IBKR request issued
    # the exception-path reason carries the machine token so qualification_detail labels it (not NULL)
    assert "venue_unresolved" in reason
    assert _qualification_detail(status, reason, inst, matched) == "venue_unresolved"


# ------------------------------------------------------------------ registry: new mappings + venue category
def test_new_primary_mappings_are_grounded():
    assert venue.resolve_ibkr_exchanges("XMAD") == ("BM",)
    assert venue.resolve_ibkr_exchanges("XTKS") == ("TSEJ",)
    assert venue.resolve_ibkr_exchanges("XASX") == ("ASX",)
    assert venue.resolve_ibkr_exchanges("XSES") == ("SGX",)
    assert venue.resolve_ibkr_exchanges("XASE") == ("NYSEAMER", "AMEX")   # NYSEAMER (REPO) + legacy AMEX
    for m in venue._SEED:                                             # every entry still carries provenance
        assert m.provenance and m.confidence in ("high", "medium")


def test_non_primary_venue_metadata_and_fail_closed():
    # § WP11 areas 1/2 — the canary MICs are recognised as non-primary venues, NEVER mapped to an IBKR
    # exchange (resolve stays fail-closed), and carry their ISO operating MIC.
    assert venue.is_non_primary_venue("AACA") and venue.venue_category("AACA") == "si"
    assert venue.venue_category("BGEM") == "mtf" and venue.operating_mic("BGEM") == "XMIL"
    assert venue.venue_category("D2XC") == "deriv" and venue.operating_mic("DKED") == "XSTO"
    assert venue.venue_category("XMAD") == "exchange" and not venue.is_non_primary_venue("XMAD")
    for mic in ("AACA", "BGEM", "D2XC", "DKED", "BTFE"):
        assert venue.resolve_ibkr_exchanges(mic) == ()               # never an IBKR exchange (fail-closed)


def test_qualification_detail_vocabulary_is_closed():
    # Every sub-classification _qualification_detail can emit is in the documented closed vocabulary (or
    # None); the connection-loss reason maps to None (not a WP11 sub-kind). Enforces the doc's closed-vocab
    # claim and keeps _QUALIFICATION_DETAIL_KINDS load-bearing.
    inst = _inst(exchange="AFSO", isin="FR0000131104")
    echo = contract_detail_to_global(_detail(1, isin="FR0000131104", primary="SBF"))
    novenue = contract_detail_to_global(_detail(1, isin="", primary="SBF"))
    cases = [
        (QualificationStatus.VERIFIED, "unique contract match", echo),
        (QualificationStatus.VERIFIED, "unique contract match", novenue),
        (QualificationStatus.AMBIGUOUS, "2 distinct contracts", None),
        (QualificationStatus.NOT_TRADABLE, "no contract details returned", None),
        (QualificationStatus.ERROR_RETRYABLE, "venue_unresolved: unmapped", None),
        (QualificationStatus.ERROR_RETRYABLE, "currency_conflict: wrong ccy", None),
        (QualificationStatus.ERROR_RETRYABLE, "bond_not_found: none", None),
        (QualificationStatus.ERROR_RETRYABLE, "IBKR connection lost during request", None),
    ]
    emitted = {_qualification_detail(s, r, inst, m) for s, r, m in cases}
    emitted.discard(None)
    assert emitted <= _QUALIFICATION_DETAIL_KINDS                     # closed vocabulary
    assert {"verified_isin_echo", "verified_venue_match", "currency_conflict", "bond_not_found",
            "venue_unresolved"} <= emitted
    conn = _qualification_detail(QualificationStatus.ERROR_RETRYABLE, "IBKR connection lost", inst, None)
    assert conn is None                                              # broker outage is not a WP11 sub-kind


# ------------------------------------------------------------------ store-backed: migration + detail + conId
def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _upsert(store, *, asset_class, exchange, isin, currency="EUR"):
    rec = InstrumentRecord(symbol=isin, asset_class=asset_class, exchange=exchange, trading_currency=currency,
                           isin=isin, local_symbol=isin, primary_exchange=exchange, region="EUROPE",
                           country="FR", timezone="Europe/Paris", trading_calendar="eu", multiplier="1",
                           source="t")
    store.im_upsert_instrument(rec.as_record())
    return rec.instrument_id


def test_migration_032_columns_present_and_writable():
    store = _store()
    iid = _upsert(store, asset_class=AssetClass.EQUITY, exchange="AFSO", isin="FR0000131104")
    row = store.im_get_instrument(iid)
    assert row.qualification_detail is None and row.ibkr_primary_exchange is None   # additive, nullable
    # iq_apply_outcome accepts and persists the two new fields
    rid = "wp11test"
    store.iq_create_run(run_id=rid, request_checksum="c", run_label="t", exchange=None, batch_size=1,
                        pause_seconds=0.0)
    store.iq_advance_run_status(rid, "PLANNED", "RUNNING")
    store.iq_apply_outcome(iid, run_id=rid, qualification_status="VERIFIED", reason="ok",
                           verification_status="verified", con_id=101, set_last_verified=True,
                           qualification_detail="verified_isin_echo", ibkr_primary_exchange="SBF",
                           event={"id": f"{rid}-e1", "seq": 1, "instrument_id": iid,
                                  "event_type": "QUALIFY_RESULT", "severity": "INFO"})
    row = store.im_get_instrument(iid)
    assert row.qualification_detail == "verified_isin_echo" and row.ibkr_primary_exchange == "SBF"


async def test_end_to_end_isin_echo_verifies_and_records_detail_and_venue():
    store = _store()
    iid = _upsert(store, asset_class=AssetClass.EQUITY, exchange="AFSO", isin="FR0000131104")
    client = _client({"FR0000131104": [_detail(101, isin="FR0000131104", primary="SBF", currency="EUR")]})
    await qualify_instruments(store, client, run_label="wp11", config=QualificationConfig(batch_size=5))
    row = store.im_get_instrument(iid)
    assert row.qualification_status == "VERIFIED" and row.con_id == 101
    assert row.qualification_detail == "verified_isin_echo"
    assert row.ibkr_primary_exchange == "SBF"                     # IBKR's returned venue
    assert row.exchange == "AFSO"                                 # FIRDS MIC provenance preserved


async def test_conid_collision_downgrades_second_to_ambiguous():
    store = _store()
    a = _upsert(store, asset_class=AssetClass.EQUITY, exchange="AFSO", isin="FR0000000001")
    b = _upsert(store, asset_class=AssetClass.EQUITY, exchange="XPAR", isin="FR0000000002")
    # both ISINs resolve to the SAME conId 900 (a duplicate FIRDS listing of one contract)
    client = _client({"FR0000000001": [_detail(900, isin="FR0000000001", primary="SBF", currency="EUR")],
                      "FR0000000002": [_detail(900, isin="FR0000000002", primary="SBF", currency="EUR")]})
    await qualify_instruments(store, client, run_label="wp11", config=QualificationConfig(batch_size=5))
    statuses = {store.im_get_instrument(a).qualification_status,
                store.im_get_instrument(b).qualification_status}
    assert statuses == {"VERIFIED", "AMBIGUOUS"}                  # exactly one keeps the conId, never a crash
    verified = [store.im_get_instrument(i) for i in (a, b)
                if store.im_get_instrument(i).qualification_status == "VERIFIED"]
    assert verified[0].con_id == 900


# ------------------------------------------------------------------ standalone runner
def _run_all():
    import inspect
    ok = 0
    fns = {n: f for n, f in globals().items() if n.startswith("test_") and callable(f)}
    for name in sorted(fns):
        fn = fns[name]
        try:
            asyncio.run(fn()) if inspect.iscoroutinefunction(fn) else fn()
            print(f"PASS {name}")
            ok += 1
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            raise
    print(f"ALL {ok}/{len(fns)} WP11 TESTS PASS")


if __name__ == "__main__":
    _run_all()
