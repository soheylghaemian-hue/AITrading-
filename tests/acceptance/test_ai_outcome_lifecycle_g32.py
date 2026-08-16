"""Phase G3.2 — Outcome Lifecycle Controller (read-only, evaluation only).

Covers: prediction → outcome lifecycle, 1/3/5/20 trading-day calculation, immutable outcomes, missing
OHLC → PENDING, confusion-matrix classification, per-horizon accuracy, scheduler restart-safety, and
no-execution side effects. Touches no Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.evaluation.metrics import classify_outcome, compute_outcomes_summary, compute_performance
from atp.evaluation.tracker import evaluate_outcomes
from atp.store import open_store

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates the outcome-lifecycle columns (migration 10)


def seed_bars(store, sym, start, days, base, step):
    for i in range(days):
        d = (start + timedelta(days=i)).date().isoformat()
        c = base + i * step
        store.upsert_ohlc_bar(symbol=sym, interval="1D", ts=f"{d}T00:00:00+00:00",
                              open=c, high=c, low=c, close=c, volume=1000, source="TEST")


def make_pred(store, sym, t0, direction, score, confidence, price, status="COMPLETE", conflicts=None):
    pid = f"{sym}:{t0.strftime('%Y-%m-%dT%H')}"
    store.insert_ai_prediction(
        id=pid, symbol=sym, timestamp=t0.isoformat(), score=score, direction=direction, confidence=confidence,
        status=status, price_at_prediction=price,
        components_snapshot=json.dumps({"components": [{"component_name": "Fundamentals", "score": 90}],
                                        "conflicts": conflicts or []}))
    return pid


def test_full_outcome_lifecycle_true_positive(store):
    # NVDA BULLISH @ 225 → +3/trading-day → 240 after 5 days = +6.6% = CORRECT / TRUE POSITIVE.
    seed_bars(store, "NVDA", T0, 25, base=225.0, step=3.0)
    pid = make_pred(store, "NVDA", T0, "BULLISH", 82, 82, price=225.0)
    assert evaluate_outcomes(store) == 4                                     # 1/3/5/20-day windows
    outs = {o.time_horizon: o for o in store.list_ai_prediction_outcomes(pid)}
    assert set(outs) == {1, 3, 5, 20}
    o5 = outs[5]
    assert o5.future_price == 240.0
    assert o5.return_percentage == pytest.approx(6.667, abs=0.01)
    assert o5.direction_correct is True
    assert o5.direction_expected == "BULLISH" and o5.direction_actual == "BULLISH" and o5.status == "EVALUATED"
    assert classify_outcome(store.get_ai_prediction(pid), o5) == "TRUE POSITIVE"


def test_horizons_use_trading_days(store):
    seed_bars(store, "NVDA", T0, 25, base=100.0, step=1.0)                   # +1/trading-day
    pid = make_pred(store, "NVDA", T0, "BULLISH", 80, 80, price=100.0)
    evaluate_outcomes(store)
    outs = {o.time_horizon: o.future_price for o in store.list_ai_prediction_outcomes(pid)}
    assert outs[1] == 101.0 and outs[3] == 103.0 and outs[5] == 105.0 and outs[20] == 120.0


def test_outcomes_immutable_and_restart_safe(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    seed_bars(s1, "NVDA", T0, 25, base=225.0, step=3.0)
    make_pred(s1, "NVDA", T0, "BULLISH", 82, 82, price=225.0)
    assert evaluate_outcomes(s1) == 4
    assert evaluate_outcomes(s1) == 0                                        # immutable — never re-measured
    s1.close()
    s2 = open_store(path)                                                    # "restart"
    assert s2.count_ai_prediction_outcomes() == 4
    assert evaluate_outcomes(s2) == 0                                        # restart-safe, no rewrite


def test_confusion_matrix_classification(store):
    seed_bars(store, "NVDA", T0, 25, base=225.0, step=-3.0)                  # market FALLS
    pid = make_pred(store, "NVDA", T0, "BULLISH", 82, 82, price=225.0)       # bullish + falling
    seed_bars(store, "AAPL", T0, 25, base=200.0, step=-2.0)
    pidb = make_pred(store, "AAPL", T0, "BEARISH", 70, 70, price=200.0)      # bearish + falling
    seed_bars(store, "SPY", T0, 25, base=500.0, step=2.0)
    pidn = make_pred(store, "SPY", T0, "BEARISH", 65, 65, price=500.0)       # bearish + rising
    evaluate_outcomes(store)
    o = lambda pid: next(x for x in store.list_ai_prediction_outcomes(pid) if x.time_horizon == 5)  # noqa: E731
    assert classify_outcome(store.get_ai_prediction(pid), o(pid)) == "FALSE POSITIVE"
    assert classify_outcome(store.get_ai_prediction(pidb), o(pidb)) == "TRUE NEGATIVE"
    assert classify_outcome(store.get_ai_prediction(pidn), o(pidn)) == "FALSE NEGATIVE"


def test_missing_ohlc_stays_pending(store):
    make_pred(store, "NVDA", T0, "BULLISH", 82, 82, price=225.0)            # no OHLC seeded
    assert evaluate_outcomes(store) == 0                                    # no forward price → PENDING
    assert store.list_ai_prediction_outcomes() == []
    s = compute_outcomes_summary(store)
    assert s["prediction_count"] == 1 and s["evaluated_count"] == 0 and s["pending_count"] == 4
    assert s["accuracy"] is None                                           # NO DATA, never fabricated


def test_outcomes_summary_and_by_horizon(store):
    seed_bars(store, "NVDA", T0, 25, base=225.0, step=3.0)
    make_pred(store, "NVDA", T0, "BULLISH", 82, 82, price=225.0)
    evaluate_outcomes(store)
    s = compute_outcomes_summary(store)
    assert s["prediction_count"] == 1 and s["evaluated_count"] == 4 and s["pending_count"] == 0
    assert s["accuracy"] == 100.0 and s["classification"].get("TRUE POSITIVE") == 1
    bh = compute_performance(store, 5)["by_horizon"]
    assert bh["1"]["sample_size"] == 1 and bh["5"]["accuracy"] == 100.0
    assert bh["20"]["average_return"] is not None


def test_no_execution_side_effects():
    files = list((Path(__file__).resolve().parents[2] / "src" / "atp" / "evaluation").glob("*.py"))
    files.append(Path(__file__).resolve().parents[2] / "src" / "atp" / "services" / "ai_outcome_tracker.py")
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(")
    for f in files:
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
