"""Phase G3.3 — AI Decision Governance Layer (read-only, evaluation only).

Covers: APPROVED / PARTIAL / CONFLICT / BLOCKED states, missing-data handling, data-completeness
computation, immutable governance history (never rewritten), Prediction→Governance→Outcome feed
integration, governance embedded in AI history, and NO execution side effects. Touches no Trading Core
/ Risk Engine / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.aigov.engine import (
    assessment_from_prediction,
    build_governance_feed,
    evaluate_governance,
    record_governance,
)
from atp.evaluation.metrics import build_ai_history
from atp.evaluation.tracker import evaluate_outcomes
from atp.store import open_store

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

# Consensus weighting (mirrors atp.consensus.engine.WEIGHTS) — used to build realistic components.
W = {"Market Data": 0.20, "News": 0.15, "Fundamentals": 0.20, "Options": 0.15,
     "Trader Intelligence": 0.15, "Risk": 0.15}


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 11 (ai_governance_results)


def comp(name, score, direction="neutral", risk_flags=None):
    return {"component_name": name, "score": score, "weight": W[name],
            "direction": direction, "risk_flags": risk_flags or []}


def assess(score, confidence, comps, *, status="COMPLETE", direction="BULLISH", symbol="NVDA"):
    return {"symbol": symbol, "score": score, "confidence": confidence, "status": status,
            "direction": direction, "components": comps, "conflicts": []}


def make_pred(store, sym, t0, direction, score, confidence, price, comps, status="COMPLETE"):
    pid = f"{sym}:{t0.strftime('%Y-%m-%dT%H')}"
    store.insert_ai_prediction(
        id=pid, symbol=sym, timestamp=t0.isoformat(), score=score, direction=direction,
        confidence=confidence, status=status, price_at_prediction=price,
        components_snapshot=json.dumps({"components": comps, "conflicts": []}))
    return pid


# ------------------------------------------------------------------ the four governance states
def test_approved_state():
    g = evaluate_governance(assess(88, 82, [
        comp("Fundamentals", 88, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish"),
        comp("Market Data", 80, "bullish"), comp("Trader Intelligence", 75, "neutral"), comp("Risk", 90, "neutral")]))
    assert g["status"] == "APPROVED"
    assert g["approved"] is True
    assert g["data_completeness"] == 100.0
    assert g["reasons"] == []
    assert g["missing"] == []


def test_partial_missing_options():
    # Fundamentals + News present and strong, but Options is NO DATA → PARTIAL (not APPROVED).
    g = evaluate_governance(assess(84, 76, [
        comp("Fundamentals", 93, "bullish"), comp("News", 85, "bullish"),
        comp("Market Data", 78, "bullish"), comp("Trader Intelligence", 70, "neutral"), comp("Risk", 88, "neutral")]))
    assert g["status"] == "PARTIAL"
    assert g["approved"] is False
    assert "MISSING_OPTIONS" in g["reasons"]
    assert "Options" in g["missing"]
    assert g["data_completeness"] == 85.0                    # everything except Options (weight 0.15)


def test_conflict_detection():
    # Fundamentals >80 bullish vs Options >80 bearish → CONFLICT.
    g = evaluate_governance(assess(70, 70, [
        comp("Fundamentals", 88, "bullish"), comp("Options", 85, "bearish"), comp("News", 82, "bullish")]))
    assert g["status"] == "CONFLICT"
    assert g["reasons"] == ["SOURCE_CONFLICT"]
    assert any("bullish vs" in c for c in g["conflicts"])
    assert g["approved"] is False


def test_blocked_low_confidence():
    g = evaluate_governance(assess(80, 42, [
        comp("Fundamentals", 90, "bullish"), comp("News", 80, "bullish"), comp("Options", 78, "bullish")]))
    assert g["status"] == "BLOCKED"
    assert "LOW_CONFIDENCE" in g["reasons"]


def test_blocked_insufficient_data():
    # No score / NO DATA → BLOCKED.
    g = evaluate_governance({"symbol": "NVDA", "score": None, "confidence": None,
                             "status": "NO DATA", "components": []})
    assert g["status"] == "BLOCKED"
    assert g["reasons"] == ["INSUFFICIENT_DATA"]
    # A single source (completeness 20% < 40%) → BLOCKED too.
    g2 = evaluate_governance(assess(85, 60, [comp("Fundamentals", 90, "bullish")]))
    assert g2["status"] == "BLOCKED"
    assert "INSUFFICIENT_DATA" in g2["reasons"]


def test_blocked_risk_failure():
    g = evaluate_governance(assess(85, 80, [
        comp("Fundamentals", 90, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish"),
        comp("Risk", 5, "neutral", risk_flags=["Risk engine halted"])]))
    assert g["status"] == "BLOCKED"
    assert "RISK_BLOCK" in g["reasons"]


def test_partial_low_completeness():
    # All three critical sources present, score/confidence fine, but only 50% of the weighting has data.
    g = evaluate_governance(assess(84, 74, [
        comp("Fundamentals", 90, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish")]))
    assert g["status"] == "PARTIAL"
    assert g["data_completeness"] == 50.0
    assert "LOW_COMPLETENESS" in g["reasons"]


def test_conflict_surfaces_even_when_confidence_dampened():
    # The consensus dampens confidence when sources disagree, so a real conflict often sits below the
    # confidence floor. CONFLICT must still be surfaced (it is WHY confidence is low) — not hidden as
    # a generic BLOCKED. Only a HARD block (no data / risk / <40% coverage) outranks it.
    g = evaluate_governance(assess(70, 40, [
        comp("Fundamentals", 88, "bullish"), comp("Options", 85, "bearish"), comp("News", 82, "bullish")]))
    assert g["status"] == "CONFLICT"


def test_hard_block_outranks_conflict():
    # A risk failure is a HARD block and outranks a conflict (safety first).
    g = evaluate_governance(assess(70, 70, [
        comp("Fundamentals", 88, "bullish"), comp("Options", 85, "bearish"), comp("News", 82, "bullish"),
        comp("Risk", 5, "neutral", risk_flags=["Risk engine halted"])]))
    assert g["status"] == "BLOCKED"
    assert "RISK_BLOCK" in g["reasons"]


# ------------------------------------------------------------------ immutability + persistence
def test_immutable_governance_history(store):
    pid = make_pred(store, "NVDA", T0, "BULLISH", 84, 76, 200.0, [
        comp("Fundamentals", 93, "bullish"), comp("News", 85, "bullish")])
    assert record_governance(store) == 1                     # newly recorded
    v1 = store.get_governance_result(pid)
    assert v1 is not None and v1.status in ("APPROVED", "PARTIAL", "CONFLICT", "BLOCKED")
    # Re-running (e.g. a service restart) records nothing new and NEVER rewrites the old verdict.
    assert record_governance(store) == 0
    assert store.count_governance_results() == 1
    v2 = store.get_governance_result(pid)
    assert (v2.status, v2.created_at, v2.score) == (v1.status, v1.created_at, v1.score)


def test_scheduler_restart_safe(store):
    make_pred(store, "AAPL", T0, "BULLISH", 88, 82, 190.0, [
        comp("Fundamentals", 88, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish"),
        comp("Market Data", 80, "bullish"), comp("Trader Intelligence", 75, "neutral"), comp("Risk", 90, "neutral")])
    first = record_governance(store)
    # Simulate a fresh process against the same DB — no duplicate rows, no rewrites.
    again = record_governance(store)
    assert (first, again) == (1, 0)
    assert store.count_governance_results() == 1


def test_data_completeness_from_weights():
    g = evaluate_governance(assess(80, 70, [comp("Fundamentals", 80), comp("Options", 80)]))
    assert g["data_completeness"] == 35.0                    # 0.20 + 0.15


def test_assessment_from_prediction_roundtrip(store):
    pid = make_pred(store, "NVDA", T0, "BULLISH", 84, 76, 200.0, [
        comp("Fundamentals", 93, "bullish"), comp("News", 85, "bullish")])
    a = assessment_from_prediction(store.get_ai_prediction(pid))
    assert a["symbol"] == "NVDA" and a["score"] == 84 and a["confidence"] == 76
    assert {c["component_name"] for c in a["components"]} == {"Fundamentals", "News"}


# ------------------------------------------------------------------ feed + AI-history + outcome integration
def test_governance_feed_outcome_integration(store):
    # Prediction → Governance → Outcome, all read-only.
    for i in range(25):
        d = (T0 + timedelta(days=i)).date().isoformat()
        c = 200.0 + i * 3.0
        store.upsert_ohlc_bar(symbol="NVDA", interval="1D", ts=f"{d}T00:00:00+00:00",
                              open=c, high=c, low=c, close=c, volume=1000, source="TEST")
    pid = make_pred(store, "NVDA", T0, "BULLISH", 88, 82, 200.0, [
        comp("Fundamentals", 88, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish"),
        comp("Market Data", 80, "bullish"), comp("Trader Intelligence", 75, "neutral"), comp("Risk", 90, "neutral")])
    record_governance(store)
    evaluate_outcomes(store)                                 # § G3.2 measures the 5-day outcome
    feed = build_governance_feed(store, 50)
    assert feed["count"] == 1
    d0 = feed["decisions"][0]
    assert d0["prediction_id"] == pid
    assert d0["status"] == "APPROVED"
    assert d0["direction"] == "BULLISH"
    assert d0["outcome"] is not None and d0["outcome"]["time_horizon"] == 5
    assert feed["status_counts"].get("APPROVED") == 1


def test_ai_history_includes_governance(store):
    make_pred(store, "NVDA", T0, "BULLISH", 84, 76, 200.0, [
        comp("Fundamentals", 93, "bullish"), comp("News", 85, "bullish")])
    hist = build_ai_history(store, "NVDA")
    gov = hist["assessments"][0]["governance"]
    assert gov["status"] in ("APPROVED", "PARTIAL", "CONFLICT", "BLOCKED")
    assert "reasons" in gov and isinstance(gov["reasons"], list)
    assert gov["approved"] == (gov["status"] == "APPROVED")


# ------------------------------------------------------------------ security: no execution
def test_no_execution_side_effects(store):
    # Recording governance must never create orders / positions / fills or touch execution.
    make_pred(store, "NVDA", T0, "BULLISH", 88, 82, 200.0, [
        comp("Fundamentals", 88, "bullish"), comp("News", 85, "bullish"), comp("Options", 84, "bullish")])
    record_governance(store)
    assert store.list_positions() == []
    assert store.list_fills() == []


def test_governance_source_has_no_broker_tokens():
    root = Path(__file__).resolve().parents[2] / "src" / "atp"
    files = list((root / "aigov").glob("*.py")) + [root / "services" / "ai_governance.py"]
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi")
    for f in files:
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
