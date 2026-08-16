"""Trader Quality Engine (§ Phase G2.5) — deterministic 0-100 score. PURE, testable, no randomness.

Weighted components (per spec):
    Return Quality           20%   — reward positive return, diminishing (a huge return is not "free")
    Risk-Adjusted Perf.      25%   — Sharpe → Sortino → Calmar (return / |max drawdown|)
    Maximum Drawdown         25%   — lower |drawdown| is better; a deep drawdown dominates the score
    Consistency              15%   — win rate
    Track Record             15%   — longer verified history is better (diminishing)

Only components with real data contribute; missing components are dropped and the weights renormalize
(never a fabricated sub-score). No performance data at all → None (NO DATA). This is why a trader with
+100% return but -60% drawdown scores far BELOW a trader with +35% return and -8% drawdown — exactly
the behaviour the AI Brain should weight by.
"""
from __future__ import annotations

import math

WEIGHTS = {"return": 0.20, "risk_adjusted": 0.25, "drawdown": 0.25, "consistency": 0.15, "track": 0.15}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _return_quality(ret: float | None) -> float | None:
    if ret is None:
        return None
    if ret <= 0:
        return 0.0
    return round(_clamp(100.0 * (1.0 - math.exp(-ret / 0.25))), 2)   # +35%→~75, +100%→~98


def _risk_adjusted(sharpe, sortino, ret, dd) -> float | None:
    if sharpe is not None:
        return round(_clamp(100.0 * sharpe / 2.5), 2)                # Sharpe 2.5 → 100
    if sortino is not None:
        return round(_clamp(100.0 * sortino / 3.5), 2)
    d = abs(dd) if dd is not None else None
    if ret is not None and d and d > 0:
        return round(_clamp(100.0 * (ret / d) / 3.0), 2)            # Calmar 3 → 100
    return None


def _drawdown_score(dd: float | None) -> float | None:
    if dd is None:
        return None
    return round(_clamp(100.0 * (1.0 - abs(dd) / 0.5)), 2)           # |dd|≥50%→0, 0%→100


def _consistency(win_rate: float | None) -> float | None:
    if win_rate is None:
        return None
    return round(_clamp(100.0 * win_rate), 2)


def _track_record(days: int | None) -> float | None:
    if days is None:
        return None
    return round(_clamp(100.0 * days / 730.0), 2)                    # 2 years → 100


def quality_breakdown(perf, track_record_days: int | None = None) -> dict[str, float | None]:
    """Per-component sub-scores (each 0-100 or None). `perf` is a TraderPerformance/Row-like object."""
    if perf is None:
        return {k: None for k in WEIGHTS}
    ret = perf.annualized_return if perf.annualized_return is not None else perf.total_return
    return {
        "return": _return_quality(ret),
        "risk_adjusted": _risk_adjusted(perf.sharpe_ratio, perf.sortino_ratio, ret, perf.max_drawdown),
        "drawdown": _drawdown_score(perf.max_drawdown),
        "consistency": _consistency(perf.win_rate),
        "track": _track_record(track_record_days),
    }


def quality_score(perf, track_record_days: int | None = None) -> float | None:
    """Overall 0-100 quality score — weighted mean over the components that have real data. None when
    no performance data exists (NO DATA)."""
    if perf is None:
        return None
    subs = quality_breakdown(perf, track_record_days)
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = subs.get(k)
        if v is not None:
            num += w * v
            den += w
    if den == 0:
        return None
    return round(num / den, 1)
