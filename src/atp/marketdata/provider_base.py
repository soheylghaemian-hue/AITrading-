"""WP4 — the narrow, READ-ONLY market-data provider interface.

A provider exposes ONLY read-only data capabilities: instrument mapping, a quote, historical bars, corporate
actions, a capability/entitlement probe, rate-limit info, and an explicit realtime/delayed declaration. It
has NO order, execution, or account methods — by construction the interface cannot place, route, or cancel
an order, touch positions, or move funds. `StubMarketDataProvider` serves deterministic fixtures so the
pipeline is fully testable with ZERO network; real network providers may only be added when EXISTING
credentials and usage rights apply (no new keys, no paywall/ToS bypass, no scraping), and never run in CI.

SAFETY: read-only market data only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal

from .model import AdjustmentPolicy, DataStatus, EntitlementStatus, LicenseType


class MarketDataProviderError(Exception):
    """A classified, read-only provider fault. Never echoes credentials/response bodies."""

    def __init__(self, message: str, *, code: str = "PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class MarketDataEntitlementError(MarketDataProviderError):
    """The account is not entitled to this data from this provider (fail-closed → not REALTIME)."""

    def __init__(self, message: str, *, code: str = "PROVIDER_ENTITLEMENT_UNAVAILABLE") -> None:
        super().__init__(message, code=code)


class MarketDataUnavailableError(MarketDataProviderError):
    """Transient provider/connectivity fault — the instrument stays selectable for a later run."""

    def __init__(self, message: str, *, code: str = "PROVIDER_UNAVAILABLE") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """The minimal, broker-neutral instrument identity a provider needs — built from the WP2/WP3 catalogue
    row. Carries the WP3 verification so the provider layer can stay fail-closed without importing the store."""

    instrument_id: str
    symbol: str
    exchange: str
    currency: str
    asset_class: str
    con_id: int | None = None
    primary_exchange: str | None = None
    verified: bool = False


@dataclass(frozen=True, slots=True)
class ProviderMapping:
    provider_instrument_id: str
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderEntitlementResult:
    """The read-only capability/entitlement probe result (no side effects, no subscription purchase)."""

    configured: bool
    available: bool
    entitlement_status: EntitlementStatus
    license: LicenseType
    realtime_available: bool
    reason: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    requests_per_minute: int | None = None
    min_interval_s: float = 0.0
    burst: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    """Raw quote as the provider reports it, with the provider's OWN declared status/entitlement. The ingest
    layer re-classifies fail-closed — a provider claim of REALTIME is never trusted without entitlement +
    a verified instrument."""

    provider_instrument_id: str | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    volume: Decimal | None = None
    reference_price: Decimal | None = None
    previous_close: Decimal | None = None
    data_currency: str | None = None
    source_ts: str | None = None
    receive_ts: str | None = None
    declared_status: DataStatus = DataStatus.NO_DATA
    entitled: bool = False
    license: LicenseType = LicenseType.UNKNOWN


@dataclass(frozen=True, slots=True)
class ProviderBar:
    interval: str
    ts: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    trade_count: int | None = None
    data_currency: str | None = None
    source_ts: str | None = None
    receive_ts: str | None = None
    declared_status: DataStatus = DataStatus.END_OF_DAY
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    corporate_action_version: int = 0


@dataclass(frozen=True, slots=True)
class ProviderCorporateAction:
    action_type: str
    effective_date: str
    corporate_action_version: int = 0
    ex_date: str | None = None
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None


class MarketDataProvider(abc.ABC):
    """Read-only market-data provider. Implementations MUST NOT expose order/execution/account methods."""

    name: str = "provider"

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True only when the provider can actually serve data (credentials/usage rights already present)."""

    @abc.abstractmethod
    def map_instrument(self, instrument: InstrumentRef) -> ProviderMapping | None:
        """Resolve the provider-specific instrument id, or None when the provider cannot map it."""

    @abc.abstractmethod
    def probe_entitlement(self, instrument: InstrumentRef) -> ProviderEntitlementResult:
        """Read-only capability/entitlement check — no subscription is purchased or activated."""

    @abc.abstractmethod
    def get_quote(self, instrument: InstrumentRef) -> ProviderQuote | None:
        """A single read-only quote, or None when unavailable (never a fabricated value)."""

    @abc.abstractmethod
    def get_bars(self, instrument: InstrumentRef, *, interval: str, start: str, end: str) -> list[ProviderBar]:
        """Read-only historical bars for [start, end]."""

    @abc.abstractmethod
    def get_corporate_actions(self, instrument: InstrumentRef, *, start: str,
                              end: str) -> list[ProviderCorporateAction]:
        """Read-only corporate actions for [start, end]."""

    def rate_limit_info(self) -> RateLimitInfo:
        return RateLimitInfo()


