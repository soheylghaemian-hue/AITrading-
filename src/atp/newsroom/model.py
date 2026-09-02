"""WP5 — the unified, provider-neutral news & official-filings model.

A governed record for a worldwide company news item, official filing, or regulatory publication. The
ORIGINAL message is immutable and never overwritten; corrections and retractions are NEW records linked to
it, and "retracted" is DERIVED (never a mutation of the original). Translations are stored SEPARATELY from
the original and never as the original text. Nothing is fabricated: an absent value stays ``None`` / NO DATA,
a missing publish time is never replaced by the receive time, a future publish time is flagged as a conflict,
a rumor never becomes a confirmed message, and a secondary source is never relabeled primary. Classification,
relevance, sentiment and impact are RESEARCH METADATA — not facts, not trading signals.

This module is PURE (no store, no network, no trading). SAFETY: no order/execution/account path.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class SourceType(str, Enum):
    COMPANY_IR = "COMPANY_IR"                 # company website / investor relations
    EXCHANGE_NOTICE = "EXCHANGE_NOTICE"       # exchange notice / ad-hoc disclosure
    REGULATORY_FILING = "REGULATORY_FILING"   # SEC EDGAR, RNS, SEDAR+, TDnet/EDINET, HKEXnews, ASX, EU NSMs
    REGULATOR = "REGULATOR"                   # a regulatory authority
    CENTRAL_BANK = "CENTRAL_BANK"
    NEWS_AGGREGATOR = "NEWS_AGGREGATOR"       # a LICENSED news aggregator
    OTHER = "OTHER"


class Primacy(str, Enum):
    PRIMARY = "PRIMARY"          # the originating source (issuer / exchange / regulator)
    SECONDARY = "SECONDARY"      # a re-publication / aggregation
    UNKNOWN = "UNKNOWN"


class TranslationStatus(str, Enum):
    ORIGINAL_ONLY = "ORIGINAL_ONLY"           # only the original language is stored
    TRANSLATED = "TRANSLATED"
    TRANSLATION_PENDING = "TRANSLATION_PENDING"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"          # already in the target language


class EventCategory(str, Enum):
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    PROFIT_WARNING = "PROFIT_WARNING"
    DIVIDEND = "DIVIDEND"
    CAPITAL_ACTION = "CAPITAL_ACTION"
    BUYBACK = "BUYBACK"
    MA = "MA"                                  # merger / acquisition
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    PRODUCT = "PRODUCT"
    LITIGATION = "LITIGATION"
    REGULATION = "REGULATION"
    INSIDER_TRANSACTION = "INSIDER_TRANSACTION"
    RATING = "RATING"
    FINANCING = "FINANCING"
    INSOLVENCY = "INSOLVENCY"
    TRADING_HALT = "TRADING_HALT"
    CYBER = "CYBER"
    SUPPLY_CHAIN = "SUPPLY_CHAIN"
    MACRO_CENTRAL_BANK = "MACRO_CENTRAL_BANK"
    GEOPOLITICS_REF = "GEOPOLITICS_REF"
    OTHER = "OTHER"
    UNCLASSIFIED = "UNCLASSIFIED"              # fail-closed default — not a fabricated category


class MappingStatus(str, Enum):
    VERIFIED = "VERIFIED"        # a single catalogue instrument matched a stable identifier
    AMBIGUOUS = "AMBIGUOUS"      # >1 plausible match, or a symbol-only hint (never unique)
    UNMAPPED = "UNMAPPED"        # no catalogue match


class LicenseStatus(str, Enum):
    UNKNOWN = "UNKNOWN"                              # fail-closed → metadata only, no redistribution
    LICENSED_STORE_REDISTRIBUTE = "LICENSED_STORE_REDISTRIBUTE"
    LICENSED_STORE_ONLY = "LICENSED_STORE_ONLY"     # may store, may NOT redistribute
    METADATA_ONLY = "METADATA_ONLY"                 # may store metadata/link only, not the body
    NO_LICENSE = "NO_LICENSE"                        # may not store the content


class StorageStatus(str, Enum):
    STORED_FULL = "STORED_FULL"                     # title + body stored (license permits)
    STORED_METADATA_ONLY = "STORED_METADATA_ONLY"   # only title/link/metadata stored
    NOT_STORED = "NOT_STORED"                        # nothing beyond provenance stored


class TimeStatus(str, Enum):
    OK = "OK"
    MISSING_PUBLISH = "MISSING_PUBLISH"     # no publish time — receive time is NOT substituted
    FUTURE_CONFLICT = "FUTURE_CONFLICT"     # publish time is in the future → flagged, not accepted as fresh


class Level(str, Enum):
    """Shared scale for relevance / impact / uncertainty / source-confidence research metadata. Default
    UNKNOWN — these are estimates, never facts, and are never fabricated."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --------------------------------------------------------------------------- helpers
