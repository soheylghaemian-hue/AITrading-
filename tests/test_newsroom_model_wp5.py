"""§ WP5 unit — the news/filings model, dedup/cluster keys, fail-closed time & mapping, stub provider (pure).

SAFETY: research/reference data only — no trading path.
"""
from __future__ import annotations

from atp.newsroom.model import (
    MappingStatus,
    NewsMessage,
    TimeStatus,
    TranslationStatus,
    classify_time_status,
    cluster_key,
    content_checksum,
    message_id_for,
    normalize_title,
    resolve_mapping_status,
)
from atp.newsroom.provider import (
    LicenseMetadata,
    NewsProviderRateLimitedError,
    NewsProviderUnavailableError,
    ProviderNewsItem,
    StubNewsProvider,
)

P = "2026-09-02T10:00:00+00:00"


# --------------------------------------------------------------------- checksum / cluster / id
def test_content_checksum_is_provider_neutral_and_sensitive():
    a = content_checksum(original_title="T", original_body="B", original_language="en", published_at=P)
    same = content_checksum(original_title="T", original_body="B", original_language="en", published_at=P)
    diff = content_checksum(original_title="T", original_body="B2", original_language="en", published_at=P)
    assert a == same and a != diff and a.startswith("sha256:")


def test_cluster_key_groups_syndication_but_not_different_titles():
    k1 = cluster_key(original_title="Microsoft beats earnings!", published_at=P)
    k2 = cluster_key(original_title="microsoft  beats   earnings", published_at=P)   # normalized same
    k3 = cluster_key(original_title="Apple launches product", published_at=P)
    assert k1 == k2 and k1 != k3
    assert normalize_title("Microsoft BEATS earnings!!") == "microsoft beats earnings"


def test_message_id_is_deterministic_per_provider_message():
    assert message_id_for("AGG", "m1") == message_id_for("AGG", "m1")
    assert message_id_for("AGG", "m1") != message_id_for("OTHER", "m1")
    assert message_id_for("AGG", "m1").startswith("NM-")


# --------------------------------------------------------------------- fail-closed mapping
def test_resolve_mapping_status_fail_closed():
    assert resolve_mapping_status(match_count=0, by_stable_id=True) is MappingStatus.UNMAPPED
    assert resolve_mapping_status(match_count=1, by_stable_id=True) is MappingStatus.VERIFIED
    assert resolve_mapping_status(match_count=2, by_stable_id=True) is MappingStatus.AMBIGUOUS
    # a symbol alone can NEVER be VERIFIED, even with a single match
    assert resolve_mapping_status(match_count=1, by_stable_id=False) is MappingStatus.AMBIGUOUS
    assert resolve_mapping_status(match_count=0, by_stable_id=False) is MappingStatus.UNMAPPED


# --------------------------------------------------------------------- fail-closed time integrity
def test_classify_time_status():
    assert classify_time_status(P, "2026-09-02T12:00:00+00:00") is TimeStatus.OK
    assert classify_time_status(None, "2026-09-02T12:00:00+00:00") is TimeStatus.MISSING_PUBLISH
    assert classify_time_status("2026-09-02T10:00:00", "2026-09-02T12:00:00+00:00") is TimeStatus.MISSING_PUBLISH  # naive
    assert classify_time_status("2999-01-01T00:00:00+00:00", "2026-09-02T12:00:00+00:00") is TimeStatus.FUTURE_CONFLICT


def test_news_message_time_status_is_fail_closed_by_construction():
    """Regression: time_status is DERIVED from the timestamps, never a stored value — a NULL publish time can
    never masquerade as OK, even for a NewsMessage built outside the ingest pipeline."""
    rx = "2026-09-02T12:00:00+00:00"
    assert NewsMessage("P", "1", "s", published_at=None, received_at=rx).time_status == TimeStatus.MISSING_PUBLISH.value
    assert NewsMessage("P", "1", "s", published_at=P, received_at=rx).time_status == TimeStatus.OK.value
    assert NewsMessage("P", "1", "s", published_at="2999-01-01T00:00:00+00:00",
                       received_at=rx).time_status == TimeStatus.FUTURE_CONFLICT.value
    # the derived value is what gets persisted
    assert NewsMessage("P", "1", "s", published_at=None, received_at=rx).as_record()["time_status"] \
        == TimeStatus.MISSING_PUBLISH.value


# --------------------------------------------------------------------- model integrity
def test_message_keeps_translation_separate_from_original():
    m = NewsMessage(provider="AGG", provider_id="m1", source_id="s", original_title="Gewinn steigt",
                    original_body="Originaltext", original_language="de",
                    translated_title="Profit rises", translated_summary="EN summary",
                    translation_status=TranslationStatus.TRANSLATED.value, translation_source="engine")
    rec = m.as_record()
    assert rec["original_title"] == "Gewinn steigt" and rec["original_body"] == "Originaltext"
    assert rec["translated_title"] == "Profit rises" and rec["translated_summary"] == "EN summary"
    assert rec["original_title"] != rec["translated_title"]      # translation never overwrites the original
    assert rec["message_id"] == m.message_id and rec["content_checksum"] == m.content_checksum
    for col in ("published_at", "received_at", "license_status", "storage_status", "time_status",
                "correction_of_id", "retraction_of_id", "duplicate_of_id", "cluster_id"):
        assert col in rec


def test_message_defaults_are_fail_closed():
    m = NewsMessage(provider="AGG", provider_id="x", source_id="s", original_title="t")
    assert m.as_record()["event_category"] == "UNCLASSIFIED"     # never a fabricated category
    assert m.as_record()["relevance"] == "UNKNOWN" and m.as_record()["impact_estimate"] == "UNKNOWN"


# --------------------------------------------------------------------- stub provider
def test_stub_provider_paginates_and_reports_license_and_status():
    items0 = (ProviderNewsItem(provider_id="a", title="A"),)
    items1 = (ProviderNewsItem(provider_id="b", title="B"),)
    prov = StubNewsProvider(name="AGG", source_id="s", pages=[list(items0), list(items1)],
                            license=LicenseMetadata(storage_allowed=True))
    assert prov.configured and "fetch_new" in prov.capabilities()
    assert prov.license_metadata().storage_allowed is True
    assert prov.provider_status().available is True
    p0 = prov.fetch_new(cursor=None)
    assert p0.items[0].provider_id == "a" and p0.next_cursor == "1"
    p1 = prov.fetch_new(cursor="1")
    assert p1.items[0].provider_id == "b" and p1.next_cursor is None


def test_stub_provider_error_paths():
    down = StubNewsProvider(name="X", source_id="s", unavailable=True)
    try:
        down.fetch_new()
        raise AssertionError("expected unavailable")
    except NewsProviderUnavailableError:
        pass
    rl = StubNewsProvider(name="X", source_id="s", rate_limited=True)
    try:
        rl.fetch_new()
        raise AssertionError("expected rate limited")
    except NewsProviderRateLimitedError:
        pass


def test_provider_interface_has_no_trading_methods():
    from atp.newsroom.provider import NewsProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account",
                 "subscribe", "purchase", "translate", "reqMktData"}
    assert forbidden.isdisjoint(set(dir(NewsProvider)))
