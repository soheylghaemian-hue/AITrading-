"""AI performance metrics + error analysis (§ Phase G3.1) — PURE (reads persisted history only).

Deterministic: accuracy, directional accuracy, bullish/bearish accuracy, average forward return,
confidence calibration (high/medium/low buckets vs realised success), score reliability, and mistake
classification (FALSE BULLISH / FALSE BEARISH / INSUFFICIENT DATA / CONFLICT FAILURE). Zero evaluated
outcomes → sample_size 0 and every metric NO DATA. Nothing is fabricated.
"""
from __future__ import annotations

import json
from collections import defaultdict

CONF_BUCKETS = [("high", 80, 101), ("medium", 60, 80), ("low", 0, 60)]


def _snapshot(pred) -> dict:
    try:
        return json.loads(pred.components_snapshot) if pred.components_snapshot else {}
    except (ValueError, TypeError):
        return {}


def classify_error(pred, outcome) -> str | None:
    """Mistake label for a wrong/undata prediction, else None (correct or not evaluable)."""
    if pred.status in ("PARTIAL", "NO DATA"):
        return "INSUFFICIENT DATA"
    if outcome is None or outcome.direction_correct is None or outcome.direction_correct:
        return None
    if _snapshot(pred).get("conflicts"):
        return "CONFLICT FAILURE"
    if pred.direction == "BULLISH":
        return "FALSE BULLISH"
    if pred.direction == "BEARISH":
        return "FALSE BEARISH"
    return "WRONG NEUTRAL"


def _calibration(pairs) -> dict:
    out: dict = {}
    gap = 0.0
    seen = 0
    for name, lo, hi in CONF_BUCKETS:
        b = [(p, o) for p, o in pairs if lo <= (p.confidence or 0) < hi]
        if not b:
            out[name] = {"count": 0, "success_rate": None, "avg_confidence": None}
            continue
        sr = sum(1 for _, o in b if o.direction_correct) / len(b)
        ac = sum((p.confidence or 0) for p, _ in b) / len(b)
        out[name] = {"count": len(b), "success_rate": round(sr * 100, 1), "avg_confidence": round(ac, 1)}
        gap += ac / 100 - sr
        seen += 1
    g = gap / seen if seen else 0.0
    out["verdict"] = ("Overconfident" if g > 0.1 else "Underconfident" if g < -0.1 else "Good") if seen else None
    return out


def _score_reliability(pairs) -> dict:
    hi = [(p, o) for p, o in pairs if (p.score or 0) >= 70]
    lo = [(p, o) for p, o in pairs if (p.score or 0) < 70]
    acc = lambda xs: round(sum(1 for _, o in xs if o.direction_correct) / len(xs) * 100, 1) if xs else None  # noqa: E731
    return {"high_score_accuracy": acc(hi), "low_score_accuracy": acc(lo)}


def _input_analysis(pairs) -> tuple[list[str], list[str]]:
    hit: dict[str, int] = defaultdict(int)
    tot: dict[str, int] = defaultdict(int)
    for p, o in pairs:
        for c in _snapshot(p).get("components", []):
            name = c.get("component_name")
            if name and (c.get("score") or 0) >= 60:
                tot[name] += 1
                if o.direction_correct:
                    hit[name] += 1
    accs = {n: hit[n] / tot[n] for n in tot if tot[n] >= 2}   # require ≥2 samples to rank an input
    ranked = sorted(accs.items(), key=lambda kv: kv[1], reverse=True)
    best = [n for n, _ in ranked[:2]]
    weakest = [n for n, _ in ranked[-2:]] if len(ranked) > 2 else []
    return best, weakest


def _empty(horizon):
    return {"sample_size": 0, "overall_accuracy": None, "direction_accuracy": None,
            "bullish_accuracy": None, "bearish_accuracy": None, "average_return": None,
            "confidence_calibration": None, "score_reliability": None, "horizon_days": horizon,
            "errors": {}, "best_inputs": [], "weakest_inputs": []}


def compute_performance(store, horizon: int = 5) -> dict:
    preds = {p.id: p for p in store.list_ai_predictions(None, 5000)}
    pairs = [(preds[o.prediction_id], o) for o in store.list_ai_prediction_outcomes()
             if o.time_horizon == horizon and o.prediction_id in preds and o.direction_correct is not None]
    n = len(pairs)
    if n == 0:
        return _empty(horizon)
    correct = sum(1 for _, o in pairs if o.direction_correct)
    dir_acc = round(correct / n * 100, 1)
    bulls = [(p, o) for p, o in pairs if p.direction == "BULLISH"]
    bears = [(p, o) for p, o in pairs if p.direction == "BEARISH"]
    acc = lambda xs: round(sum(1 for _, o in xs if o.direction_correct) / len(xs) * 100, 1) if xs else None  # noqa: E731

    def aligned(p, o):
        r = o.return_percentage or 0.0
        return r if p.direction == "BULLISH" else -r if p.direction == "BEARISH" else 0.0

    avg_return = round(sum(aligned(p, o) for p, o in pairs) / n, 2)
    errors: dict[str, int] = defaultdict(int)
    for p, o in pairs:
        e = classify_error(p, o)
        if e:
            errors[e] += 1
    best, weakest = _input_analysis(pairs)
    return {"sample_size": n, "overall_accuracy": dir_acc, "direction_accuracy": dir_acc,
            "bullish_accuracy": acc(bulls), "bearish_accuracy": acc(bears), "average_return": avg_return,
            "confidence_calibration": _calibration(pairs), "score_reliability": _score_reliability(pairs),
            "horizon_days": horizon, "errors": dict(errors), "best_inputs": best, "weakest_inputs": weakest}


def build_ai_history(store, symbol: str, limit: int = 50) -> dict:
    out = []
    for p in store.list_ai_predictions(symbol, limit):
        outs = sorted(store.list_ai_prediction_outcomes(p.id), key=lambda x: x.time_horizon)
        out.append({
            "id": p.id, "symbol": p.symbol, "timestamp": p.timestamp, "score": p.score,
            "direction": p.direction, "confidence": p.confidence, "status": p.status,
            "price_at_prediction": p.price_at_prediction,
            "outcomes": [{"time_horizon": o.time_horizon, "future_price": o.future_price,
                          "return_percentage": o.return_percentage, "direction_correct": o.direction_correct}
                         for o in outs],
        })
    return {"symbol": symbol.upper(), "count": len(out), "assessments": out}
