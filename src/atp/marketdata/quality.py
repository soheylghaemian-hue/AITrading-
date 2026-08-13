"""Data-quality gate (§ Phase 10) — the single authority on whether a quote may drive autonomous
trading. Only READY (real-time, two-sided, valid, fresh, known source) passes. Everything else is
rejected with an explicit reason. Never fabricate, never accept delayed/stale/invalid data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class QualityStatus(str, Enum):
    READY = "READY"                              # the ONLY tradeable state
    SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"
    DELAYED = "DELAYED"
    STALE = "STALE"
    INVALID = "INVALID"
    BLOCKED = "BLOCKED"
    CLOSED_MARKET = "CLOSED_MARKET"
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"


def _present(v) -> bool:
    """A real, usable price/size: a finite number strictly > 0 (rejects None, NaN, 0, -1 sentinel)."""
    try:
        f = float(v)
        return f == f and f > 0.0
    except (TypeError, ValueError):
        return False


def quality_gate(q, *, now: datetime | None = None, max_age_s: float = 30.0) -> tuple[QualityStatus, str]:
    """Classify a NormalizedQuote. Returns (status, reason). READY only when the data is real-time,
    two-sided, positive, non-crossed, fresh and carries a source."""
    now = now or datetime.now(timezone.utc)
    ec = q.error_code
    em = (q.error_message or "").lower()

    # 1) explicit provider errors
    if ec == 10089 or "subscription" in em:
        return (QualityStatus.SUBSCRIPTION_REQUIRED, "IBKR 10089 — market-data subscription required")
    if ec == 10197:
        return (QualityStatus.BLOCKED, "competing live session (IBKR 10197)")

    # 2) sentinel / negative values are never a valid price
    for name, v in (("bid", q.bid), ("ask", q.ask), ("last", q.last)):
        if v is not None:
            try:
                if float(v) < 0.0:
                    return (QualityStatus.INVALID, f"negative/sentinel {name} ({v})")
            except (TypeError, ValueError):
                return (QualityStatus.INVALID, f"non-numeric {name}")

    mdt = (q.market_data_type or "").upper()
    if mdt in ("DELAYED", "DELAYED_FROZEN"):
        return (QualityStatus.DELAYED, "delayed feed — not real-time")
    if mdt == "FROZEN":
        return (QualityStatus.INVALID, "frozen data — not real-time")

    # 3) no usable price at all
    if not any(_present(v) for v in (q.bid, q.ask, q.last)):
        if ec is not None:
            return (QualityStatus.BLOCKED, q.error_message or f"IBKR error {ec}")
        return (QualityStatus.DATA_NOT_AVAILABLE, "no quote")

    # 4) require a valid two-sided top of book
    if not _present(q.bid):
        return (QualityStatus.INVALID, "missing/invalid bid")
    if not _present(q.ask):
        return (QualityStatus.INVALID, "missing/invalid ask")
    if float(q.ask) < float(q.bid):
        return (QualityStatus.INVALID, f"crossed quote (ask {q.ask} < bid {q.bid})")

    # 5) staleness
    if q.timestamp is not None:
        age = (now - q.timestamp).total_seconds()
        if age > max_age_s:
            return (QualityStatus.STALE, f"stale quote ({age:.0f}s old)")

    # 6) unknown source
    if not (q.source or q.exchange):
        return (QualityStatus.INVALID, "unknown source")

    # 7) must be real-time
    if mdt != "REALTIME":
        return (QualityStatus.INVALID, f"not real-time ({mdt or 'unknown'})")

    return (QualityStatus.READY, "real-time L1 available")
