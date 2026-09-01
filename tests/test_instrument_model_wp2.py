"""§ WP2 unit — the unified instrument reference model (pure, no store).

Proves the identity/collision/mapping/no-fabrication contract of `atp.instruments.model` and the request
checksum of `atp.instruments.importer` deterministically. SAFETY: reference data only — no trading path.
"""
from __future__ import annotations

from atp.core.enums import AssetClass
from atp.instruments.importer import MarketPlan, import_request_checksum, record_from_listing
from atp.instruments.listing_sources import ListingCandidate
from atp.instruments.model import (
    InstrumentRecord,
    MarketDataStatus,
    SourceStatus,
    TradabilityStatus,
    VerificationStatus,
    canon_decimal_text,
    instrument_id_for,
    instrument_natural_key,
    sec_type_to_asset_class,
)

US = MarketPlan(market_id="US", region="AMERICAS", country="US",
                timezone="America/New_York", calendar="us_equity", default_currency="USD")


# --------------------------------------------------------------------- natural key / stable id
def test_same_symbol_different_exchange_is_a_distinct_instrument():
    """Symbol-collision protection: the same ticker on two venues must never collapse into one row."""
    a = instrument_natural_key(asset_class=AssetClass.EQUITY, symbol="AAPL", exchange="NASDAQ", currency="USD")
    b = instrument_natural_key(asset_class=AssetClass.EQUITY, symbol="AAPL", exchange="XLON", currency="GBP")
    assert a != b
    assert instrument_id_for(a) != instrument_id_for(b)


def test_natural_key_and_id_are_deterministic_and_idempotent():
    kwargs = dict(asset_class=AssetClass.EQUITY, symbol="msft", exchange="nasdaq", currency="usd")
    k1 = instrument_natural_key(**kwargs)
    k2 = instrument_natural_key(**kwargs)
    assert k1 == k2
    assert instrument_id_for(k1) == instrument_id_for(k2)
    # Case/whitespace-insensitive on the identity fields → one canonical instrument.
    k3 = instrument_natural_key(asset_class=AssetClass.EQUITY, symbol=" MSFT ", exchange="NASDAQ", currency="USD")
    assert k1 == k3


def test_derivative_identity_includes_expiry_strike_right():
    call = instrument_natural_key(asset_class=AssetClass.OPTION, symbol="AAPL", exchange="CBOE",
                                  currency="USD", expiry="20260320", strike="200", option_right="C")
    put = instrument_natural_key(asset_class=AssetClass.OPTION, symbol="AAPL", exchange="CBOE",
                                 currency="USD", expiry="20260320", strike="200", option_right="P")
    other_strike = instrument_natural_key(asset_class=AssetClass.OPTION, symbol="AAPL", exchange="CBOE",
                                          currency="USD", expiry="20260320", strike="210", option_right="C")
    assert call != put != other_strike and call != other_strike


def test_strike_canonicalization_is_stable():
    assert canon_decimal_text("200") == canon_decimal_text("200.00") == "200"
    assert canon_decimal_text(0) == "0"
    assert canon_decimal_text(None) is None
    assert canon_decimal_text("") is None
    # equal strikes written differently → identical natural key
    k1 = instrument_natural_key(asset_class=AssetClass.OPTION, symbol="X", exchange="E", currency="USD",
                                expiry="20260320", strike="200.0", option_right="C")
    k2 = instrument_natural_key(asset_class=AssetClass.OPTION, symbol="X", exchange="E", currency="USD",
                                expiry="20260320", strike="200", option_right="C")
    assert k1 == k2


# --------------------------------------------------------------------- checksum / change detection
def test_content_checksum_stable_and_sensitive():
    base = InstrumentRecord(symbol="AAPL", asset_class=AssetClass.EQUITY, exchange="NASDAQ")
    same = InstrumentRecord(symbol="AAPL", asset_class=AssetClass.EQUITY, exchange="NASDAQ")
    changed = InstrumentRecord(symbol="AAPL", asset_class=AssetClass.EQUITY, exchange="NASDAQ",
                               description="Apple Inc.")
    assert base.content_checksum == same.content_checksum
    assert base.content_checksum != changed.content_checksum
    # identity is preserved across a descriptive change (same instrument, new content)
    assert base.instrument_id == changed.instrument_id


def test_as_record_carries_identity_and_all_columns():
    rec = InstrumentRecord(symbol="AAPL", asset_class=AssetClass.EQUITY, exchange="NASDAQ")
    row = rec.as_record()
    assert row["instrument_id"] == rec.instrument_id
    assert row["natural_key"] == rec.natural_key
    assert row["content_checksum"] == rec.content_checksum
    assert row["asset_class"] == "equity"
    for col in ("con_id", "isin", "figi", "cusip", "sedol", "settlement_currency", "tick_size"):
        assert col in row


# --------------------------------------------------------------------- listing mapping / no fabrication
def test_sec_type_mapping():
    assert sec_type_to_asset_class("STK") is AssetClass.EQUITY
    assert sec_type_to_asset_class("etf") is AssetClass.ETF
    assert sec_type_to_asset_class("FUT") is AssetClass.FUTURE
    assert sec_type_to_asset_class("NONSENSE") is None
    assert sec_type_to_asset_class(None) is None


def test_record_from_listing_never_fabricates_unknowns():
    cand = ListingCandidate(symbol="MSFT", sec_type="STK", exchange="NASDAQ", currency="USD",
                            description="Microsoft Corp", lot_size=100.0, source="NASDAQ Trader")
    rec = record_from_listing(cand, US)
    assert rec is not None
    assert rec.asset_class is AssetClass.EQUITY and rec.sub_class == "common_stock"
    # venue facts are real, taken from the market plan
    assert rec.region == "AMERICAS" and rec.country == "US"
    assert rec.timezone == "America/New_York" and rec.trading_calendar == "us_equity"
    assert rec.trading_currency == "USD" and rec.multiplier == "1" and rec.lot_size == "100"
    # governance defaults are fail-closed / discovered-only
    assert rec.verification_status == VerificationStatus.UNVERIFIED.value
    assert rec.tradability_status == TradabilityStatus.UNKNOWN.value
    assert rec.market_data_status == MarketDataStatus.UNKNOWN.value
    assert rec.source_status == SourceStatus.DISCOVERED.value
    # identifiers absent from a listing file are NO DATA — never invented
    assert rec.con_id is None and rec.isin is None and rec.figi is None
    assert rec.cusip is None and rec.sedol is None and rec.settlement_currency is None
    assert rec.tick_size is None and rec.last_verified_at is None


def test_record_from_listing_skips_unmappable_sec_type():
    cand = ListingCandidate(symbol="???", sec_type="WEIRD", exchange="NASDAQ", currency="USD",
                            description="unknown")
    assert record_from_listing(cand, US) is None


def test_etf_multiplier_is_unit_but_derivative_multiplier_unknown():
    etf = record_from_listing(ListingCandidate(symbol="SPY", sec_type="ETF", exchange="ARCA",
                                               currency="USD", description="SPDR"), US)
    assert etf.multiplier == "1"


# --------------------------------------------------------------------- request checksum
def test_import_request_checksum_is_order_independent_and_deterministic():
    a = import_request_checksum("src", ["US", "EU", "APAC"])
    b = import_request_checksum("src", ["APAC", "US", "EU"])
    c = import_request_checksum("src", ["US", "EU"])
    d = import_request_checksum("other", ["US", "EU", "APAC"])
    assert a == b            # market order does not change request identity
    assert a != c            # a different market set is a different request
    assert a != d            # a different source label is a different request
    assert a.startswith("sha256:")
