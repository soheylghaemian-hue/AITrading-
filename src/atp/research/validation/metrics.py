"""§ R3.1A — deterministic prediction-quality metrics (RESEARCH ONLY, separate from trading P&L).

Pure functions over a frozen list of normalized samples
    {symbol, session_date, horizon, expected, actual, correct, confidence, regime, return_pct}
Computes coverage, effective (session-clustered) sample counts, directional accuracy, confusion matrix,
per-class precision/recall/F1, and slices by horizon / symbol / regime / confidence bucket. NEVER labels
the heuristic confidence as a probability. No optimization, no parameter search.
"""
from __future__ import annotations

from ..intel import policy

_CLASSES = ("BULLISH", "BEARISH", "NEUTRAL")


def _rate(n, d):
    return round(n / d, 4) if d else None


def _confusion(samples) -> dict:
    graded = [s for s in samples if s.get("expected") in _CLASSES and s.get("correct") is not None]
    matrix = {e: {a: 0 for a in _CLASSES} for e in _CLASSES}
    for s in graded:
        matrix[s["expected"]][s["actual"]] += 1
    per_class = {}
    for c in _CLASSES:
        tp = matrix[c][c]
        fp = sum(matrix[e][c] for e in _CLASSES if e != c)
        fn = sum(matrix[c][a] for a in _CLASSES if a != c)
        prec, rec = _rate(tp, tp + fp), _rate(tp, tp + fn)
        f1 = round(2 * prec * rec / (prec + rec), 4) if (prec and rec) else None
        per_class[c] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}
    correct = sum(1 for s in graded if s["correct"])
    return {"graded": len(graded), "accuracy": _rate(correct, len(graded)),
            "matrix": matrix, "per_class": per_class}


def _accuracy_block(samples) -> dict:
    b = _confusion(samples)
    return {"graded": b["graded"], "accuracy": b["accuracy"]}


def coverage(snapshots, outcomes) -> dict:
    """Separates the distinct evidence classes (§ correction 6): captured snapshots, matured price outcomes,
    GRADED prediction outcomes (a real directional call verified), abstentions (NO-DATA/ABSTAIN), failures.
    Effective samples per horizon count UNIQUE DECISION SESSIONS (not symbol×session snapshots) — three
    symbols from one session are correlated observations of ONE effective session cluster."""
    sess_of = {s.snapshot_id: s.decision_session_date for s in snapshots}
    matured = [o for o in outcomes if o.status == "MATURED"]
    sessions = {s.decision_session_date for s in snapshots}
    symbols = {s.symbol for s in snapshots}
    by_h = {}
    for h in policy.HORIZONS:
        mh = [o for o in matured if o.horizon_sessions == h]
        graded = [o for o in mh if o.direction_correct is not None and o.classification != "ABSTAIN"]
        abstained = [o for o in mh if o.classification == "ABSTAIN" or o.direction_correct is None]
        fh = [o for o in outcomes if o.status == "FAILED" and o.horizon_sessions == h]
        by_h[str(h)] = {
            "matured": len(mh), "graded": len(graded), "abstained": len(abstained), "failed": len(fh),
            # effective = unique DECISION SESSIONS with a graded outcome at this horizon (session-clustered)
            "effective_graded_sessions": len({sess_of.get(o.snapshot_id) for o in graded}),
        }
    return {"raw_snapshot_count": len(snapshots), "captured_snapshots": len(snapshots),
            "effective_canonical_sessions": len(sessions), "unique_symbols": len(symbols),
            "symbols": sorted(symbols), "matured_total": len(matured),
            "graded_total": sum(v["graded"] for v in by_h.values()),
            "abstained_total": sum(v["abstained"] for v in by_h.values()),
            "failed_total": len([o for o in outcomes if o.status == "FAILED"]),
            "by_horizon": by_h}


def compute(samples: list[dict]) -> dict:
    """Full metric block from normalized samples. Probabilistic calibration is NOT APPLICABLE (heuristic
    confidence). Naming: `exposure` metrics are absent here — this is prediction quality, not trading."""
    by_horizon = {}
    for h in policy.HORIZONS:
        sh = [s for s in samples if s["horizon"] == h]
        by_horizon[str(h)] = {**_confusion(sh), "n": len(sh)}
    by_symbol = {}
    for sym in sorted({s["symbol"] for s in samples}):
        by_symbol[sym] = _accuracy_block([s for s in samples if s["symbol"] == sym])
    by_regime = {}
    for rg in sorted({s.get("regime") or "UNKNOWN" for s in samples}):
        by_regime[rg] = _accuracy_block([s for s in samples if (s.get("regime") or "UNKNOWN") == rg])
    by_bucket = {}
    for lo, hi in policy.CONFIDENCE_BUCKETS:
        label = f"[{int(lo)},{int(hi)})"
        sb = [s for s in samples if s.get("confidence") is not None and lo <= s["confidence"] < hi]
        by_bucket[label] = {**_accuracy_block(sb), "confidence_is_probability": False}
    return {
        "overall": _confusion(samples),
        "by_horizon": by_horizon, "by_symbol": by_symbol, "by_regime": by_regime,
        "by_confidence_bucket": by_bucket,
        "probabilistic_calibration": "NOT APPLICABLE",
        "probabilistic_reason": "confidence is a heuristic 0-100 score, not a calibrated probability",
    }
