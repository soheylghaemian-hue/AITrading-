"""§ WP3 unit — the fail-closed IBKR qualification matcher / request builder / error classifier (pure).

No store, no IBKR. Proves the identity-resolution contract: VERIFIED only on a UNIQUE consistent contract,
never by symbol alone; several plausible ⇒ AMBIGUOUS; missing/conflicting ⇒ never VERIFIED.
SAFETY: reference data only — no orders/execution/market-data path.
"""
from __future__ import annotations

from types import SimpleNamespace

from atp.instruments.global_catalog import GlobalContract
from atp.instruments.qualification import (
    ConnectionUnavailableError,
    MarketDataNotEntitledError,
    NotTradableError,
    PermanentQualificationError,
    QualificationStatus,
    RetryableQualificationError,
    asset_class_to_ib_sec_type,
    build_request_spec,
    classify_ibkr_error,
    match_contract,
)


def inst(*, asset_class="equity", currency="USD", exchange="NASDAQ", primary_exchange="NASDAQ",
         con_id=None, multiplier="1", expiry=None, strike=None, option_right=None, symbol="MSFT",
         local_symbol="MSFT", instrument_id="INS-x"):
    return SimpleNamespace(instrument_id=instrument_id, symbol=symbol, asset_class=asset_class,
                           trading_currency=currency, exchange=exchange, primary_exchange=primary_exchange,
                           con_id=con_id, multiplier=multiplier, expiry=expiry, strike=strike,
                           option_right=option_right, local_symbol=local_symbol)


def gc(con_id, *, sec_type="STK", exchange="SMART", primary="NASDAQ", currency="USD", expiry="",
       strike=None, right="", multiplier=1.0):
    return GlobalContract(con_id=con_id, symbol="MSFT", local_symbol="MSFT", sec_type=sec_type,
                          exchange=exchange, primary_exchange=primary, currency=currency, expiry=expiry,
                          strike=strike, right=right, multiplier=multiplier)


# --------------------------------------------------------------------- status model
def test_status_values_match_the_spec():
    assert [s.value for s in QualificationStatus] == [
        "DISCOVERED", "QUALIFICATION_PENDING", "VERIFIED", "AMBIGUOUS", "NOT_TRADABLE",
        "MARKET_DATA_NOT_ENTITLED", "ERROR_RETRYABLE", "ERROR_PERMANENT"]


# --------------------------------------------------------------------- matcher: verify / ambiguous
def test_unique_consistent_contract_verifies():
    out = match_contract(inst(), [gc(1)])
    assert out.status is QualificationStatus.VERIFIED and out.matched.con_id == 1


def test_same_conid_across_exchanges_is_still_one_verified():
    # IBKR returns one row per valid exchange for the SAME contract → dedup by conId → unique.
    out = match_contract(inst(), [gc(1, exchange="SMART"), gc(1, exchange="ISLAND", primary="NASDAQ")])
    assert out.status is QualificationStatus.VERIFIED and out.matched.con_id == 1


def test_two_distinct_consistent_contracts_are_ambiguous():
    out = match_contract(inst(), [gc(1), gc(2)])
    assert out.status is QualificationStatus.AMBIGUOUS and out.matched is None
    assert out.consistent_con_ids == (1, 2)


# --------------------------------------------------------------------- fail-closed: never VERIFIED
def test_no_details_is_not_tradable():
    out = match_contract(inst(), [])
    assert out.status is QualificationStatus.NOT_TRADABLE and "no contract details" in out.reason


def test_symbol_alone_cannot_verify_currency_mismatch():
    out = match_contract(inst(currency="USD"), [gc(1, currency="EUR")])
    assert out.status is QualificationStatus.NOT_TRADABLE


def test_venue_mismatch_is_not_consistent():
    out = match_contract(inst(exchange="NYSE", primary_exchange="NYSE"), [gc(1, primary="NASDAQ")])
    assert out.status is QualificationStatus.NOT_TRADABLE


def test_conid_mismatch_when_instrument_already_pinned():
    out = match_contract(inst(con_id=999), [gc(1)])
    assert out.status is QualificationStatus.NOT_TRADABLE


def test_zero_conid_candidate_is_ignored():
    out = match_contract(inst(), [gc(0)])
    assert out.status is QualificationStatus.NOT_TRADABLE


def test_empty_venue_strings_never_verify_an_unconfirmed_venue():
    # Regression: a NULL primary_exchange (→"") must not "match" a candidate's blank venue field via ''∈venues.
    i = inst(exchange="NASDAQ", primary_exchange="")
    assert match_contract(i, [gc(888, exchange="SMART", primary="")]).status is QualificationStatus.NOT_TRADABLE
    assert match_contract(i, [gc(889, exchange="", primary="")]).status is QualificationStatus.NOT_TRADABLE
    # a genuine venue echo (SMART route + real primaryExchange) still verifies
    assert match_contract(i, [gc(890, exchange="SMART", primary="NASDAQ")]).status is QualificationStatus.VERIFIED


