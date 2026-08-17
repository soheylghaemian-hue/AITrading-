"""§ Phase R3.0 — Deterministic Backtesting & Strategy Validation (RESEARCH ONLY).

A new, fully DECOUPLED research/validation engine. It evaluates historical decision logic against
stored OHLC and produces immutable internal backtest records. It NEVER creates, submits, routes, or
simulates through a broker/order/execution/autonomous path, never touches the live kill switch, and
never imports the execution-coupled legacy `atp.backtest.Backtester`. Every value it reports traces to
real historical OHLC + the run's own immutable configuration snapshot — nothing is fabricated, and
missing information is recorded explicitly (never treated as zero).

Safety invariant for this whole package: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

#: Bumped whenever the deterministic replay/accounting semantics change. Persisted with every run so a
#: result is reproducible only against the exact engine that produced it.
ENGINE_VERSION = "r3-engine-1"

from .calendars import (  # noqa: E402
    AvailabilityPolicy, PointInTimeError, available_at, expected_bars, resolve_policy,
)
from .strategy import (  # noqa: E402
    ACTIONS, OHLC_TREND_BASELINE, ResearchDecision, ResearchStrategy, get_strategy,
)

__all__ = [
    "ENGINE_VERSION",
    "AvailabilityPolicy", "PointInTimeError", "available_at", "expected_bars", "resolve_policy",
    "ACTIONS", "OHLC_TREND_BASELINE", "ResearchDecision", "ResearchStrategy", "get_strategy",
]
