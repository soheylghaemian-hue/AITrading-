"""§ WP7 unit — the fundamentals/macro observation model, fail-closed value/link helpers, content checksum &
ids, stub provider (pure, no store/network).

SAFETY: research/reference data only — no trading path.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from atp.fundseries import (
    Frequency,
    FundamentalCategory,
    FundamentalObservation,
    FundamentalSeries,
    FundamentalSourceEntry,
    LinkStatus,
    SourceType,
    Unit,
    ValueStatus,
    classify_value_status,
    content_checksum,
    link_status_from_mappings,
    normalize_value,
    observation_id_for,
    resolve_link_status,
    series_id_for,
)
from atp.fundseries.model import MappingStatus, normalize_token
from atp.fundseries.provider import (
    FundamentalItem,
    FundamentalProvider,
    NewsProviderRateLimitedError,
    NewsProviderUnavailableError,
    StubFundamentalProvider,
)

P = "2026-09-01T12:00:00+00:00"


# --------------------------------------------------------------------- fail-closed defaults
def test_enum_fail_closed_defaults():
    assert FundamentalCategory.UNCLASSIFIED.value == "UNCLASSIFIED"
    assert SourceType.OTHER.value == "OTHER"
    assert Unit.UNKNOWN.value == "UNKNOWN" and Frequency.UNKNOWN.value == "UNKNOWN"
    assert LinkStatus.NONE.value == "NONE"
    s = FundamentalSeries(source_id="s", series_key="k").as_record()
    assert s["category"] == "UNCLASSIFIED" and s["unit"] == "UNKNOWN" and s["frequency"] == "UNKNOWN"
    assert s["link_status"] == "NONE"


# --------------------------------------------------------------------- numeric value normalization
def test_normalize_value_canonical_and_fail_closed():
    assert normalize_value(3.20) == "3.2" and normalize_value("3.2") == "3.2"     # equal values → one token
    assert normalize_value(Decimal("100.00")) == "100" and normalize_value(0) == "0"
    assert normalize_value(-1.5) == "-1.5" and normalize_value(95000000000) == "95000000000"
    # fail-closed: missing/None/non-numeric/non-finite never fabricated
    assert normalize_value(None) is None
    assert normalize_value("n/a") is None and normalize_value("") is None
    assert normalize_value(float("nan")) is None and normalize_value(float("inf")) is None
    assert normalize_value(True) is None and normalize_value(False) is None       # bool is never a value


def test_classify_value_status():
    assert classify_value_status("3.2", None) is ValueStatus.OK
    assert classify_value_status(None, "AA+") is ValueStatus.NON_NUMERIC          # rating in value_text
    assert classify_value_status(None, None) is ValueStatus.MISSING              # never invented


def test_normalize_token_is_enum_safe():
    assert normalize_token("us") == "US"
    assert normalize_token(Unit.PERCENT) == "PERCENT"     # enum reduced to value, not the dotted repr


# --------------------------------------------------------------------- fail-closed link status
def test_resolve_link_status_fail_closed():
    assert resolve_link_status(had_hints=False, match_count=0, by_stable_id=False) is LinkStatus.NONE
    assert resolve_link_status(had_hints=True, match_count=0, by_stable_id=True) is LinkStatus.UNMAPPED
    assert resolve_link_status(had_hints=True, match_count=1, by_stable_id=True) is LinkStatus.VERIFIED
    assert resolve_link_status(had_hints=True, match_count=2, by_stable_id=True) is LinkStatus.AMBIGUOUS
    assert resolve_link_status(had_hints=True, match_count=1, by_stable_id=False) is LinkStatus.AMBIGUOUS


def test_link_status_from_mappings():
    V, A = MappingStatus.VERIFIED.value, MappingStatus.AMBIGUOUS.value
    assert link_status_from_mappings([], had_hints=False) is LinkStatus.NONE
    assert link_status_from_mappings([], had_hints=True) is LinkStatus.UNMAPPED
    assert link_status_from_mappings([V, V], had_hints=True) is LinkStatus.VERIFIED
    assert link_status_from_mappings([V, A], had_hints=True) is LinkStatus.AMBIGUOUS


# --------------------------------------------------------------------- checksum / ids
def test_content_checksum_distinguishes_revision_and_value():
    base = {"series_id": "FS-x", "period": "2026-08", "value": "3.2", "value_text": None,
            "revision_seq": 0, "published_at": P}
    a = content_checksum(**base)
    assert a == content_checksum(**base) and a.startswith("sha256:")
    assert a != content_checksum(**{**base, "value": "3.3"})           # different value → not a duplicate
    assert a != content_checksum(**{**base, "revision_seq": 1})        # a revision → not a duplicate
    assert a != content_checksum(**{**base, "period": "2026-07"})


def test_ids_are_deterministic():
    assert observation_id_for("BLS", "o1") == observation_id_for("BLS", "o1")
    assert observation_id_for("BLS", "o1") != observation_id_for("BEA", "o1")
    assert observation_id_for("BLS", "o1").startswith("FO-")
    assert series_id_for("us_bls", "CPI") == series_id_for("us_bls", "CPI")
    assert series_id_for("us_bls", "CPI") != series_id_for("eurostat", "CPI")
    assert series_id_for("us_bls", "CPI").startswith("FS-")


# --------------------------------------------------------------------- observation record
def test_observation_as_record_value_status_and_checksum_independent_of_gate():
    # a metadata-only (license-gated) observation still fingerprints its FETCHED value → dedup is gate-independent
    stored = FundamentalObservation(series_id="FS-x", provider="P", provider_id="o", source_id="s",
                                    period="2026-08", value=None, fetched_value="3.2", published_at=P)
    licensed = FundamentalObservation(series_id="FS-x", provider="P", provider_id="o", source_id="s",
                                      period="2026-08", value="3.2", fetched_value="3.2", published_at=P)
    assert stored.content_checksum == licensed.content_checksum        # identity independent of storage
    r = stored.as_record()
    assert r["value"] is None and r["value_status"] == "MISSING"       # not stored, and not fabricated
    assert licensed.as_record()["value_status"] == "OK"


def test_series_record_normalizes_dimensions():
    r = FundamentalSeries(source_id="s", series_key="k", region="americas", country="us",
                          currency="usd").as_record()
    assert r["region"] == "AMERICAS" and r["country"] == "US" and r["currency"] == "USD"


# --------------------------------------------------------------------- registry fail-closed
def test_source_entry_is_fail_closed():
    r = FundamentalSourceEntry("us_bls", "US BLS", source_type=SourceType.STATISTICS_OFFICE.value).as_record()
    assert r["available"] is False and r["license_status"] == "UNKNOWN"
    assert r["storage_allowed"] is False and r["redistribution_allowed"] is False
    assert r["commercial_use_allowed"] is False and r["attribution_required"] is True


# --------------------------------------------------------------------- read-only stub provider
def test_stub_provider_paginates_and_is_honest():
    prov = StubFundamentalProvider(pages=[[FundamentalItem(series_key="k", provider_id="a")],
                                          [FundamentalItem(series_key="k", provider_id="b")]])
    p0 = prov.fetch_new(cursor=None)
    assert len(p0.items) == 1 and p0.next_cursor == "1"
    assert prov.fetch_new(cursor="1").next_cursor is None
    assert prov.license_metadata().license_status.value == "UNKNOWN"     # honest: unlicensed by default


def test_stub_provider_signals_outage_and_rate_limit():
    with pytest.raises(NewsProviderUnavailableError):
        StubFundamentalProvider(unavailable=True).fetch_new()
    with pytest.raises(NewsProviderRateLimitedError):
        StubFundamentalProvider(rate_limited=True).fetch_new()


def test_provider_abc_defines_no_trading_surface():
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account"}
    assert forbidden.isdisjoint(set(dir(FundamentalProvider)))
    assert set(dir(FundamentalProvider)) >= {"fetch_new", "license_metadata", "provider_status", "configured"}
