"""§ R3.0A — Historical OHLC backfill & research data integrity.

Builds IMMUTABLE, versioned research OHLC datasets from split-adjusted 1-minute aggregates normalized to
regular-session (RTH) daily bars. Strictly decoupled market-DATA + persistence: this package imports
nothing from the execution / broker / IBKR / autonomous / F2 paths and NEVER writes live `ohlc_bars`.
Safety invariant across R3.0A: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

from .dataset import DatasetRequest, DatasetRequestError, build_request
from .normalize import (
    ADJUSTMENT_POLICY,
    MISSING_MINUTE_THRESHOLD,
    NORMALIZATION_POLICY,
    PROVIDER,
    PROVIDER_CONTRACT_VERSION,
    MinuteBar,
    last_completed_session,
    normalize_minutes_to_daily,
)
from .provider import (
    EntitlementError,
    FetchResult,
    MinuteAggregatesProvider,
    MockAggregatesProvider,
    PolygonAggregatesProvider,
    ProviderError,
)
from .readmodel import dataset_coverage, dataset_detail, dataset_summary, datasets_list
from .runner import (
    CHUNK_SESSIONS,
    MAX_PAGES_PER_CHUNK,
    MAX_RESULTS_PER_CHUNK,
    STALE_RUNNING_AFTER_S,
    BackfillConflict,
    claim_and_run,
    claim_dataset,
    enqueue_backfill,
    execute_claimed,
    execute_dataset,
    run_backfill,
)
from .select import validate_selection
from .validate import (
    StreamingPagesChecksum,
    ValidationError,
    dataset_checksum,
    raw_pages_checksum,
    validate_daily_bars,
    validate_minutes,
)

__all__ = [
    "DatasetRequest", "DatasetRequestError", "build_request",
    "MinuteBar", "normalize_minutes_to_daily", "last_completed_session",
    "PROVIDER", "PROVIDER_CONTRACT_VERSION", "ADJUSTMENT_POLICY", "NORMALIZATION_POLICY",
    "MISSING_MINUTE_THRESHOLD",
    "MinuteAggregatesProvider", "PolygonAggregatesProvider", "MockAggregatesProvider", "FetchResult",
    "EntitlementError", "ProviderError",
    "validate_minutes", "validate_daily_bars", "raw_pages_checksum", "dataset_checksum",
    "StreamingPagesChecksum", "ValidationError",
    "enqueue_backfill", "execute_dataset", "execute_claimed", "claim_dataset", "claim_and_run",
    "run_backfill", "BackfillConflict", "validate_selection",
    "CHUNK_SESSIONS", "MAX_PAGES_PER_CHUNK", "MAX_RESULTS_PER_CHUNK", "STALE_RUNNING_AFTER_S",
    "dataset_summary", "dataset_detail", "dataset_coverage", "datasets_list",
]
