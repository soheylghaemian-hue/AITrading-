"""WP7 — the global fundamentals & macro-series model (numeric reference observations, RESEARCH DATA ONLY).

A governed record for one numeric/structured fundamental OBSERVATION — a macro indicator (CPI, GDP, policy
rate, unemployment), a company fundamental (revenue, EPS, margin), a rating event, a profit warning — pulled
as a reference stream. Observations belong to a SERIES (a stable metric stream); each observation is an
IMMUTABLE data point, and a later REVISION of the same series+period is a NEW record linked to the prior one
(the "current" value is DERIVED, never a mutation of the original — mirroring the newsroom correction model).

Nothing is fabricated: a missing value stays ``None`` / NO DATA (never a zero, never an interpolation), a
missing publish time is never replaced by the receive time, a future publish time is flagged, and the
dedup/duplicate identity is a property of the ORIGINAL fetched value, independent of any license/storage
gate. Instrument / asset-class / region mapping is FAIL-CLOSED: a company series with no stable id is
AMBIGUOUS/UNMAPPED, a macro series that names no instrument is NONE, never a fabricated VERIFIED.

This module is PURE (no store, no network, no trading). SAFETY: no order/execution/account path.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum

# reuse the WP5 newsroom primitives — provenance / time / license semantics are consistent across the platform
from atp.newsroom.model import (
    Level,
    LicenseStatus,
    MappingStatus,
    Primacy,
    StorageStatus,
    TimeStatus,
    classify_time_status,
    utc_ts,
)

__all__ = [
    "Frequency",
    "FundamentalCategory",
    "FundamentalObservation",
    "FundamentalSeries",
    "FundamentalSourceEntry",
    "Level",
    "LicenseStatus",
    "LinkStatus",
    "MappingStatus",
    "Primacy",
    "SourceType",
    "StorageStatus",
    "TimeStatus",
    "Unit",
    "ValueStatus",
    "classify_time_status",
    "classify_value_status",
    "content_checksum",
    "link_status_from_mappings",
    "normalize_token",
    "normalize_value",
    "observation_id_for",
    "resolve_link_status",
    "series_id_for",
]


class FundamentalCategory(str, Enum):
    """The kind of fundamental / macro observation. UNCLASSIFIED is the fail-closed default — never fabricated."""

    MACRO_INDICATOR = "MACRO_INDICATOR"
    GDP = "GDP"
    INFLATION = "INFLATION"
    INTEREST_RATE = "INTEREST_RATE"
    EMPLOYMENT = "EMPLOYMENT"
    TRADE_BALANCE = "TRADE_BALANCE"
    SENTIMENT = "SENTIMENT"
    COMPANY_FUNDAMENTAL = "COMPANY_FUNDAMENTAL"
    EARNINGS = "EARNINGS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    PROFIT_WARNING = "PROFIT_WARNING"
    RATING_EVENT = "RATING_EVENT"
    VOLUME_METRIC = "VOLUME_METRIC"
    OTHER = "OTHER"
    UNCLASSIFIED = "UNCLASSIFIED"


class SourceType(str, Enum):
    """The class of fundamentals/macro channel. OTHER is the fail-closed default."""

    STATISTICS_OFFICE = "STATISTICS_OFFICE"      # national statistics office (BLS, Eurostat, …)
    CENTRAL_BANK = "CENTRAL_BANK"
    SUPRANATIONAL = "SUPRANATIONAL"              # IMF / World Bank / OECD data
    RATING_AGENCY = "RATING_AGENCY"
    COMPANY_FILINGS = "COMPANY_FILINGS"          # issuer financial statements
    DATA_VENDOR = "DATA_VENDOR"                  # a LICENSED fundamentals vendor
    OTHER = "OTHER"


class Unit(str, Enum):
    """The unit of a numeric value. UNKNOWN is fail-closed (a value with an unknown unit is not reinterpreted)."""

    PERCENT = "PERCENT"
    INDEX = "INDEX"
    RATIO = "RATIO"
    CURRENCY = "CURRENCY"
    COUNT = "COUNT"
    PERSONS = "PERSONS"
    BPS = "BPS"
    UNKNOWN = "UNKNOWN"


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMIANNUAL = "SEMIANNUAL"
    ANNUAL = "ANNUAL"
    IRREGULAR = "IRREGULAR"
    UNKNOWN = "UNKNOWN"


class ValueStatus(str, Enum):
    OK = "OK"                    # a finite numeric value is present
    NON_NUMERIC = "NON_NUMERIC"  # a structured non-numeric payload (e.g. a rating "AA+") in value_text
    MISSING = "MISSING"          # no value — NOT fabricated, NOT zero, NOT interpolated


class LinkStatus(str, Enum):
    """Fail-closed summary of a SERIES's linkage to catalogue instruments. NONE is the default: a macro series
    (CPI, GDP) names no instrument — it is not instrument-specific, which is not the same as UNMAPPED."""

    VERIFIED = "VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMAPPED = "UNMAPPED"
    NONE = "NONE"


# --------------------------------------------------------------------------- helpers
def _s(value) -> str:
    return (value or "").strip()


def normalize_token(value) -> str:
    """Canonical upper token for a scope/dimension value. A ``(str, Enum)`` member's ``str()`` is its dotted
    repr, so extract ``.value`` FIRST — otherwise an enum input would be silently mangled."""
    return str(getattr(value, "value", value)).strip().upper()


def normalize_value(value) -> str | None:
    """Canonical decimal string for a numeric fundamental value; ``None`` stays ``None`` (never fabricated).
    A non-finite or non-numeric input returns ``None`` (the caller uses ``value_text`` for non-numeric
    payloads). Trailing zeros are normalized (3.20 == 3.2) and exponents are avoided, so equal values share a
    checksum — this is a true-equality merge, never a silent collision of DISTINCT values."""
    if value is None or isinstance(value, bool):    # bool is an int subclass — never a fundamental value
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    if d.is_zero():
        d = Decimal(0)          # normalize -0 to 0 so numerically-equal zeros share one token (dedup correctly)
    return format(d.normalize(), "f")


def classify_value_status(value: str | None, value_text: str | None) -> ValueStatus:
    """Fail-closed value integrity: a present numeric value is OK; else a non-numeric text payload is
    NON_NUMERIC; else MISSING (never invented)."""
    if value is not None:
        return ValueStatus.OK
    if _s(value_text):
        return ValueStatus.NON_NUMERIC
    return ValueStatus.MISSING


def resolve_link_status(*, had_hints: bool, match_count: int, by_stable_id: bool) -> LinkStatus:
    """Fail-closed instrument-linkage summary for a series. No hints → NONE (macro series, not
    instrument-specific). With hints: 0 → UNMAPPED; a symbol-only hint is never unique → AMBIGUOUS; a stable
    id with exactly one match → VERIFIED; several → AMBIGUOUS."""
    if not had_hints:
        return LinkStatus.NONE
    if match_count == 0:
        return LinkStatus.UNMAPPED
    if not by_stable_id:
        return LinkStatus.AMBIGUOUS
    return LinkStatus.VERIFIED if match_count == 1 else LinkStatus.AMBIGUOUS


def link_status_from_mappings(mapping_statuses, *, had_hints: bool) -> LinkStatus:
    """Summarize per-instrument mapping statuses into one fail-closed series link status. Any AMBIGUOUS →
    AMBIGUOUS; hints but no rows → UNMAPPED; all VERIFIED → VERIFIED; no hints → NONE."""
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


def series_id_for(source_id: str, series_key: str) -> str:
    """Stable internal series id per (source, series key), idempotent so re-ingest maps to the same series."""
    raw = f"{_s(source_id)}|{_s(series_key)}"
    return "FS-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def observation_id_for(provider: str, provider_id: str) -> str:
    """Stable internal observation id per provider observation (re-ingest → same id → no duplicate)."""
    raw = f"{_s(provider)}|{_s(provider_id)}"
    return "FO-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def content_checksum(*, series_id: str, period: str, value: str | None, value_text: str | None,
                     revision_seq: int, published_at) -> str:
    """Provider-neutral exact-duplicate key over the observation's CONTENT (series + period + value +
    revision + publish time). Two byte-identical observations — even from different providers — share this
    checksum. A REVISION (different value or revision_seq) has a DIFFERENT checksum, so a revision is never
    mistaken for a duplicate, and two distinct values never silently collide."""
    payload = {"series": _s(series_id), "period": _s(period), "value": (value if value is not None else ""),
               "text": _s(value_text), "rev": int(revision_seq),
               "published_at": utc_ts(published_at) or "", "tag": "atp.fundseries.obs.v1"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FundamentalSeries:
    """The definition of a fundamental/macro metric stream. Mutable registry row; its instrument linkage is
    fail-closed and stored separately (never a fabricated VERIFIED)."""

    source_id: str
    series_key: str
    category: str = FundamentalCategory.UNCLASSIFIED.value
    metric: str = ""
    unit: str = Unit.UNKNOWN.value
    frequency: str = Frequency.UNKNOWN.value
    region: str | None = None
    country: str | None = None
    currency: str | None = None
    description: str | None = None
    link_status: str = LinkStatus.NONE.value
    provenance: dict = field(default_factory=dict)

    @property
    def series_id(self) -> str:
        return series_id_for(self.source_id, self.series_key)

    def as_record(self) -> dict:
        return {
            "series_id": self.series_id, "source_id": self.source_id, "series_key": self.series_key,
            "category": self.category, "metric": self.metric, "unit": self.unit,
            "frequency": self.frequency,
            "region": (normalize_token(self.region) if self.region else None),
            "country": (normalize_token(self.country) if self.country else None),
            "currency": (normalize_token(self.currency) if self.currency else None),
            "description": self.description, "link_status": self.link_status,
            "provenance_json": json.dumps(self.provenance, sort_keys=True),
        }


@dataclass(frozen=True, slots=True)
class FundamentalObservation:
    """One IMMUTABLE fundamental/macro data point. A later revision of the same series+period is a SEPARATE
    observation linked via ``revision_of_id``; the original is never overwritten."""

    series_id: str
    provider: str
    provider_id: str
    source_id: str
    period: str = ""
    period_start: str | None = None
    period_end: str | None = None
    value: str | None = None            # the STORED value (license-gated by the caller); None → not stored
    # the as-fetched raw value used ONLY for the dedup checksum — never persisted as its own column, so the
    # duplicate identity is independent of the license/storage gate (mirrors the newsroom fetched_body).
    fetched_value: str | None = None
    value_text: str | None = None
    revision_seq: int = 0
    revision_of_id: str | None = None
    is_preliminary: bool = False
    published_at: str | None = None
    received_at: str | None = None
    license_status: str = LicenseStatus.UNKNOWN.value
    storage_status: str = StorageStatus.STORED_METADATA_ONLY.value
    duplicate_of_id: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def observation_id(self) -> str:
        return observation_id_for(self.provider, self.provider_id)

    @property
    def value_status(self) -> str:
        return classify_value_status(self.value, self.value_text)

    @property
    def time_status(self) -> str:
        return classify_time_status(self.published_at, self.received_at).value

    @property
    def content_checksum(self) -> str:
        raw = self.fetched_value if self.fetched_value is not None else self.value
        return content_checksum(series_id=self.series_id, period=self.period, value=raw,
                                value_text=self.value_text, revision_seq=self.revision_seq,
                                published_at=self.published_at)

    def as_record(self, *, duplicate_of_id: str | None = None) -> dict:
        return {
            "observation_id": self.observation_id, "series_id": self.series_id, "provider": self.provider,
            "provider_id": self.provider_id, "source_id": self.source_id, "period": self.period,
            "period_start": utc_ts(self.period_start), "period_end": utc_ts(self.period_end),
            "value": self.value, "value_text": self.value_text, "value_status": self.value_status,
            "revision_seq": int(self.revision_seq), "revision_of_id": self.revision_of_id,
            "is_preliminary": bool(self.is_preliminary),
            "published_at": utc_ts(self.published_at), "received_at": utc_ts(self.received_at),
            "time_status": self.time_status, "license_status": self.license_status,
            "storage_status": self.storage_status, "content_checksum": self.content_checksum,
            "duplicate_of_id": duplicate_of_id if duplicate_of_id is not None else self.duplicate_of_id,
            "provenance_json": json.dumps(self.provenance, sort_keys=True),
        }


@dataclass(frozen=True, slots=True)
class FundamentalSourceEntry:
    """A fundamentals/macro data source in the registry, with its explicit license + usage rights and
    availability. Fail-closed: unavailable and unlicensed until a real entitled source attaches."""

    source_id: str
    name: str
    source_type: str = SourceType.OTHER.value
    regions: tuple = ()
    languages: tuple = ()
    update_mode: str = "unknown"
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
            "regions_json": json.dumps([normalize_token(r) for r in self.regions if r is not None and _s(r)]),
            "languages_json": json.dumps(list(self.languages)), "update_mode": self.update_mode,
            "rate_limit_json": json.dumps(self.rate_limit, sort_keys=True),
            "license_status": self.license_status, "storage_allowed": self.storage_allowed,
            "redistribution_allowed": self.redistribution_allowed,
            "commercial_use_allowed": self.commercial_use_allowed,
            "attribution_required": self.attribution_required, "available": self.available,
        }
