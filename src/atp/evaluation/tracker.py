"""Prediction snapshotter + outcome tracker (§ Phase G3.1).

snapshot_prediction() records the EXACT AI state at prediction time (immutable — one per symbol per
hour; re-running never overwrites it). evaluate_outcomes() measures the forward return from real OHLC
once a horizon has elapsed and records it once (never overwritten, never removed). No prices available
→ nothing is evaluated (NO DATA); nothing is fabricated. No execution / broker / IBKR access.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

HORIZONS = [1, 3, 5, 20]     # trading-intelligence evaluation windows, in days


def _latest_close(store, symbol: str) -> float | None:
    bars = store.list_ohlc_bars(symbol, "1D", 5) or store.list_ohlc_bars(symbol, "1m", 5)
    return float(bars[-1].close) if bars else None


def snapshot_prediction(store, symbol: str, assessment: dict, now: datetime | None = None) -> bool:
    """Append an immutable snapshot of the current AI view. Skips NO DATA. One row per symbol per hour."""
    if assessment.get("status") == "NO DATA" or assessment.get("score") is None:
        return False
    now = now or datetime.now(timezone.utc)
    pid = f"{symbol.upper()}:{now.strftime('%Y-%m-%dT%H')}"
    store.insert_ai_prediction(
        id=pid, symbol=symbol, timestamp=now.isoformat(), score=assessment["score"],
        direction=assessment["direction"], confidence=assessment["confidence"], status=assessment["status"],
        price_at_prediction=_latest_close(store, symbol),
        components_snapshot=json.dumps({
            "components": assessment.get("components", []), "conflicts": assessment.get("conflicts", []),
            "strengths": assessment.get("strengths", []), "risks": assessment.get("risks", [])}))
    return True


def _price_on_or_after(store, symbol: str, target_date_iso: str) -> float | None:
    """Close of the first daily bar on/after a target date (ISO-8601 prefix compare). None if not there yet."""
    for b in store.list_ohlc_bars(symbol, "1D", 500):   # oldest → newest
        if b.ts >= target_date_iso:
            return float(b.close)
    return None


def direction_correct(direction: str | None, ret_pct: float) -> bool | None:
    """A BULLISH call is right if the market rose, BEARISH if it fell, NEUTRAL if it stayed within ±2%."""
    if direction == "BULLISH":
        return ret_pct > 0
    if direction == "BEARISH":
        return ret_pct < 0
    if direction == "NEUTRAL":
        return abs(ret_pct) <= 2.0
    return None


def evaluate_outcomes(store, now: datetime | None = None) -> int:
    """Measure + record every not-yet-evaluated (prediction, horizon) whose window has elapsed and whose
    forward price exists. Returns the number newly recorded. Immutable: never re-measures."""
    now = now or datetime.now(timezone.utc)
    done = {(o.prediction_id, o.time_horizon) for o in store.list_ai_prediction_outcomes()}
    recorded = 0
    for p in store.list_ai_predictions(None, 3000):
        if p.price_at_prediction in (None, 0) or not p.timestamp:
            continue
        try:
            t0 = datetime.fromisoformat(p.timestamp)
        except ValueError:
            continue
        for h in HORIZONS:
            if (p.id, h) in done:
                continue
            target = t0 + timedelta(days=h)
            if now < target:
                continue                                # window not elapsed yet
            fut = _price_on_or_after(store, p.symbol, target.date().isoformat())
            if fut is None:
                continue                                # no forward price yet → not evaluable (NO DATA)
            ret = (fut - p.price_at_prediction) / p.price_at_prediction * 100.0
            store.insert_ai_prediction_outcome(
                prediction_id=p.id, time_horizon=h, price_at_prediction=p.price_at_prediction,
                future_price=fut, return_percentage=round(ret, 3),
                direction_correct=direction_correct(p.direction, ret))
            recorded += 1
    return recorded
