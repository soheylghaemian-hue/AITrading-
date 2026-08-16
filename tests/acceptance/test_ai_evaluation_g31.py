"""Phase G3.1 — AI Evaluation & Performance Tracking (read-only, honest).

Covers: prediction persistence, snapshot IMMUTABILITY, outcome calculation, accuracy calculation,
calibration, missing-data handling, error classification, and no-execution side effects. Touches no
Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.evaluation.metrics import build_ai_history, compute_performance
from atp.evaluation.tracker import evaluate_outcomes, snapshot_prediction
from atp.store import open_store

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates ai_prediction* (migration 9)


def assessment(direction="BULLISH", score=85, confidence=85, status="COMPLETE", conflicts=None):
    return {"symbol": "NVDA", "score": score, "direction": direction, "confidence": confidence,
            "status": status, "coverage": 1.0,
            "components": [{"component_name": "Fundamentals", "score": 93, "weight": 0.2,
                            "direction": "bullish", "reason": "q93", "risk_flags": []}],
            "strengths": ["Revenue growth"], "risks": ["High valuation"], "conflicts": conflicts or []}


def seed_bars(store, sym, start, days, base=100.0, step=0.0):
    for i in range(days):
        d = (start + timedelta(days=i)).date().isoformat()
        c = base + i * step
        store.upsert_ohlc_bar(symbol=sym, interval="1D", ts=f"{d}T00:00:00+00:00",
                              open=c, high=c, low=c, close=c, volume=1000, source="TEST")


def test_prediction_persistence_and_snapshot_immutability(store):
    seed_bars(store, "NVDA", T0 - timedelta(days=3), 4, base=100.0, step=0.0)   # latest close = 100
    assert snapshot_prediction(store, "NVDA", assessment(score=85), now=T0) is True
    assert store.count_ai_predictions() == 1
    p = store.list_ai_predictions("NVDA")[0]
    assert p.score == 85 and p.direction == "BULLISH" and p.price_at_prediction == 100.0
    # re-snapshot the SAME hour with a DIFFERENT score → history must NOT change (immutable)
    snapshot_prediction(store, "NVDA", assessment(score=50, direction="BEARISH"), now=T0)
    assert store.count_ai_predictions() == 1
    assert store.list_ai_predictions("NVDA")[0].score == 85          # original preserved, never rewritten


def test_outcome_calculation_and_accuracy(store):
    seed_bars(store, "NVDA", T0 - timedelta(days=1), 2, base=100.0)              # price@prediction = 100
    snapshot_prediction(store, "NVDA", assessment(direction="BULLISH", score=85, confidence=85), now=T0)
    seed_bars(store, "NVDA", T0, 25, base=100.0, step=0.84)                      # rising: +4.2% by day 5
    n = evaluate_outcomes(store, now=T0 + timedelta(days=25))
    assert n >= 3                                                                # 1/3/5/20-day windows
    o5 = next(o for o in store.list_ai_prediction_outcomes() if o.time_horizon == 5)
    assert o5.return_percentage == pytest.approx(4.2, abs=0.2)                   # ≈ +4.2% after 5 days
    assert o5.direction_correct is True                                         # bullish call → market rose

    perf = compute_performance(store, horizon=5)
    assert perf["sample_size"] == 1
    assert perf["direction_accuracy"] == 100.0 and perf["bullish_accuracy"] == 100.0
    assert perf["average_return"] == pytest.approx(4.2, abs=0.2)


def test_false_bullish_error_classification(store):
    seed_bars(store, "NVDA", T0 - timedelta(days=1), 2, base=100.0)
    snapshot_prediction(store, "NVDA", assessment(direction="BULLISH", score=85, confidence=85), now=T0)
    seed_bars(store, "NVDA", T0, 25, base=100.0, step=-0.84)                     # market FALLS
    evaluate_outcomes(store, now=T0 + timedelta(days=25))
    perf = compute_performance(store, horizon=5)
    assert perf["direction_accuracy"] == 0.0
    assert perf["errors"].get("FALSE BULLISH") == 1                             # bullish call, market negative


def test_calibration_buckets(store):
    # two high-confidence predictions; one right, one wrong → success rate 50% vs ~90% confidence
    for i, (dirn, step, conf) in enumerate([("BULLISH", 0.84, 90), ("BULLISH", -0.84, 88)]):
        sym = f"S{i}"
        seed_bars(store, sym, T0 - timedelta(days=1), 2, base=100.0)
        a = assessment(direction=dirn, score=85, confidence=conf)
        a["symbol"] = sym
        snapshot_prediction(store, sym, a, now=T0)
        seed_bars(store, sym, T0, 25, base=100.0, step=step)
    evaluate_outcomes(store, now=T0 + timedelta(days=25))
    perf = compute_performance(store, horizon=5)
    calib = perf["confidence_calibration"]
    assert calib["high"]["count"] == 2 and calib["high"]["success_rate"] == 50.0
    assert calib["verdict"] == "Overconfident"                                  # 89% confidence, 50% realised


def test_missing_data_is_no_data(store):
    perf = compute_performance(store, horizon=5)
    assert perf["sample_size"] == 0 and perf["direction_accuracy"] is None and perf["average_return"] is None
    hist = build_ai_history(store, "NVDA")
    assert hist["count"] == 0 and hist["assessments"] == []
    # a prediction with no forward price is not evaluated (NO DATA), never fabricated
    seed_bars(store, "NVDA", T0 - timedelta(days=1), 2, base=100.0)
    snapshot_prediction(store, "NVDA", assessment(), now=T0)
    assert evaluate_outcomes(store, now=T0 + timedelta(hours=1)) == 0           # horizon not elapsed
    assert compute_performance(store, 5)["sample_size"] == 0


def test_history_with_outcomes(store):
    seed_bars(store, "NVDA", T0 - timedelta(days=1), 2, base=100.0)
    snapshot_prediction(store, "NVDA", assessment(score=83), now=T0)
    seed_bars(store, "NVDA", T0, 25, base=100.0, step=0.62)
    evaluate_outcomes(store, now=T0 + timedelta(days=25))
    hist = build_ai_history(store, "NVDA")
    assert hist["count"] == 1
    a = hist["assessments"][0]
    assert a["direction"] == "BULLISH" and a["score"] == 83
    o5 = next(o for o in a["outcomes"] if o["time_horizon"] == 5)
    assert o5["return_percentage"] is not None and o5["direction_correct"] is True


def test_no_execution_side_effects():
    pkg = Path(__file__).resolve().parents[2] / "src" / "atp" / "evaluation"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(")
    for f in pkg.glob("*.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
