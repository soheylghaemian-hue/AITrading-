"""WP5 — the seed source registry.

Declares the source CLASSES the platform intends to cover (company IR, exchange notices, the major official
filing systems worldwide, central banks, regulators, licensed aggregators) with their regions, languages,
source type, primacy and — importantly — a fail-closed default: every seed source is ``available=False`` and
``license_status=UNKNOWN`` until a real, entitled provider is actually attached and probed. Declaring a source
class does NOT claim coverage of it; the read-model reports active vs missing sources explicitly.

SAFETY: reference/registry data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

from .model import LicenseStatus, Primacy, SourceRegistryEntry, SourceType


def _src(source_id, name, source_type, primacy, regions, languages, update_mode="poll") -> SourceRegistryEntry:
    # fail-closed: unavailable + license UNKNOWN + storage/redistribution/commercial NOT allowed until proven.
    return SourceRegistryEntry(
        source_id=source_id, name=name, source_type=source_type.value, primacy=primacy.value,
        regions=tuple(regions), languages=tuple(languages), update_mode=update_mode,
        license_status=LicenseStatus.UNKNOWN.value, storage_allowed=False, redistribution_allowed=False,
        commercial_use_allowed=False, attribution_required=True, available=False)


def seed_sources() -> list[SourceRegistryEntry]:
    """The declared source classes (§2). All fail-closed until a real entitled provider is attached."""
    P, S = Primacy.PRIMARY, Primacy.SECONDARY
    return [
        _src("company_ir", "Company websites / investor relations", SourceType.COMPANY_IR, P,
             ["GLOBAL"], ["mul"]),
        _src("exchange_notices", "Exchange notices / ad-hoc disclosures", SourceType.EXCHANGE_NOTICE, P,
             ["GLOBAL"], ["mul"]),
        _src("sec_edgar", "SEC EDGAR", SourceType.REGULATORY_FILING, P, ["AMERICAS", "US"], ["en"]),
        _src("rns_uk", "RNS (Regulatory News Service, UK)", SourceType.REGULATORY_FILING, P, ["EUROPE", "GB"], ["en"]),
        _src("sedar_plus_ca", "SEDAR+ (Canada)", SourceType.REGULATORY_FILING, P, ["AMERICAS", "CA"], ["en", "fr"]),
        _src("tdnet_jp", "TDnet (Japan)", SourceType.EXCHANGE_NOTICE, P, ["APAC", "JP"], ["ja", "en"]),
        _src("edinet_jp", "EDINET (Japan)", SourceType.REGULATORY_FILING, P, ["APAC", "JP"], ["ja", "en"]),
        _src("hkexnews", "HKEXnews (Hong Kong)", SourceType.EXCHANGE_NOTICE, P, ["APAC", "HK"], ["zh", "en"]),
        _src("asx_announcements", "ASX announcements (Australia)", SourceType.EXCHANGE_NOTICE, P, ["APAC", "AU"], ["en"]),
        _src("eu_nsm", "European national storage mechanisms (OAMs/NSMs)", SourceType.REGULATORY_FILING, P,
             ["EUROPE"], ["mul"]),
        _src("central_banks", "Central banks", SourceType.CENTRAL_BANK, P, ["GLOBAL"], ["mul"]),
        _src("regulators", "Regulatory authorities", SourceType.REGULATOR, P, ["GLOBAL"], ["mul"]),
        _src("licensed_aggregators", "Licensed news aggregators", SourceType.NEWS_AGGREGATOR, S,
             ["GLOBAL"], ["mul"]),
    ]


def seed_registry(store) -> int:
    """Idempotently upsert the seed source classes into the store's registry. Returns the count."""
    n = 0
    for entry in seed_sources():
        store.nx_upsert_source(entry.as_record())
        n += 1
    return n