def test_smart_routing_token_never_confirms_a_venue():
    # Regression: "SMART" is a routing pseudo-venue, not a listing venue — matching on it alone must NOT verify.
    smart_inst = inst(exchange="SMART", primary_exchange="SMART")
    assert match_contract(smart_inst, [gc(1, exchange="SMART", primary="NYSE")]).status \
        is QualificationStatus.NOT_TRADABLE
    # conflicting real venues that merely share the SMART routing token must not verify either
    conflicting = inst(exchange="SMART", primary_exchange="NYSE")
    assert match_contract(conflicting, [gc(2, exchange="SMART", primary="NASDAQ")]).status \
        is QualificationStatus.NOT_TRADABLE
    # but a shared REAL venue (NYSE) still verifies through the SMART routing token
    assert match_contract(conflicting, [gc(3, exchange="SMART", primary="NYSE")]).status \
        is QualificationStatus.VERIFIED


# --------------------------------------------------------------------- derivative constraints
def test_future_multiplier_and_expiry_must_match():
    fut = inst(asset_class="future", exchange="CME", primary_exchange="CME", multiplier="50", expiry="20261218")
    good = gc(7, sec_type="FUT", exchange="CME", primary="CME", multiplier=50.0, expiry="20261218")
    assert match_contract(fut, [good]).status is QualificationStatus.VERIFIED
    wrong_mult = gc(7, sec_type="FUT", exchange="CME", primary="CME", multiplier=10.0, expiry="20261218")
    assert match_contract(fut, [wrong_mult]).status is QualificationStatus.NOT_TRADABLE
    wrong_exp = gc(7, sec_type="FUT", exchange="CME", primary="CME", multiplier=50.0, expiry="20270319")
    assert match_contract(fut, [wrong_exp]).status is QualificationStatus.NOT_TRADABLE


def test_option_strike_and_right_must_match():
    opt = inst(asset_class="option", exchange="CBOE", primary_exchange="CBOE", multiplier="100",
               expiry="20261218", strike="200", option_right="C")
    good = gc(8, sec_type="OPT", exchange="CBOE", primary="CBOE", multiplier=100.0, expiry="20261218",
              strike=200.0, right="C")
    assert match_contract(opt, [good]).status is QualificationStatus.VERIFIED
    wrong_right = gc(8, sec_type="OPT", exchange="CBOE", primary="CBOE", multiplier=100.0, expiry="20261218",
                     strike=200.0, right="P")
    assert match_contract(opt, [wrong_right]).status is QualificationStatus.NOT_TRADABLE


def test_equity_and_etf_are_compatible_but_not_other_classes():
    # documented relaxation: IBKR carries ETFs as STK; equity↔etf compatible
    assert match_contract(inst(asset_class="etf"), [gc(1, sec_type="STK")]).status is QualificationStatus.VERIFIED
    # a bond returned for an equity request is never consistent
    assert match_contract(inst(asset_class="equity"),
                          [gc(1, sec_type="BOND")]).status is QualificationStatus.NOT_TRADABLE


# --------------------------------------------------------------------- request builder / mapping
def test_request_builder_uses_full_identity_not_symbol_only():
    req = build_request_spec(inst(asset_class="future", exchange="CME", primary_exchange="CME",
                                  expiry="20261218", con_id=42))
    assert req.sec_type == "FUT" and req.currency == "USD" and req.exchange == "CME"
    assert req.expiry == "20261218" and req.con_id == 42


def test_asset_class_to_ib_sec_type():
    assert asset_class_to_ib_sec_type("equity") == "STK"
    assert asset_class_to_ib_sec_type("etf") == "STK"
    assert asset_class_to_ib_sec_type("future") == "FUT"
    assert asset_class_to_ib_sec_type("option") == "OPT"
    assert asset_class_to_ib_sec_type("fx") == "CASH"


# --------------------------------------------------------------------- error classification
def test_error_classification_is_fail_closed():
    assert isinstance(classify_ibkr_error(10089), MarketDataNotEntitledError)
    assert isinstance(classify_ibkr_error(200), NotTradableError)
    assert isinstance(classify_ibkr_error(321), PermanentQualificationError)
    assert isinstance(classify_ibkr_error(1100), ConnectionUnavailableError)
    assert isinstance(classify_ibkr_error(100), RetryableQualificationError)
    # unknown code is conservatively retryable (never silently permanent)
    assert isinstance(classify_ibkr_error(99999), RetryableQualificationError)
    assert isinstance(classify_ibkr_error(None), RetryableQualificationError)
