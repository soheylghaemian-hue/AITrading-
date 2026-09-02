"""§ WP6 unit — the macro/geopolitical event overlay model, fail-closed link/scope helpers, macro checksum &
cluster keys, stub provider (pure, no store/network).

SAFETY: research/reference data only — no trading path.
"""
from __future__ import annotations

from atp.macroevents import (
    AssetClassScope,
    GeoScope,
    LinkStatus,
    MacroEvent,
    MacroEventType,
    MacroSourceClass,
    MacroSourceEntry,
    link_status_from_mappings,
    macro_checksum,
    macro_cluster_id_for,
    macro_cluster_key,
    normalize_asset_classes,
    normalize_scope_list,
    resolve_link_status,
)
from atp.macroevents.model import MappingStatus
from atp.macroevents.provider import (
    MacroEventItem,
    MacroEventProvider,
    NewsProviderRateLimitedError,
    NewsProviderUnavailableError,
    StubMacroEventProvider,
)

P = "2026-09-02T10:00:00+00:00"


# --------------------------------------------------------------------- fail-closed defaults
def test_enum_fail_closed_defaults():
    assert MacroEventType.UNCLASSIFIED.value == "UNCLASSIFIED"
    assert MacroSourceClass.OTHER.value == "OTHER"
    assert GeoScope.UNKNOWN.value == "UNKNOWN"          # never widened to GLOBAL
    assert LinkStatus.NONE.value == "NONE"
    # a macro event built with only a message id is fully fail-closed
    m = MacroEvent(message_id="NM-x")
    r = m.as_record()
    assert r["macro_type"] == "UNCLASSIFIED" and r["source_class"] == "OTHER"
    assert r["geo_scope"] == "UNKNOWN" and r["severity"] == "UNKNOWN" and r["link_status"] == "NONE"
    assert r["affected_regions_json"] == "[]" and r["affected_asset_classes_json"] == "[]"


# --------------------------------------------------------------------- normalize helpers
def test_normalize_scope_list_upper_dedup_sorted_drops_empty():
    assert normalize_scope_list(["us", "US", " eu ", "", None]) == ("EU", "US")
    assert normalize_scope_list(()) == ()


def test_normalize_asset_classes_is_fail_closed():
    assert normalize_asset_classes(["rates", "FX"]) == ("FX", "RATES")
    # an unrecognized token is coerced to UNKNOWN, never dropped silently nor invented
    assert normalize_asset_classes(["rates", "banana"]) == ("RATES", "UNKNOWN")
    assert AssetClassScope.UNKNOWN.value == "UNKNOWN"


def test_normalize_accepts_enum_members_without_mangling():
    """Regression: the fields accept `AssetClassScope | str` — a `(str, Enum)` member's str() is the dotted
    repr, so it must be reduced to its `.value` (not mangled to a lost UNKNOWN / a 'GEOSCOPE.X' token)."""
    assert normalize_asset_classes((AssetClassScope.ENERGY, AssetClassScope.FX)) == ("ENERGY", "FX")
    assert normalize_asset_classes((AssetClassScope.ENERGY, "rates", "banana")) == ("ENERGY", "RATES", "UNKNOWN")
    assert normalize_scope_list((GeoScope.COUNTRY, "us")) == ("COUNTRY", "US")


# --------------------------------------------------------------------- fail-closed link status
def test_resolve_link_status_fail_closed():
    assert resolve_link_status(had_hints=False, match_count=0, by_stable_id=False) is LinkStatus.NONE
    assert resolve_link_status(had_hints=True, match_count=0, by_stable_id=True) is LinkStatus.UNMAPPED
    assert resolve_link_status(had_hints=True, match_count=1, by_stable_id=True) is LinkStatus.VERIFIED
    assert resolve_link_status(had_hints=True, match_count=2, by_stable_id=True) is LinkStatus.AMBIGUOUS
    # a symbol-only hint can NEVER be VERIFIED, even with a single match
    assert resolve_link_status(had_hints=True, match_count=1, by_stable_id=False) is LinkStatus.AMBIGUOUS


def test_link_status_from_mappings():
    V, A = MappingStatus.VERIFIED.value, MappingStatus.AMBIGUOUS.value
    assert link_status_from_mappings([], had_hints=False) is LinkStatus.NONE
    assert link_status_from_mappings([], had_hints=True) is LinkStatus.UNMAPPED
    assert link_status_from_mappings([V, V], had_hints=True) is LinkStatus.VERIFIED
    assert link_status_from_mappings([V, A], had_hints=True) is LinkStatus.AMBIGUOUS


