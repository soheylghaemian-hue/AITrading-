"""WP6 — worldwide macro / geopolitical / regulatory event intake (RESEARCH DATA ONLY).

A read-only, fail-closed, provider-neutral pipeline for central-bank / supranational / regulatory /
sanctions / trade / conflict / energy / transport events. A macro event is a WP5 newsroom record PLUS a
macro overlay (this package). No source is activated, no network client ships here, and nothing reaches the
trading path: no orders/execution, no subscription purchase, no HTTP write path.

AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

# reused WP5 newsroom primitives, re-exported for convenience
from atp.newsroom.model import Level, LicenseStatus, MappingStatus, Primacy, message_id_for

from .ingest import (
    MacroIngestConfig,
    MacroIngestSummary,
    ingest_macro_events,
    macro_ingest_request_checksum,
)
from .model import (
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
from .provider import (
    LicenseMetadata,
    MacroEventItem,
    MacroEventProvider,
    MacroPage,
    MappingHint,
    NewsProviderRateLimitedError,
    NewsProviderStatus,
    NewsProviderUnavailableError,
    StubMacroEventProvider,
)
from .readmodel import macro_health, macro_source_coverage
from .registry import seed_registry, seed_sources

__all__ = [
    "AssetClassScope",
    "GeoScope",
    "Level",
    "LicenseMetadata",
    "LicenseStatus",
    "LinkStatus",
    "MacroEvent",
    "MacroEventItem",
    "MacroEventProvider",
    "MacroEventType",
    "MacroIngestConfig",
    "MacroIngestSummary",
    "MacroPage",
    "MacroSourceClass",
    "MacroSourceEntry",
    "MappingHint",
    "MappingStatus",
    "NewsProviderRateLimitedError",
    "NewsProviderStatus",
    "NewsProviderUnavailableError",
    "Primacy",
    "StubMacroEventProvider",
    "ingest_macro_events",
    "link_status_from_mappings",
    "macro_checksum",
    "macro_cluster_id_for",
    "macro_cluster_key",
    "macro_health",
    "macro_ingest_request_checksum",
    "macro_source_coverage",
    "message_id_for",
    "normalize_asset_classes",
    "normalize_scope_list",
    "resolve_link_status",
    "seed_registry",
    "seed_sources",
]
