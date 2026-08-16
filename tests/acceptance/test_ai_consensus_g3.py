"""Phase G3 — AI Consensus Engine (read-only orchestration).

Covers: component aggregation, weighting, conflict detection, missing-data (PARTIAL / NO DATA),
explanation generation, persistence, and no-execution side effects. Touches no Trading Core / Risk /
Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atp.consensus.engine import build_ai_consensus, persist_ai_consensus
from atp.store import open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates ai_assessment* (migration 8)


def seed_fundamentals(s, sym="NVDA"):
    s.upsert_company(symbol=sym, company_name="Nvidia Corp", sector="SEMIS", industry="SEMIS",
                     exchange="XNAS", country="US")
    s.upsert_financial_metrics(symbol=sym, period="FY", revenue=130e9, revenue_growth=0.35, gross_margin=0.60,
                               operating_margin=0.50, net_margin=0.44, eps=2.3, eps_growth=0.35,
                               free_cash_flow=None, debt=None, cash=None)
    s.upsert_valuation(symbol=sym, market_cap=3e12, pe_ratio=50.0, forward_pe=None, price_sales=23.0,
                       enterprise_value=None)


def seed_options(s, sym="NVDA", sentiment="Bullish", pcr=0.37):
    s.upsert_options_flow(symbol=sym, timestamp="t", call_volume=35000, put_volume=13000, call_put_ratio=pcr,
                          implied_volatility=0.42, open_interest=46000, unusual_activity_score=72.8,
                          large_trade_count=4, premium_volume=19.18e6, sentiment=sentiment)


def seed_traders(s, sym="NVDA"):
    s.upsert_trader(id="B", name="BetaSteady", source="test", market_focus="US Tech",
                    strategy_type="US Tech", track_record_days=650)
    s.upsert_trader_performance(trader_id="B", total_return=0.35, annualized_return=0.35, win_rate=0.68,
                                max_drawdown=-0.08, sharpe_ratio=2.4, sortino_ratio=3.2,
                                average_holding_period=20, number_of_trades=120)
    s.upsert_trader_position(trader_id="B", symbol=sym, direction="LONG", entry_price=200, position_size=1000,
                             timestamp="t")


def seed_news(s, sym="NVDA", score=0.6):
    s.upsert_news_item(id="n1", symbol=sym, title="NVDA strong", source="MW", url=None, published_at="2026-08-16T10:00:00Z",
                       content_summary=None, sentiment_score=score, impact_level="HIGH")


def test_component_aggregation_and_weighting(store):
    seed_fundamentals(store)
    seed_options(store)
    seed_traders(store)
    seed_news(store)
    a = build_ai_consensus(store, "NVDA")

    names = {c["component_name"] for c in a["components"]}
    assert names == {"Fundamentals", "Options", "Trader Intelligence", "News"}   # market data / risk have no data
    # weighting: overall score is the renormalized weighted mean of the present components
    tw = sum(c["weight"] for c in a["components"])
    expect = round(sum(c["score"] * c["weight"] for c in a["components"]) / tw, 1)
    assert a["score"] == expect
    assert a["status"] == "COMPLETE"                                             # ≥3 components, coverage ≥ 0.5
    assert a["direction"] == "BULLISH" and not a["conflicts"]
    assert 0 < a["confidence"] <= 100


def test_explanation_generation(store):
    seed_fundamentals(store)
    seed_options(store)
    seed_news(store)
    a = build_ai_consensus(store, "NVDA")
    assert "Revenue growth" in a["strengths"] and "High call activity" in a["strengths"]
    assert "High valuation" in a["risks"]                                        # from fundamentals
    assert "Elevated implied volatility" in a["risks"]                           # from options


def test_conflict_detection_is_surfaced(store):
    # News positive, Fundamentals strong, but Options BEARISH → the disagreement must be shown.
    seed_fundamentals(store)
    seed_news(store, score=0.6)
    seed_options(store, sentiment="Bearish", pcr=1.6)
    a = build_ai_consensus(store, "NVDA")
    assert a["conflicts"], "a bull/bear disagreement must be surfaced, not hidden"
    assert any("Options bearish" in c for c in a["conflicts"])
    assert a["direction"] == "NEUTRAL"                                           # mixed → leans neutral


def test_missing_data_partial_and_no_data(store):
    assert build_ai_consensus(store, "NVDA")["status"] == "NO DATA"              # nothing seeded
    seed_fundamentals(store)                                                     # a single component
    a = build_ai_consensus(store, "NVDA")
    assert a["status"] == "PARTIAL" and a["score"] is not None                   # scored, but flagged partial
    assert len(a["components"]) == 1 and a["components"][0]["component_name"] == "Fundamentals"


def test_persistence(store):
    seed_fundamentals(store)
    seed_options(store)
    seed_traders(store)
    a = build_ai_consensus(store, "NVDA")
    persist_ai_consensus(store, a)
    assert store.count_ai_assessments() == 1
    row = store.get_ai_assessment("NVDA")
    assert row.overall_score == a["score"] and row.direction_bias == a["direction"]
    comps = store.list_ai_assessment_components("NVDA")
    assert len(comps) == len(a["components"]) and {c.component_name for c in comps} >= {"Fundamentals", "Options"}


def test_no_execution_side_effects():
    pkg = Path(__file__).resolve().parents[2] / "src" / "atp" / "consensus"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(")
    for f in pkg.glob("*.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