# --------------------------------------------------------------------- checksum / cluster
def test_macro_checksum_is_deterministic_and_sensitive():
    kw = {"macro_type": "SANCTION", "source_class": "SANCTIONS_AUTHORITY", "geo_scope": "BLOC",
          "policy_area": "SANCTIONS", "regions": ("EUROPE",), "countries": ("DE",), "blocs": ("EU",),
          "asset_classes": ("ENERGY",), "published_at": P}
    a = macro_checksum(**kw)
    assert a == macro_checksum(**kw) and a.startswith("sha256:")
    assert a != macro_checksum(**{**kw, "geo_scope": "GLOBAL"})
    # normalization: scope order/case does not change identity
    assert a == macro_checksum(**{**kw, "regions": ("europe",)})
    # policy_area (free text) is case-stable, consistent with macro_cluster_key
    assert a == macro_checksum(**{**kw, "policy_area": "sanctions"})


def test_macro_cluster_groups_same_situation():
    k1 = macro_cluster_key(macro_type="MONETARY_POLICY_DECISION", primary_region="AMERICAS",
                           policy_area="MONETARY", published_at=P)
    k2 = macro_cluster_key(macro_type="MONETARY_POLICY_DECISION", primary_region="americas",
                           policy_area="monetary", published_at="2026-09-02T18:00:00+00:00")   # same day
    k3 = macro_cluster_key(macro_type="SANCTION", primary_region="EUROPE",
                           policy_area="SANCTIONS", published_at=P)
    assert macro_cluster_id_for(k1) == macro_cluster_id_for(k2)          # same situation, same day
    assert macro_cluster_id_for(k1) != macro_cluster_id_for(k3)
    assert macro_cluster_id_for(k1).startswith("MG-")


def test_macro_event_as_record_and_primary_region():
    m = MacroEvent(message_id="NM-1", macro_type=MacroEventType.SANCTION.value,
                   source_class=MacroSourceClass.SANCTIONS_AUTHORITY.value, geo_scope=GeoScope.BLOC.value,
                   affected_regions=("europe", "EUROPE"), affected_countries=("DE",), affected_blocs=("EU",),
                   affected_asset_classes=("energy", "junk"), published_at=P)
    r = m.as_record()
    assert r["affected_regions_json"] == '["EUROPE"]'          # deduped + upper
    assert r["affected_asset_classes_json"] == '["ENERGY", "UNKNOWN"]'   # fail-closed unknown
    assert r["macro_checksum"].startswith("sha256:") and r["macro_cluster_id"].startswith("MG-")
    assert m.primary_region == "EUROPE"


# --------------------------------------------------------------------- registry fail-closed
def test_macro_source_entry_is_fail_closed():
    e = MacroSourceEntry("ecb", "European Central Bank", source_class=MacroSourceClass.CENTRAL_BANK.value)
    r = e.as_record()
    assert r["available"] is False and r["license_status"] == "UNKNOWN"
    assert r["storage_allowed"] is False and r["redistribution_allowed"] is False
    assert r["commercial_use_allowed"] is False and r["attribution_required"] is True


# --------------------------------------------------------------------- read-only stub provider
def test_stub_macro_provider_paginates_and_is_honest():
    prov = StubMacroEventProvider(pages=[[MacroEventItem(provider_id="a")], [MacroEventItem(provider_id="b")]])
    p0 = prov.fetch_new(cursor=None)
    assert len(p0.items) == 1 and p0.next_cursor == "1"
    p1 = prov.fetch_new(cursor="1")
    assert p1.next_cursor is None
    assert prov.license_metadata().license_status.value == "UNKNOWN"     # honest: unlicensed by default
    assert prov.provider_status().available is True


def test_stub_macro_provider_signals_outage_and_rate_limit():
    import pytest
    with pytest.raises(NewsProviderUnavailableError):
        StubMacroEventProvider(unavailable=True).fetch_new()
    with pytest.raises(NewsProviderRateLimitedError):
        StubMacroEventProvider(rate_limited=True).fetch_new()


def test_provider_abc_defines_no_trading_surface():
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account"}
    assert forbidden.isdisjoint(set(dir(MacroEventProvider)))
    assert set(dir(MacroEventProvider)) >= {"fetch_new", "license_metadata", "provider_status", "configured"}
