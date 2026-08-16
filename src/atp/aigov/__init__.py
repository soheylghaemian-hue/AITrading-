"""AI Decision Governance (§ Phase G3.3) — deterministic decision-quality/readiness layer.

This is NOT model governance (that's `atp.governance`). It decides whether an AI ASSESSMENT is
APPROVED / PARTIAL / CONFLICT / BLOCKED. It performs NO trading, NO order/broker/IBKR/execution, and
holds no credentials — it only evaluates whether a decision is trustworthy enough to proceed.
"""

from .engine import (
    APPROVE_COMPLETENESS,
    APPROVE_CONFIDENCE,
    APPROVE_SCORE,
    assessment_from_prediction,
    build_governance_feed,
    evaluate_governance,
    record_governance,
)

__all__ = [
    "evaluate_governance",
    "assessment_from_prediction",
    "record_governance",
    "build_governance_feed",
    "APPROVE_SCORE",
    "APPROVE_CONFIDENCE",
    "APPROVE_COMPLETENESS",
]
