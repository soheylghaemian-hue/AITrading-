"""WP6 — the narrow, READ-ONLY macro / geopolitical / regulatory event provider interface.

A provider exposes ONLY read-only capabilities: a capability/license probe, a cursor-paginated fetch of new
macro events, rate-limit info, and a provider-status check. It has NO order/execution/account method, cannot
buy a subscription or fetch anything but public/licensed data, and never leaks credentials. It reuses the WP5
newsroom license / status / error / mapping-hint types (a macro event IS a newsroom record + a macro overlay).
``StubMacroEventProvider`` serves deterministic fixtures so the pipeline is fully testable with ZERO network;
a real network provider may only be added when EXISTING legal credentials + usage rights apply (no new keys,
no paywall/ToS bypass, no scraping), and never runs in CI.

SAFETY: read-only macro event data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

# reuse the WP5 newsroom read-only provider primitives (no duplication of license/status/error/hint types)
from atp.newsroom.model import Level, Primacy
from atp.newsroom.provider import (
    LicenseMetadata,
    MappingHint,
    NewsProviderError,
    NewsProviderRateLimitedError,
    NewsProviderStatus,
    NewsProviderUnavailableError,
)

from .model import GeoScope, MacroEventType, MacroSourceClass

# re-export the reused provider types so callers can import them from the macro package
__all__ = [
    "LicenseMetadata",
    "MacroEventItem",
    "MacroEventProvider",
    "MacroPage",
    "MappingHint",
    "NewsProviderError",
    "NewsProviderRateLimitedError",
    "NewsProviderStatus",
    "NewsProviderUnavailableError",
    "StubMacroEventProvider",
]


@dataclass(frozen=True, slots=True)
class MacroEventItem:
    """One macro/geopolitical/regulatory event as a provider offers it. Macro-specific fields carry the
    overlay; the rest map to a newsroom record. Missing values stay None — never fabricated."""

    provider_id: str
    title: str = ""
    body: str | None = None
    language: str | None = None
    url: str | None = None
    source_name: str | None = None
    published_at: str | None = None
    primacy: Primacy = Primacy.UNKNOWN
    macro_type: MacroEventType = MacroEventType.UNCLASSIFIED
    source_class: MacroSourceClass = MacroSourceClass.OTHER
    geo_scope: GeoScope = GeoScope.UNKNOWN
    severity: Level = Level.UNKNOWN
    policy_area: str | None = None
    regions: tuple = ()
    countries: tuple = ()
    blocs: tuple = ()
    asset_classes: tuple = ()                 # tuple[AssetClassScope | str, ...] — coarse macro scope
    mapping_hints: tuple = ()                 # tuple[MappingHint, ...] — usually empty (macro ≠ instrument)
    correction_of_provider_id: str | None = None
    retraction_of_provider_id: str | None = None


@dataclass(frozen=True, slots=True)
class MacroPage:
    items: tuple = ()                         # tuple[MacroEventItem, ...]
    next_cursor: str | None = None            # None → no more pages


class MacroEventProvider(abc.ABC):
    """Read-only macro/geopolitical/regulatory event provider. Implementations MUST NOT expose
    order/execution/account methods."""

    name: str = "macro-provider"
    source_id: str = "macro-source"
    source_class: str = MacroSourceClass.OTHER.value

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
    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> MacroPage:
        """Fetch a page of new macro events from `cursor`. Returns items + the next cursor (None when done)."""

    def rate_limit_info(self) -> dict:
        return {}


# --------------------------------------------------------------------------- deterministic stub (tests/CI)
@dataclass
class StubMacroEventProvider(MacroEventProvider):
    """A deterministic, network-free macro-event provider for tests/CI. Serves the fixture pages it is given,
    paginating by cursor, and reports its license honestly (default: an unlicensed source → metadata only)."""

    name: str = "MACRO-STUB"
    source_id: str = "macro-stub-source"
    source_class: str = MacroSourceClass.OTHER.value
    pages: list = field(default_factory=list)      # list[list[MacroEventItem]] — one per page
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

    def fetch_new(self, *, cursor: str | None = None, limit: int = 100) -> MacroPage:
        self.calls.append(cursor)
        if self.unavailable:
            raise NewsProviderUnavailableError("stub: macro provider unavailable")
        if self.rate_limited:
            raise NewsProviderRateLimitedError("stub: rate limited")
        idx = 0 if cursor is None else int(cursor)
        if idx >= len(self.pages):
            return MacroPage(items=(), next_cursor=None)
        items = tuple(self.pages[idx])
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
        return MacroPage(items=items, next_cursor=nxt)
