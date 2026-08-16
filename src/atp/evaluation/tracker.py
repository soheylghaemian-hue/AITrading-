"""Prediction snapshotter + outcome tracker (§ Phase G3.1).

snapshot_prediction() records the EXACT AI state at prediction time (immutable — one per symbol per
hour; re-running never overwrites it). evaluate_outcomes() measures the forward return from real OHLC
once a horizon has elapsed and records it once (never overwritten, never removed). No prices available
→ nothing is evaluated (NO DATA); nothing is fabricated. No execution / broker / IBKR access.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

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


def direction_correct(direction: str | None, ret_pct: float) -> bool | None:
    """A BULLISH call is right if the market rose, BEARISH if it fell, NEUTRAL if it stayed within ±2%."""
    if direction == "BULLISH":
        return ret_pct > 0
    if direction == "BEARISH":
        return ret_pct < 0
    if direction == "NEUTRAL":
        return abs(ret_pct) <= 2.0
    return None


def direction_actual(ret_pct: float) -> str:
    """The market's realised direction over the horizon (by sign of the forward return)."""
    return "BULLISH" if ret_pct > 0 else "BEARISH" if ret_pct < 0 else "NEUTRAL"


def _pred_bar_index(bars, prediction_ts: str) -> int | None:
    """Index of the first daily bar on/after the prediction date (ISO-8601 prefix compare)."""
    pdate = prediction_ts[:10]
    for i, b in enumerate(bars):
        if b.ts >= pdate:
            return i
    return None


def evaluate_outcomes(store, now: datetime | None = None) -> int:
    """Measure + record every not-yet-evaluated (prediction, horizon). The horizon is counted in TRADING
    DAYS (daily OHLC bars): the forward price is the close N bars after the prediction's bar. If that bar
    does not exist yet the outcome stays PENDING (no row). Returns the number newly recorded. Immutable:
    never re-measures. Uses ONLY OHLC — never broker/simulated/manual prices."""
    done = {(o.prediction_id, o.time_horizon) for o in store.list_ai_prediction_outcomes()}
    bars_cache: dict[str, list] = {}
    recorded = 0
    for p in store.list_ai_predictions(None, 3000):
        if p.price_at_prediction in (None, 0) or not p.timestamp:
            continue
        sym = p.symbol
        if sym not in bars_cache:
            bars_cache[sym] = store.list_ohlc_bars(sym, "1D", 800)   # oldest → newest (trading days)
        bars = bars_cache[sym]
        idx = _pred_bar_index(bars, p.timestamp)
        if idx is None:
            continue
        for h in HORIZONS:
            if (p.id, h) in done or idx + h >= len(bars):
                continue                                # not enough trading days elapsed → PENDING
            fut = float(bars[idx + h].close)
            ret = (fut - p.price_at_prediction) / p.price_at_prediction * 100.0
            store.insert_ai_prediction_outcome(
                prediction_id=p.id, time_horizon=h, price_at_prediction=p.price_at_prediction,
                future_price=fut, return_percentage=round(ret, 3),
                direction_correct=direction_correct(p.direction, ret),
                direction_expected=p.direction, direction_actual=direction_actual(ret), status="EVALUATED")
            recorded += 1
    return recorded
