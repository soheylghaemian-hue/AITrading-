"""§ WP9 — coverage read-model: dimensions + the explicit four-stage funnel, read-only.

Verifies coverage is reported by region/country/exchange/asset-class/source/qualification-status, that the
'source connected → imported → IBKR-verified → tradable' stages are distinct, and that a partial universe is
never presented as complete.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from atp.core.enums import AssetClass
from atp.instruments.coverage import instrument_coverage, source_coverage
from atp.instruments.model import InstrumentRecord
from atp.instruments.sources import seed_sources
from atp.store import open_store


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _rec(symbol, exchange, ccy, region, country, ac=AssetClass.EQUITY, source="FIRDS"):
    return InstrumentRecord(symbol=symbol, asset_class=ac, exchange=exchange, trading_currency=ccy,
                            region=region, country=country, source=source)


def test_source_coverage_reports_available_missing_blocked():
    cov = source_coverage(seed_sources())
    assert cov["available_sources"] == []                    # fail-closed: nothing active by default
    assert "esma_firds" in cov["missing_sources"]
    assert set(cov["blocked_sources"]) >= {"jpx_listed_issues", "asx_listed", "hkex_listed", "sgx_listed"}
    assert cov["coverage_partial"] is True                   # never claim full coverage from an empty set
    assert cov["licenses"]["sec_company_tickers"]["license_status"] == "public_domain"
    assert cov["by_region"]["EUROPE"]["available"] == 0 and cov["by_region"]["EUROPE"]["declared"] >= 1


def test_instrument_coverage_dimensions_and_funnel():
    store = _store()
    store.im_upsert_instrument(_rec("FR0000131104", "XPAR", "EUR", "EUROPE", "FR").as_record())
    store.im_upsert_instrument(_rec("DE0007164600", "XETR", "EUR", "EUROPE", "DE").as_record())
    store.im_upsert_instrument(_rec("AAPL", "NASDAQ", "USD", "AMERICAS", "US",
                                    ac=AssetClass.EQUITY, source="SEC company_tickers_exchange").as_record())

    cov = instrument_coverage(store)
    inst = cov["instruments"]
    assert inst["total"] == 3
    assert inst["by_region"] == {"AMERICAS": 1, "EUROPE": 2}
    assert inst["by_country"] == {"DE": 1, "FR": 1, "US": 1}
    assert inst["by_exchange"] == {"NASDAQ": 1, "XETR": 1, "XPAR": 1}
    assert inst["by_asset_class"] == {"equity": 3}
    assert set(inst["by_source"]) == {"FIRDS", "SEC company_tickers_exchange"}
    assert inst["by_qualification_status"] == {"DISCOVERED": 3}       # imported, not yet IBKR-verified

    funnel = cov["funnel"]
    assert funnel["sources_connected"] == 0        # 'Quelle angebunden' — none activated
    assert funnel["imported"] == 3                 # 'Instrument importiert'
    assert funnel["ibkr_verified"] == 0            # 'IBKR-verifiziert' — distinct stage
    assert funnel["tradable"] == 0                 # 'handelbar' — the strongest claim
    assert cov["coverage_partial"] is True         # partial ≠ complete


def test_activated_blocked_source_stays_blocked_in_coverage():
    # a BLOCKED source that an operator flips to available=True must still be reported BLOCKED, and the
    # universe must still read as partial — never presented as covered.
    import dataclasses

    from atp.instruments.sources import source_by_id
    srcs = [dataclasses.replace(source_by_id("jpx_listed_issues"), available=True)]
    cov = source_coverage(srcs)
    assert cov["available_sources"] == [] and cov["blocked_sources"] == ["jpx_listed_issues"]
    assert cov["coverage_partial"] is True


def test_null_dimensions_reported_as_unknown_not_dropped():
    store = _store()
    # an instrument whose region/country were unknown at listing stage (NO DATA) must still be counted
    store.im_upsert_instrument(
        InstrumentRecord(symbol="X", asset_class=AssetClass.BOND, exchange="XZZZ", trading_currency="EUR",
                         region=None, country=None, source="FIRDS").as_record())
    cov = instrument_coverage(store)
    assert cov["instruments"]["by_region"] == {"UNKNOWN": 1}
    assert cov["instruments"]["by_country"] == {"UNKNOWN": 1}
