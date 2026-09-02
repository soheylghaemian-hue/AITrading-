"""WP7 — the narrow, READ-ONLY fundamentals & macro-series provider interface.

A provider exposes ONLY read-only capabilities: a capability/license probe, a cursor-paginated fetch of new
observations, rate-limit info, and a provider-status check. It has NO order/execution/account method, cannot
buy a subscription or fetch anything but public/licensed data, and never leaks credentials. It reuses the WP5
newsroom license / status / error / mapping-hint types. ``StubFundamentalProvider`` serves deterministic
fixtures so the pipeline is fully testable with ZERO network; a real network provider may only be added when
EXISTING legal credentials + usage rights apply (no new keys, no paywall/ToS bypass, no scraping), and never
runs in CI.

SAFETY: read-only fundamentals/macro data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

# reuse the WP5 newsroom read-only provider primitives (no duplication of license/status/error/hint types)
from atp.newsroom.model import Primacy
from atp.newsroom.provider import (
    LicenseMetadata,
    MappingHint,
    NewsProviderError,
    NewsProviderRateLimitedError,
    NewsProviderStatus,
    NewsProviderUnavailableError,
)

from .model import Frequency, FundamentalCategory, SourceType, Unit

__all__ = [
    "FundamentalItem",
    "FundamentalPage",
    "FundamentalProvider",
    "LicenseMetadata",
    "MappingHint",
    "NewsProviderError",
    "NewsProviderRateLimitedError",
    "NewsProviderStatus",
    "NewsProviderUnavailableError",
    "StubFundamentalProvider",
]


@dataclass(frozen=True, slots=True)
class FundamentalItem:
    """One fundamental/macro observation as a provider offers it, plus the series metadata it belongs to.
    Missing values stay None — never fabricated (a missing numeric value is NOT zero)."""

    series_key: str
    provider_id: str
    category: FundamentalCategory = FundamentalCategory.UNCLASSIFIED
    metric: str = ""
    unit: Unit = Unit.UNKNOWN
    frequency: Frequency = Frequency.UNKNOWN
    region: str | None = None
    country: str | None = None
    currency: str | None = None
    description: str | None = None
    period: str = ""
    period_start: str | None = None
    period_end: str | None = None
    value: object | None = None            # raw numeric (int/float/Decimal/str) — normalized fail-closed
    value_text: str | None = None          # non-numeric payload (e.g. a rating "AA+")
    revision_seq: int = 0
    revision_of_provider_id: str | None = None
    is_preliminary: bool = False
    published_at: str | None = None
    source_name: str | None = None
    primacy: Primacy = Primacy.UNKNOWN
    mapping_hints: tuple = ()               # tuple[MappingHint, ...] — for the SERIES → instrument mapping


@dataclass(frozen=True, slots=True)
class FundamentalPage:
    items: tuple = ()                       # tuple[FundamentalItem, ...]
    next_cursor: str | None = None          # None → no more pages


class FundamentalProvider(abc.ABC):
    """Read-only fundamentals/macro provider. Implementations MUST NOT expose order/execution/account
    methods."""

    name: str = "fundamentals-provider"
    source_id: str = "fundamentals-source"
    source_type: str = SourceType.OTHER.value

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True only when the provider can serve data (existing credentials/usage rights present)."""

    @abc.abstractmethod
    def capabilities(self) -> tuple:
        """The read-only capabilities the provider supports (e.g. ('fetch_new', 'cursor'))."""

    @abc.abstractmethod
    def license_metadata(self) -> LicenseMetadata:
        """The provider's license + usage rights — recorded explicitly; fail-closed when unknown."""

    @abc.abstractmethod
    def provider_status(self) -> NewsProviderStatus:
        """A read-only availability check — no side effects, no subscription purchase."""

    @abc.abstractmethod
    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> FundamentalPage:
        """Fetch a page of new observations from `cursor`. Returns items + the next cursor (None when done)."""

    def rate_limit_info(self) -> dict:
        return {}


# --------------------------------------------------------------------------- deterministic stub (tests/CI)
@dataclass
class StubFundamentalProvider(FundamentalProvider):
    """A deterministic, network-free fundamentals provider for tests/CI. Serves the fixture pages it is
    given, paginating by cursor, and reports its license honestly (default: unlicensed → metadata only)."""

    name: str = "FUND-STUB"
    source_id: str = "fund-stub-source"
    source_type: str = SourceType.OTHER.value
    pages: list = field(default_factory=list)      # list[list[FundamentalItem]] — one per page
    license: LicenseMetadata = field(default_factory=LicenseMetadata)
    available: bool = True
    rate_limited: bool = False
    unavailable: bool = False
    calls: list = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return True

    def capabilities(self) -> tuple:
        return ("fetch_new", "cursor", "license")

    def license_metadata(self) -> LicenseMetadata:
        return self.license

    def provider_status(self) -> NewsProviderStatus:
        return NewsProviderStatus(available=self.available and not self.unavailable,
                                  reason=("unavailable" if self.unavailable else ""),
                                  rate_limited=self.rate_limited)

    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> FundamentalPage:
        self.calls.append(cursor)
        if self.unavailable:
            raise NewsProviderUnavailableError("stub: fundamentals provider unavailable")
        if self.rate_limited:
            raise NewsProviderRateLimitedError("stub: rate limited")
        idx = 0 if cursor is None else int(cursor)
        if idx >= len(self.pages):
            return FundamentalPage(items=(), next_cursor=None)
        items = tuple(self.pages[idx])
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
        return FundamentalPage(items=items, next_cursor=nxt)
