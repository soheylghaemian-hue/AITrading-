"""WP7 — global fundamentals & macro-series intake (RESEARCH DATA ONLY).

A read-only, fail-closed, provider-neutral pipeline for macro indicators and company fundamentals as
reference streams (numeric observations with an immutable revision history). No source is activated, no
network client ships here, and nothing reaches the trading path: no orders/execution, no subscription
purchase, no HTTP write path.

AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

# reused WP5 newsroom primitives, re-exported for convenience
from atp.newsroom.model import Level, LicenseStatus, MappingStatus, Primacy

from .ingest import (
    FundamentalIngestConfig,
    FundamentalIngestSummary,
    fundamentals_ingest_request_checksum,
    ingest_fundamentals,
)
from .model import (
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
from .provider import (
    FundamentalItem,
    FundamentalPage,
    FundamentalProvider,
    LicenseMetadata,
    MappingHint,
    NewsProviderRateLimitedError,
    NewsProviderStatus,
    NewsProviderUnavailableError,
    StubFundamentalProvider,
)
from .readmodel import fundamentals_health, fundamentals_source_coverage
from .registry import seed_registry, seed_sources

__all__ = [
    "Frequency",
    "FundamentalCategory",
    "FundamentalIngestConfig",
    "FundamentalIngestSummary",
    "FundamentalItem",
    "FundamentalObservation",
    "FundamentalPage",
    "FundamentalProvider",
    "FundamentalSeries",
    "FundamentalSourceEntry",
    "Level",
    "LicenseMetadata",
    "LicenseStatus",
    "LinkStatus",
    "MappingHint",
    "MappingStatus",
    "NewsProviderRateLimitedError",
    "NewsProviderStatus",
    "NewsProviderUnavailableError",
    "Primacy",
    "SourceType",
    "StubFundamentalProvider",
    "Unit",
    "ValueStatus",
    "classify_value_status",
    "content_checksum",
    "fundamentals_health",
    "fundamentals_ingest_request_checksum",
    "fundamentals_source_coverage",
    "ingest_fundamentals",
    "link_status_from_mappings",
    "normalize_value",
    "observation_id_for",
    "resolve_link_status",
    "seed_registry",
    "seed_sources",
    "series_id_for",
]
