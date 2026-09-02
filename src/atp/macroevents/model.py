"""WP6 — the macro / geopolitical / regulatory event model (a research-data OVERLAY on the WP5 newsroom).

A macro event IS a WP5 ``NewsMessage`` (an immutable, provenance-carrying, deduplicated record) PLUS a thin
structured overlay that adds the macro-specific classification the newsroom model does not carry: the event
sub-type (rate decision, sanction, embargo, conflict, energy/transport warning, …), the source class (central
bank, supranational body, regulator, sanctions/trade authority, …), the geographic SCOPE, and the affected
regions / countries / blocs / asset CLASSES. The overlay never duplicates or mutates the original — dedup,
corrections/retractions and immutability are inherited from the newsroom record it is keyed to.

Nothing here is fabricated. An absent value stays ``None`` / NO DATA; a missing publish time is never
replaced by the receive time (inherited from the newsroom record); severity / impact are RESEARCH METADATA,
never facts or trading signals; instrument/asset-class linkage is FAIL-CLOSED (a broad macro event that
names no instrument is ``NONE``, an inexact match is ``AMBIGUOUS``, never a fabricated ``VERIFIED``).

This module is PURE (no store, no network, no trading). SAFETY: no order/execution/account path.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

# reuse the WP5 newsroom primitives — a macro event is a newsroom record + this overlay
from atp.newsroom.model import Level, LicenseStatus, MappingStatus, normalize_title, utc_ts


class MacroEventType(str, Enum):
    """The macro / geopolitical / regulatory event sub-type. UNCLASSIFIED is the fail-closed default — never
    a fabricated type."""

    MONETARY_POLICY_DECISION = "MONETARY_POLICY_DECISION"   # rate decision / policy rate change
    RATE_GUIDANCE = "RATE_GUIDANCE"                         # forward guidance / minutes
    POLICY_STATEMENT = "POLICY_STATEMENT"                   # central-bank / official statement
    INFLATION_REPORT = "INFLATION_REPORT"
    FX_INTERVENTION = "FX_INTERVENTION"
    LIQUIDITY_OPERATION = "LIQUIDITY_OPERATION"             # facilities / QE / QT operations
    SANCTION = "SANCTION"
    EMBARGO = "EMBARGO"
    EXPORT_CONTROL = "EXPORT_CONTROL"
    TARIFF_MEASURE = "TARIFF_MEASURE"                       # tariff / trade-war measure
    TRADE_AGREEMENT = "TRADE_AGREEMENT"
    ARMED_CONFLICT = "ARMED_CONFLICT"
    CIVIL_UNREST = "CIVIL_UNREST"
    ENERGY_SUPPLY_WARNING = "ENERGY_SUPPLY_WARNING"
    TRANSPORT_DISRUPTION = "TRANSPORT_DISRUPTION"           # shipping / port / air / rail disruption
    REGULATORY_ACTION = "REGULATORY_ACTION"
    SUPRANATIONAL_OUTLOOK = "SUPRANATIONAL_OUTLOOK"         # IMF WEO / World Bank / UN outlook
    SYSTEMIC_RISK_WARNING = "SYSTEMIC_RISK_WARNING"         # BIS / FSB systemic-risk note
    OTHER = "OTHER"
    UNCLASSIFIED = "UNCLASSIFIED"


class MacroSourceClass(str, Enum):
    """The class of macro/geopolitical/regulatory channel. OTHER is the fail-closed default."""

    CENTRAL_BANK = "CENTRAL_BANK"                 # Fed / ECB / BoE / national central banks
    SUPRANATIONAL = "SUPRANATIONAL"               # BIS / IMF / World Bank / UN / FSB
    NATIONAL_REGULATOR = "NATIONAL_REGULATOR"
    SANCTIONS_AUTHORITY = "SANCTIONS_AUTHORITY"   # OFAC / EU / UN sanctions
    TRADE_AUTHORITY = "TRADE_AUTHORITY"           # tariff / export-control bodies
    CONFLICT_MONITOR = "CONFLICT_MONITOR"
    ENERGY_AUTHORITY = "ENERGY_AUTHORITY"
    TRANSPORT_AUTHORITY = "TRANSPORT_AUTHORITY"
    OTHER = "OTHER"


class GeoScope(str, Enum):
    """The geographic scope of the event. UNKNOWN is the fail-closed default (never widened to GLOBAL)."""

    GLOBAL = "GLOBAL"
    BLOC = "BLOC"           # EU / G7 / OPEC / ASEAN …
    REGION = "REGION"
    COUNTRY = "COUNTRY"
    UNKNOWN = "UNKNOWN"


class AssetClassScope(str, Enum):
    """COARSE macro-level asset-class scope (broad, not instrument-level). UNKNOWN is the fail-closed default;
    a value here is a stated scope, never a resolved instrument."""

    EQUITY = "EQUITY"
    RATES = "RATES"
    FX = "FX"
    CREDIT = "CREDIT"
    COMMODITY = "COMMODITY"
    ENERGY = "ENERGY"
    CRYPTO = "CRYPTO"
    MULTI = "MULTI"
    UNKNOWN = "UNKNOWN"


class LinkStatus(str, Enum):
    """Fail-closed summary of a macro event's linkage to catalogue instruments. NONE is the default: a broad
    macro event that names NO instrument is not 'unmapped', it is simply not instrument-specific."""

    VERIFIED = "VERIFIED"        # every stable-id hint resolved to exactly one catalogue instrument
    AMBIGUOUS = "AMBIGUOUS"      # at least one hint was ambiguous / symbol-only
    UNMAPPED = "UNMAPPED"        # hints were given but none matched the catalogue
    NONE = "NONE"               # no instrument hints — the event is not instrument-specific (fail-closed)


_VALID_ASSET_CLASSES = frozenset(a.value for a in AssetClassScope)


# --------------------------------------------------------------------------- helpers
def _s(value) -> str:
    return (value or "").strip()


def _token(v) -> str:
    """Coerce a scope/asset value to its canonical upper token. A ``(str, Enum)`` member's ``str()`` is the
    dotted repr ("AssetClassScope.ENERGY"), so extract ``.value`` FIRST — otherwise an in-contract enum input
    (the field type is ``AssetClassScope | str``) would be silently mangled and lost to UNKNOWN."""
    return str(getattr(v, "value", v)).strip().upper()


def normalize_scope_list(values) -> tuple:
    """Upper-cased, de-duplicated, sorted scope tokens (regions / countries / blocs). None / empty dropped
    (never stringified into a fabricated token); enum members reduced to their value — never fabricates a
    scope."""
    seen = {_token(v) for v in (values or ()) if v is not None and _token(v)}
    return tuple(sorted(seen))


def normalize_asset_classes(values) -> tuple:
    """Coarse asset-class scopes, fail-closed: an unrecognized token becomes UNKNOWN (never dropped silently,
    never invented). None / empty dropped (not coerced to UNKNOWN); enum members reduced to their value.
    De-duplicated + sorted."""
    out = set()
    for v in (values or ()):
        if v is None:
            continue
        tok = _token(v)
        if not tok:
            continue
        out.add(tok if tok in _VALID_ASSET_CLASSES else AssetClassScope.UNKNOWN.value)
    return tuple(sorted(out))


def resolve_link_status(*, had_hints: bool, match_count: int, by_stable_id: bool) -> LinkStatus:
    """Fail-closed instrument-linkage summary. No hints → NONE (not instrument-specific). With hints: 0 matches
    → UNMAPPED; a symbol-only hint is never unique → AMBIGUOUS; a stable id with exactly one match → VERIFIED;
    several matches → AMBIGUOUS. Mirrors the newsroom ``resolve_mapping_status`` invariants."""
    if not had_hints:
        return LinkStatus.NONE
    if match_count == 0:
        return LinkStatus.UNMAPPED
    if not by_stable_id:
        return LinkStatus.AMBIGUOUS
    return LinkStatus.VERIFIED if match_count == 1 else LinkStatus.AMBIGUOUS


def link_status_from_mappings(mapping_statuses, *, had_hints: bool) -> LinkStatus:
    """Summarize the per-instrument mapping statuses (VERIFIED/AMBIGUOUS from the catalogue resolver) into one
    fail-closed link status. Any AMBIGUOUS → AMBIGUOUS; hints but no rows → UNMAPPED; all VERIFIED → VERIFIED;
    no hints → NONE."""
    statuses = list(mapping_statuses)
    if not had_hints:
        return LinkStatus.NONE
    if not statuses:
        return LinkStatus.UNMAPPED
    if any(s == MappingStatus.AMBIGUOUS.value for s in statuses):
        return LinkStatus.AMBIGUOUS
    if all(s == MappingStatus.VERIFIED.value for s in statuses):
        return LinkStatus.VERIFIED
    return LinkStatus.AMBIGUOUS


def macro_checksum(*, macro_type: str, source_class: str, geo_scope: str, policy_area: str | None,
                   regions, countries, blocs, asset_classes, published_at) -> str:
    """Stable identity of the macro OVERLAY classification (independent of the newsroom content checksum). Two
    overlays describing the same macro fact fingerprint identically; used for macro-level integrity, not for
    content dedup (content dedup lives on the newsroom record)."""
    payload = {
        # enum-valued fields are upper by construction; policy_area is free text → upper-cased so the overlay
        # identity is case-stable and consistent with macro_cluster_key's normalization.
        "type": _s(macro_type).upper(), "class": _s(source_class).upper(), "scope": _s(geo_scope).upper(),
        "policy": _s(policy_area).upper(), "regions": list(normalize_scope_list(regions)),
        "countries": list(normalize_scope_list(countries)), "blocs": list(normalize_scope_list(blocs)),
        "assets": list(normalize_asset_classes(asset_classes)), "day": (utc_ts(published_at) or "")[:10],
        "tag": "atp.macro.overlay.v1",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def macro_cluster_key(*, macro_type: str, primary_region: str | None, policy_area: str | None,
                      published_at) -> str:
    """Group related macro events into one 'situation' cluster: same event type + primary region + policy area
    + publish DAY. Soft read-time grouping metadata, never a dedup/audit decision."""
    day = (utc_ts(published_at) or "")[:10]
    payload = {"type": _s(macro_type), "region": normalize_title(primary_region or ""),
               "policy": _s(policy_area).upper(), "day": day, "tag": "atp.macro.cluster.v1"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def macro_cluster_id_for(key: str) -> str:
    return "MG-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """The macro overlay for ONE newsroom message (keyed by its ``message_id``). Immutable; corrections and
    retractions are inherited from the newsroom record and mirrored here for macro-level queries."""

    message_id: str
    macro_type: str = MacroEventType.UNCLASSIFIED.value
    source_class: str = MacroSourceClass.OTHER.value
    geo_scope: str = GeoScope.UNKNOWN.value
    severity: str = Level.UNKNOWN.value                 # RESEARCH METADATA — never a confirmed impact
    policy_area: str | None = None
    affected_regions: tuple = ()
    affected_countries: tuple = ()
    affected_blocs: tuple = ()
    affected_asset_classes: tuple = ()
    link_status: str = LinkStatus.NONE.value
    correction_of_id: str | None = None
    retraction_of_id: str | None = None
    published_at: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def primary_region(self) -> str | None:
        regions = normalize_scope_list(self.affected_regions)
        if regions:
            return regions[0]
        countries = normalize_scope_list(self.affected_countries)
        return countries[0] if countries else None

    @property
    def macro_checksum(self) -> str:
        return macro_checksum(macro_type=self.macro_type, source_class=self.source_class,
                              geo_scope=self.geo_scope, policy_area=self.policy_area,
                              regions=self.affected_regions, countries=self.affected_countries,
                              blocs=self.affected_blocs, asset_classes=self.affected_asset_classes,
                              published_at=self.published_at)

    @property
    def macro_cluster_id(self) -> str:
        return macro_cluster_id_for(macro_cluster_key(
            macro_type=self.macro_type, primary_region=self.primary_region,
            policy_area=self.policy_area, published_at=self.published_at))

    def as_record(self) -> dict:
        return {
            "message_id": self.message_id, "macro_type": self.macro_type,
            "source_class": self.source_class, "geo_scope": self.geo_scope, "severity": self.severity,
            "policy_area": self.policy_area,
            "affected_regions_json": json.dumps(list(normalize_scope_list(self.affected_regions))),
            "affected_countries_json": json.dumps(list(normalize_scope_list(self.affected_countries))),
            "affected_blocs_json": json.dumps(list(normalize_scope_list(self.affected_blocs))),
            "affected_asset_classes_json": json.dumps(list(normalize_asset_classes(self.affected_asset_classes))),
            "link_status": self.link_status, "macro_cluster_id": self.macro_cluster_id,
            "macro_checksum": self.macro_checksum, "correction_of_id": self.correction_of_id,
            "retraction_of_id": self.retraction_of_id, "provenance_json": json.dumps(self.provenance, sort_keys=True),
        }


@dataclass(frozen=True, slots=True)
class MacroSourceEntry:
    """A macro/geopolitical/regulatory channel in the registry, with its explicit mandate, license + usage
    rights and availability. Fail-closed: unavailable and unlicensed until a real entitled source attaches.
    Never claim full coverage from a partial set of active sources."""

    source_id: str
    name: str
    source_class: str = MacroSourceClass.OTHER.value
    regions: tuple = ()
    languages: tuple = ()
    mandate: str = "unknown"                     # what the source is authoritative for (monetary/trade/...)
    update_mode: str = "unknown"                 # push / poll / batch / unknown
    rate_limit: dict = field(default_factory=dict)
    license_status: str = LicenseStatus.UNKNOWN.value
    storage_allowed: bool = False
    redistribution_allowed: bool = False
    commercial_use_allowed: bool = False
    attribution_required: bool = True
    available: bool = False

    def as_record(self) -> dict:
        return {
            "source_id": self.source_id, "name": self.name, "source_class": self.source_class,
            "regions_json": json.dumps(list(normalize_scope_list(self.regions))),
            "languages_json": json.dumps(list(self.languages)), "mandate": self.mandate,
            "update_mode": self.update_mode, "rate_limit_json": json.dumps(self.rate_limit, sort_keys=True),
            "license_status": self.license_status, "storage_allowed": self.storage_allowed,
            "redistribution_allowed": self.redistribution_allowed,
            "commercial_use_allowed": self.commercial_use_allowed,
            "attribution_required": self.attribution_required, "available": self.available,
        }
