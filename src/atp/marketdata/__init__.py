"""Provider-independent market-data layer (§ Phase 10).

A global instrument universe → contract resolution → NormalizedQuote → data-quality gate. The
autonomous pipeline consumes ONLY normalized, quality-gated quotes — it never touches IBKR
directly, never sees delayed/stale/invalid/fabricated prices.
"""

from .ingest import IngestConfig, IngestSummary, ingest_market_data, ingest_request_checksum
from .manager import MarketDataManager
from .massive_provider import (
    MASSIVE_SYMBOLS,
    MassiveAuthError,
    MassiveEntitlementError,
    MassiveError,
    MassiveProvider,
)
from .model import (
    AdjustmentPolicy,
    BarObservation,
    CorporateAction,
    DataStatus,
    EntitlementStatus,
    LicenseType,
    ProviderEntitlement,
    QualityFlag,
    QuoteObservation,
    classify_data_status,
    classify_quality,
)
from .provider_base import (
    InstrumentRef,
    MarketDataEntitlementError,
    MarketDataProvider,
    MarketDataProviderError,
    MarketDataUnavailableError,
    ProviderBar,
    ProviderCorporateAction,
    ProviderEntitlementResult,
    ProviderQuote,
    StubMarketDataProvider,
)
from .quality import QualityStatus, quality_gate
from .quote import NormalizedQuote
from .universe import GLOBAL_UNIVERSE, InstrumentSpec

__all__ = [
    "MarketDataManager", "QualityStatus", "quality_gate", "NormalizedQuote",
    "GLOBAL_UNIVERSE", "InstrumentSpec",
    "MassiveProvider", "MASSIVE_SYMBOLS",
    "MassiveError", "MassiveAuthError", "MassiveEntitlementError",
    # WP4 — provider-neutral persistent market-data foundation
    "DataStatus", "EntitlementStatus", "LicenseType", "QualityFlag", "AdjustmentPolicy",
    "QuoteObservation", "BarObservation", "CorporateAction", "ProviderEntitlement",
    "classify_data_status", "classify_quality",
    "MarketDataProvider", "StubMarketDataProvider", "InstrumentRef",
    "MarketDataProviderError", "MarketDataEntitlementError", "MarketDataUnavailableError",
    "ProviderQuote", "ProviderBar", "ProviderCorporateAction", "ProviderEntitlementResult",
    "IngestConfig", "IngestSummary", "ingest_market_data", "ingest_request_checksum",
]
