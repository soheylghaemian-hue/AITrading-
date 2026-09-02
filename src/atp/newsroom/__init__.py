"""WP5 — worldwide company news, official filings & regulatory publications (RESEARCH DATA ONLY).

A provider-neutral, persistent, multilingual, auditable foundation: immutable original messages with
separate translations, append-only corrections/retractions, a source registry with explicit license + usage
rights, fail-closed instrument mapping to the WP2 catalogue, deduplication/clustering, event classification,
a narrow read-only provider interface, and a resumable/observable/isolated import orchestrator. Classification,
relevance, sentiment and impact are research metadata — not facts, not trading signals. No trading, no
orders/execution, no news/subscription purchase, no HTTP write path.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

from .ingest import IngestConfig, IngestSummary, ingest_news, ingest_request_checksum
from .model import (
    EventCategory,
    LicenseStatus,
    MappingStatus,
    NewsMessage,
    Primacy,
    SourceRegistryEntry,
    SourceType,
    StorageStatus,
    TimeStatus,
    TranslationStatus,
    classify_time_status,
    cluster_id_for,
    content_checksum,
    message_id_for,
    resolve_mapping_status,
)
from .provider import (
    LicenseMetadata,
    MappingHint,
    NewsPage,
    NewsProvider,
    NewsProviderError,
    NewsProviderRateLimitedError,
    NewsProviderStatus,
    NewsProviderUnavailableError,
    ProviderNewsItem,
    StubNewsProvider,
)
from .readmodel import news_health, news_source_coverage
from .registry import seed_registry, seed_sources

__all__ = [
    "EventCategory",
    "IngestConfig",
    "IngestSummary",
    "LicenseMetadata",
    "LicenseStatus",
    "MappingHint",
    "MappingStatus",
    "NewsMessage",
    "NewsPage",
    "NewsProvider",
    "NewsProviderError",
    "NewsProviderRateLimitedError",
    "NewsProviderStatus",
    "NewsProviderUnavailableError",
    "Primacy",
    "ProviderNewsItem",
    "SourceRegistryEntry",
    "SourceType",
    "StorageStatus",
    "StubNewsProvider",
    "TimeStatus",
    "TranslationStatus",
    "classify_time_status",
    "cluster_id_for",
    "content_checksum",
    "ingest_news",
    "ingest_request_checksum",
    "message_id_for",
    "news_health",
    "news_source_coverage",
    "resolve_mapping_status",
    "seed_registry",
    "seed_sources",
]
