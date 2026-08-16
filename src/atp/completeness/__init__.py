"""Data Completeness Engine (§ Phase C1) — a read-only reliability / capital-protection layer.

Measures how COMPLETE GIGBAY's information is for a symbol across the 7 intelligence domains, before the
AI produces a high-confidence assessment. Deterministic 0-100 score + readiness state (READY / PARTIAL
/ INSUFFICIENT). It only MEASURES information quality — it never trades, generates orders, or touches
Trading Core / Risk Engine / Broker / IBKR / Execution. This is NOT `atp.dataquality` (§10, the market-
data NO-TRADE gate) — that is a trading gate; this is an intelligence-quality read-model.
"""

from .engine import (
    DOMAIN_LABELS,
    WEIGHTS,
    compute_completeness,
    readiness_state,
    record_completeness,
    snapshot_completeness,
)

__all__ = [
    "compute_completeness",
    "snapshot_completeness",
    "record_completeness",
    "readiness_state",
    "WEIGHTS",
    "DOMAIN_LABELS",
]