# --------------------------------------------------------------------------- deterministic stub (tests/CI)
@dataclass
class StubMarketDataProvider(MarketDataProvider):
    """A deterministic, network-free provider for tests and CI. Serves only the fixtures it is given and
    reports its entitlement honestly (default: a FREE_OFFICIAL source that is NOT realtime-entitled)."""

    name: str = "STUB"
    quotes: dict = field(default_factory=dict)                # instrument_id -> ProviderQuote
    bars: dict = field(default_factory=dict)                  # instrument_id -> list[ProviderBar]
    corporate_actions: dict = field(default_factory=dict)     # instrument_id -> list[ProviderCorporateAction]
    mappings: dict = field(default_factory=dict)              # instrument_id -> provider_instrument_id
    license: LicenseType = LicenseType.FREE_OFFICIAL
    realtime_entitled: bool = False
    unavailable: set = field(default_factory=set)             # instrument_ids that raise MarketDataUnavailableError
    not_entitled: set = field(default_factory=set)            # instrument_ids that raise MarketDataEntitlementError

    @property
    def configured(self) -> bool:
        return True

    def _guard(self, instrument: InstrumentRef) -> None:
        if instrument.instrument_id in self.unavailable:
            raise MarketDataUnavailableError("stub: provider unavailable")
        if instrument.instrument_id in self.not_entitled:
            raise MarketDataEntitlementError("stub: not entitled")

    def map_instrument(self, instrument: InstrumentRef) -> ProviderMapping | None:
        pid = self.mappings.get(instrument.instrument_id)
        if pid is None and instrument.con_id is not None and self.name == "IBKR":
            pid = str(instrument.con_id)
        return ProviderMapping(provider_instrument_id=pid) if pid else None

    def probe_entitlement(self, instrument: InstrumentRef) -> ProviderEntitlementResult:
        entitled = self.realtime_entitled and instrument.instrument_id not in self.not_entitled
        status = (EntitlementStatus.ENTITLED if entitled
                  else EntitlementStatus.DELAYED_ONLY if self.license is LicenseType.FREE_OFFICIAL
                  else EntitlementStatus.NOT_ENTITLED)
        return ProviderEntitlementResult(
            configured=True, available=instrument.instrument_id not in self.unavailable,
            entitlement_status=status, license=self.license, realtime_available=entitled,
            reason="stub", capabilities=("quote", "bars", "corporate_actions"))

    def get_quote(self, instrument: InstrumentRef) -> ProviderQuote | None:
        self._guard(instrument)
        return self.quotes.get(instrument.instrument_id)

    def get_bars(self, instrument: InstrumentRef, *, interval: str, start: str, end: str) -> list[ProviderBar]:
        self._guard(instrument)
        return [b for b in self.bars.get(instrument.instrument_id, []) if b.interval == interval]

    def get_corporate_actions(self, instrument: InstrumentRef, *, start: str,
                              end: str) -> list[ProviderCorporateAction]:
        self._guard(instrument)
        return list(self.corporate_actions.get(instrument.instrument_id, []))