def _s(value) -> str:
    return (value or "").strip()


def utc_ts(value) -> str | None:
    """Canonical UTC ISO-8601 (offset +00:00). Naive / bad → None. Never fabricates a time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))   # 3.11+ parses a trailing 'Z'
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC).isoformat()


def classify_time_status(published_at, received_at) -> TimeStatus:
    """Fail-closed time integrity: a missing publish time is MISSING_PUBLISH (never replaced by receive
    time); a publish time later than receive is a FUTURE_CONFLICT (flagged, not accepted as fresh)."""
    p, r = utc_ts(published_at), utc_ts(received_at)
    if p is None:
        return TimeStatus.MISSING_PUBLISH
    if r is not None and datetime.fromisoformat(p) > datetime.fromisoformat(r):
        return TimeStatus.FUTURE_CONFLICT
    return TimeStatus.OK


_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^0-9a-zÀ-￿ ]+")


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed title for near-duplicate clustering."""
    t = _NONWORD.sub(" ", (title or "").lower())
    return _WS.sub(" ", t).strip()


def content_checksum(*, original_title: str, original_body: str | None, original_language: str | None,
                     published_at) -> str:
    """Provider-NEUTRAL exact-duplicate key over the content itself (title+body+language+publish time). Two
    byte-identical items — even from different providers — share this checksum and are detected as exact
    duplicates. Provider/url/receive-time deliberately excluded so syndication is caught."""
    payload = {"title": _s(original_title), "body": _s(original_body),
               "lang": _s(original_language).lower(), "published_at": utc_ts(published_at) or "",
               "tag": "atp.news.content.v1"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cluster_key(*, original_title: str, published_at) -> str:
    """Near-duplicate / syndication cluster key: normalized title + publish DAY. Members share a cluster_id
    (the original source of the cluster is derived at read time as its PRIMARY, earliest member)."""
    day = (utc_ts(published_at) or "")[:10]
    payload = {"nt": normalize_title(original_title), "day": day, "tag": "atp.news.cluster.v1"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def message_id_for(provider: str, provider_id: str) -> str:
    """Stable internal message id, idempotent per provider message (re-ingest → same id → no duplicate)."""
    raw = f"{_s(provider)}|{_s(provider_id)}"
    return "NM-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def cluster_id_for(key: str) -> str:
    return "NC-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def resolve_mapping_status(*, match_count: int, by_stable_id: bool) -> MappingStatus:
    """Fail-closed mapping decision. A stable identifier (conId / ISIN / symbol+exchange) that yields EXACTLY
    ONE catalogue instrument is VERIFIED; a symbol-only hint (``by_stable_id`` False) is NEVER unique — any
    match(es) → AMBIGUOUS; several matches → AMBIGUOUS; no match → UNMAPPED. Symbols of different exchanges
    stay separate because each catalogue instrument has its own instrument_id."""
    if match_count == 0:
        return MappingStatus.UNMAPPED
    if not by_stable_id:
        return MappingStatus.AMBIGUOUS      # symbol alone can never create a unique mapping
    return MappingStatus.VERIFIED if match_count == 1 else MappingStatus.AMBIGUOUS


@dataclass(frozen=True, slots=True)
class NewsMessage:
    """One immutable original news/filing message. Corrections/retractions are SEPARATE messages linked via
    ``correction_of_id`` / ``retraction_of_id``; the original is never overwritten."""

    provider: str
    provider_id: str
    source_id: str
    source_type: str = SourceType.OTHER.value
    primacy: str = Primacy.UNKNOWN.value
    original_title: str = ""
    original_body: str | None = None
    # The as-fetched original body, used ONLY to compute the content-identity checksum. It is NEVER persisted
    # as a column — storage of the body itself is license-gated via ``original_body`` + ``storage_status``. This
    # keeps dedup identity a property of the ORIGINAL CONTENT, independent of any redistribution/storage policy:
    # a metadata-only source (original_body=None) still fingerprints its distinct fetched bodies, and the same
    # syndicated story fingerprints identically whether or not a given source is licensed to store it. When None,
    # the checksum falls back to ``original_body`` (so a message built without a separate fetched body is stable).
    fetched_body: str | None = None
    original_language: str | None = None
    translated_title: str | None = None
    translated_summary: str | None = None
    translation_status: str = TranslationStatus.ORIGINAL_ONLY.value
    translation_source: str | None = None
    url: str | None = None
    published_at: str | None = None
    received_at: str | None = None
    correction_at: str | None = None
    event_category: str = EventCategory.UNCLASSIFIED.value
    relevance: str = Level.UNKNOWN.value
    impact_estimate: str = Level.UNKNOWN.value
    uncertainty: str = Level.UNKNOWN.value
    source_confidence: str = Level.UNKNOWN.value
    license_status: str = LicenseStatus.UNKNOWN.value
    storage_status: str = StorageStatus.STORED_METADATA_ONLY.value
    correction_of_id: str | None = None
    retraction_of_id: str | None = None
    supersedes_id: str | None = None
    duplicate_of_id: str | None = None
    affected_countries: tuple = ()
    affected_regions: tuple = ()
    affected_industries: tuple = ()
    affected_companies: tuple = ()
    affected_exchanges: tuple = ()
    provenance: dict = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        return message_id_for(self.provider, self.provider_id)

    @property
    def time_status(self) -> str:
        """Fail-closed by construction: derived from the timestamps, never a stored value that could go stale
        or claim OK over a NULL publish time (missing publish → MISSING_PUBLISH, publish > receive → CONFLICT)."""
        return classify_time_status(self.published_at, self.received_at).value

    @property
    def content_checksum(self) -> str:
        # fingerprint the as-fetched original content (fetched_body), NOT the license-gated stored body, so the
        # dedup/duplicate identity never depends on whether a source was permitted to store the body.
        body = self.fetched_body if self.fetched_body is not None else self.original_body
        return content_checksum(original_title=self.original_title, original_body=body,
                                original_language=self.original_language, published_at=self.published_at)

    @property
    def cluster_id(self) -> str:
        return cluster_id_for(cluster_key(original_title=self.original_title, published_at=self.published_at))

    def as_record(self, *, duplicate_of_id: str | None = None) -> dict:
        return {
            "message_id": self.message_id, "provider": self.provider, "provider_id": self.provider_id,
            "source_id": self.source_id, "source_type": self.source_type, "primacy": self.primacy,
            "original_title": self.original_title, "original_body": self.original_body,
            "original_language": self.original_language, "translated_title": self.translated_title,
            "translated_summary": self.translated_summary, "translation_status": self.translation_status,
            "translation_source": self.translation_source, "url": self.url,
            "published_at": utc_ts(self.published_at), "received_at": utc_ts(self.received_at),
            "correction_at": utc_ts(self.correction_at), "event_category": self.event_category,
            "relevance": self.relevance, "impact_estimate": self.impact_estimate,
            "uncertainty": self.uncertainty, "source_confidence": self.source_confidence,
            "license_status": self.license_status, "storage_status": self.storage_status,
            "time_status": self.time_status, "content_checksum": self.content_checksum,
            "cluster_id": self.cluster_id, "correction_of_id": self.correction_of_id,
            "retraction_of_id": self.retraction_of_id, "supersedes_id": self.supersedes_id,
            "duplicate_of_id": duplicate_of_id if duplicate_of_id is not None else self.duplicate_of_id,
            "affected_countries_json": json.dumps(list(self.affected_countries)),
            "affected_regions_json": json.dumps(list(self.affected_regions)),
            "affected_industries_json": json.dumps(list(self.affected_industries)),
            "affected_companies_json": json.dumps(list(self.affected_companies)),
            "affected_exchanges_json": json.dumps(list(self.affected_exchanges)),
            "provenance_json": json.dumps(self.provenance, sort_keys=True),
        }


@dataclass(frozen=True, slots=True)
class SourceRegistryEntry:
    """A news/filings source in the registry, with its explicit license + usage rights and availability.
    Never claim full coverage from a partial set of active sources."""

    source_id: str
    name: str
    source_type: str = SourceType.OTHER.value
    primacy: str = Primacy.UNKNOWN.value
    regions: tuple = ()
    languages: tuple = ()
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
            "source_id": self.source_id, "name": self.name, "source_type": self.source_type,
            "primacy": self.primacy, "regions_json": json.dumps(list(self.regions)),
            "languages_json": json.dumps(list(self.languages)), "update_mode": self.update_mode,
            "rate_limit_json": json.dumps(self.rate_limit, sort_keys=True),
            "license_status": self.license_status,
            "storage_allowed": self.storage_allowed, "redistribution_allowed": self.redistribution_allowed,
            "commercial_use_allowed": self.commercial_use_allowed,
            "attribution_required": self.attribution_required, "available": self.available,
        }
