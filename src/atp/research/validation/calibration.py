"""§ R3.1A — confidence calibration stance.

The consensus `confidence` is a heuristic 0-100 score, NOT a calibrated probability. Therefore Brier score,
log loss and Expected Calibration Error are reported as NOT APPLICABLE — never computed as if `confidence`
were P(direction). Descriptive accuracy per fixed confidence bucket lives in `metrics.by_confidence_bucket`.
A calibrated probability is deferred to a separately versioned future contract.
"""
from __future__ import annotations

PROBABILISTIC_METRICS = ("brier_score", "log_loss", "expected_calibration_error")
FUTURE_PROBABILITY_CONTRACT = "PROBABILITY_CONTRACT_V2 (not implemented in R3.1A)"


def probabilistic_calibration() -> dict:
    return {m: "NOT APPLICABLE" for m in PROBABILISTIC_METRICS} | {
        "confidence_is_probability": False,
        "reason": "confidence is a heuristic 0-100 score, not a calibrated probability",
        "future_contract": FUTURE_PROBABILITY_CONTRACT,
    }
