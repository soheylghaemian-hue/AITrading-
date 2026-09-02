"""WP5 — the narrow, READ-ONLY news/filings provider interface.

A provider exposes ONLY read-only capabilities: a capability/license probe, a cursor-paginated fetch of new
messages, rate-limit info, and a provider-status check. It has NO order/execution/account method, cannot buy
a subscription or fetch anything but public/licensed data, and never leaks credentials. `StubNewsProvider`
serves deterministic fixtures so the pipeline is fully testable with ZERO network; a real network provider
may only be added when EXISTING legal credentials + usage rights apply (no new keys, no paywall/ToS bypass,
no scraping), and never runs in CI.

SAFETY: read-only news data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from .model import EventCategory, LicenseStatus, Primacy


class NewsProviderError(Exception):
    def __init__(self, message: str, *, code: str = "NEWS_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class NewsProviderUnavailableError(NewsProviderError):
    """The provider is unavailable → the source is recorded UNAVAILABLE, never a frozen 'fresh' state."""

    def __init__(self, message: str, *, code: str = "NEWS_PROVIDER_UNAVAILABLE") -> None:
        super().__init__(message, code=code)


class NewsProviderRateLimitedError(NewsProviderError):
    def __init__(self, message: str, *, code: str = "NEWS_PROVIDER_RATE_LIMITED") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class MappingHint:
    """The identifiers a provider offers for instrument mapping. A bare `symbol` can never map uniquely;
    `con_id`, `isin` or `symbol`+`exchange` are the stable keys."""

    symbol: str | None = None
    exchange: str | None = None
    isin: str | None = None
    con_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderNewsItem:
    provider_id: str
    title: str = ""
    body: str | None = None
    language: str | None = None
    url: str | None = None
    source_name: str | None = None
    published_at: str | None = None
    primacy: Primacy = Primacy.UNKNOWN
    event_category: EventCategory = EventCategory.UNCLASSIFIED
    mapping_hints: tuple = ()                 # tuple[MappingHint, ...]
    correction_of_provider_id: str | None = None
    retraction_of_provider_id: str | None = None
    countries: tuple = ()
    regions: tuple = ()
    industries: tuple = ()
    companies: tuple = ()
    exchanges: tuple = ()


@dataclass(frozen=True, slots=True)
class NewsPage:
    items: tuple = ()                         # tuple[ProviderNewsItem, ...]
    next_cursor: str | None = None            # None → no more pages


@dataclass(frozen=True, slots=True)
class NewsProviderStatus:
    available: bool
    reason: str = ""
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class LicenseMetadata:
    license_status: LicenseStatus = LicenseStatus.UNKNOWN
    storage_allowed: bool = False
    redistribution_allowed: bool = False
    commercial_use_allowed: bool = False
    attribution_required: bool = True


class NewsProvider(abc.ABC):
    """Read-only news/filings provider. Implementations MUST NOT expose order/execution/account methods."""

    name: str = "provider"
    source_id: str = "source"
    source_type: str = "OTHER"

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
    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> NewsPage:
        """Fetch a page of new messages from `cursor`. Returns items + the next cursor (None when done)."""

    def rate_limit_info(self) -> dict:
        return {}


# --------------------------------------------------------------------------- deterministic stub (tests/CI)
@dataclass
class StubNewsProvider(NewsProvider):
    """A deterministic, network-free provider for tests/CI. Serves the fixture pages it is given, paginating
    by cursor, and reports its license honestly (default: an unlicensed source → metadata only)."""

    name: str = "STUB"
    source_id: str = "stub-source"
    source_type: str = "NEWS_AGGREGATOR"
    pages: list = field(default_factory=list)      # list[list[ProviderNewsItem]] — one per page
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

    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> NewsPage:
        self.calls.append(cursor)
        if self.unavailable:
            raise NewsProviderUnavailableError("stub: provider unavailable")
        if self.rate_limited:
            raise NewsProviderRateLimitedError("stub: rate limited")
        idx = 0 if cursor is None else int(cursor)
        if idx >= len(self.pages):
            return NewsPage(items=(), next_cursor=None)
        items = tuple(self.pages[idx])
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
        return NewsPage(items=items, next_cursor=nxt)
