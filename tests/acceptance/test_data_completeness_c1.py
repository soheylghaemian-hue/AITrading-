"""Phase C1 — Data Completeness Engine (read-only reliability layer).

Covers: the 0-100 weighted calculation, per-domain checks, READY / PARTIAL / INSUFFICIENT states,
missing sources reported (never fabricated), the score never rises to cover a gap, immutable snapshots,
and no execution side effects. Touches no Trading Core / Risk / Broker / IBKR / Execution / autonomous.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from atp.completeness.engine import (
    WEIGHTS,
    compute_completeness,
    readiness_state,
    record_completeness,
    snapshot_completeness,
)
from atp.store import open_store

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 12 (data_completeness_snapshots)


# ---- domain seeders (each makes one domain "available") ----
def seed_market(store, sym):
    store.upsert_md_health(symbol=sym, source="MASSIVE", status="UP", latency_ms=8,
                           ts=NOW.isoformat())


def seed_technical(store, sym):
    for iv in ("1D", "1h"):
        for i in range(6):
            d = (NOW - timedelta(days=6 - i)).isoformat()
            store.upsert_ohlc_bar(symbol=sym, interval=iv, ts=f"{d}", open=100, high=101, low=99,
                                  close=100.5, volume=1000, source="seed")


def seed_news(store, sym):
    for i in range(3):
        store.upsert_news_item(id=f"{sym}-n{i}", symbol=sym, title=f"{sym} {i}", source="seed", url=None,
                               published_at=NOW.isoformat(), content_summary="x", sentiment_score=0.4,
                               impact_level="MEDIUM")


def seed_fundamentals(store, sym):
    store.upsert_company(symbol=sym, company_name=sym, sector="Tech", industry="Semis",
                         exchange="NASDAQ", country="US")
    store.upsert_financial_metrics(symbol=sym, period="FY2025", revenue=1e11, revenue_growth=0.5,
                                   gross_margin=0.7, operating_margin=0.5, net_margin=0.4, eps=3.0,
                                   eps_growth=0.4, free_cash_flow=5e10, debt=1e10, cash=3e10)
    store.upsert_valuation(symbol=sym, market_cap=3e12, pe_ratio=40, forward_pe=32, price_sales=20,
                           enterprise_value=3e12)


def seed_options(store, sym):
    store.upsert_options_flow(symbol=sym, timestamp=NOW.isoformat(), call_volume=100000, put_volume=60000,
                              call_put_ratio=1.6, implied_volatility=0.42, open_interest=800000,
                              unusual_activity_score=70, large_trade_count=30, premium_volume=4e7,
                              sentiment="Bullish")


def seed_trader(store, sym):
    store.upsert_trader(id=f"t-{sym}", name="Desk", source="seed", market_focus="US", strategy_type="Mom",
                        track_record_days=800)
    store.upsert_trader_performance(trader_id=f"t-{sym}", total_return=1.5, annualized_return=0.3,
                                    win_rate=0.6, max_drawdown=0.15, sharpe_ratio=1.8, sortino_ratio=2.3,
                                    average_holding_period=5.0, number_of_trades=300)
    store.upsert_trader_position(trader_id=f"t-{sym}", symbol=sym, direction="LONG", entry_price=200,
                                 position_size=1000, timestamp=NOW.isoformat())


ALL = [seed_market, seed_technical, seed_news, seed_fundamentals, seed_options, seed_trader]


# ------------------------------------------------------------------ weighting + states
def test_weights_sum_to_one():
    assert round(sum(WEIGHTS.values()), 6) == 1.0


def test_no_data_is_insufficient_never_fabricated(store):
    c = compute_completeness(store, "NVDA", NOW)
    assert c["score"] == 0.0
    assert c["state"] == "INSUFFICIENT"
    assert c["available"] == []
    assert set(c["missing"]) == set(WEIGHTS)                 # every domain missing, none invented
    assert c["details"]["macro"]["checks"]["macro_snapshot"] is False   # no macro snapshot → NO DATA


def test_partial_state_example(store):
    # Market + News + Fundamentals present (20+15+20=55) → PARTIAL. Options/Trader/Macro missing.
    seed_market(store, "NVDA"); seed_news(store, "NVDA"); seed_fundamentals(store, "NVDA")
    c = compute_completeness(store, "NVDA", NOW)
    assert c["state"] == "PARTIAL"
    assert 50.0 <= c["score"] < 80.0
    assert set(c["available"]) == {"market", "news", "fundamentals"}
    assert "options" in c["missing"] and "trader" in c["missing"] and "macro" in c["missing"]


def test_ready_state_when_all_present(store):
    for seed in ALL:
        seed(store, "NVDA")
    c = compute_completeness(store, "NVDA", NOW)
    # Macro is 10% and always NO DATA, so the ceiling is 90 — still READY (>=80).
    assert c["score"] == pytest.approx(90.0, abs=0.1)
    assert c["state"] == "READY"
    assert "macro" in c["missing"]                          # macro interface prepared, no data
    assert set(c["available"]) >= {"market", "technical", "news", "fundamentals", "options", "trader"}


def test_readiness_thresholds():
    assert readiness_state(80.0) == "READY"
    assert readiness_state(79.9) == "PARTIAL"
    assert readiness_state(50.0) == "PARTIAL"
    assert readiness_state(49.9) == "INSUFFICIENT"
    assert readiness_state(None) == "INSUFFICIENT"


def test_missing_source_scores_zero_and_score_does_not_cover_gap(store):
    # With only fundamentals (20%), the score is exactly its weight — the gap is never back-filled.
    seed_fundamentals(store, "NVDA")
    c = compute_completeness(store, "NVDA", NOW)
    assert c["score"] == pytest.approx(20.0, abs=0.1)
    assert c["details"]["options"]["score"] == 0.0
    assert c["details"]["fundamentals"]["available"] is True


def test_partial_domain_counts_but_is_not_available(store):
    # Only a company profile (1 of 3 fundamentals checks) → fundamentals fraction 1/3, below the 0.5
    # availability bar: it contributes to the score but is neither "available" nor fully "missing".
    store.upsert_company(symbol="NVDA", company_name="NVDA", sector="Tech", industry="Semis",
                         exchange="NASDAQ", country="US")
    c = compute_completeness(store, "NVDA", NOW)
    assert "fundamentals" in c["partial"]
    assert "fundamentals" not in c["available"]
    assert "fundamentals" not in c["missing"]
    assert c["details"]["fundamentals"]["score"] == pytest.approx(33.3, abs=0.1)


def test_stale_ohlc_fails_freshness(store):
    # Old candles present but far in the past → the freshness check fails (reliability, not fabrication).
    for i in range(6):
        d = (NOW - timedelta(days=60 + i)).isoformat()
        store.upsert_ohlc_bar(symbol="NVDA", interval="1D", ts=d, open=100, high=101, low=99,
                              close=100.5, volume=1000, source="seed")
    c = compute_completeness(store, "NVDA", NOW)
    assert c["details"]["technical"]["checks"]["candles"] is True
    assert c["details"]["technical"]["checks"]["freshness"] is False


# ------------------------------------------------------------------ persistence + immutability
def test_snapshot_is_immutable(store):
    seed_market(store, "NVDA"); seed_news(store, "NVDA")
    assert snapshot_completeness(store, "NVDA", NOW) is True
    row = store.latest_data_completeness("NVDA")
    assert row is not None and row.state in ("READY", "PARTIAL", "INSUFFICIENT")
    available = json.loads(row.available_sources)
    assert "market" in available and "news" in available
    # Re-recording in the same hour must not create a second row or rewrite the first.
    first_created = row.created_at
    seed_fundamentals(store, "NVDA")                        # even if the picture changes…
    snapshot_completeness(store, "NVDA", NOW)
    assert store.count_data_completeness() == 1
    assert store.latest_data_completeness("NVDA").created_at == first_created


def test_record_completeness_over_symbols(store):
    for sym in ("NVDA", "AAPL"):
        seed_market(store, sym)
    n = record_completeness(store, ["NVDA", "AAPL", "SPY"], NOW)
    assert n == 3
    assert store.count_data_completeness() == 3


# ------------------------------------------------------------------ security: no execution
def test_no_execution_side_effects(store):
    for seed in ALL:
        seed(store, "NVDA")
    compute_completeness(store, "NVDA", NOW)
    snapshot_completeness(store, "NVDA", NOW)
    assert store.list_positions() == []
    assert store.list_fills() == []


def test_engine_source_has_no_broker_tokens():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "src" / "atp"
    files = list((root / "completeness").glob("*.py")) + [root / "services" / "data_completeness.py"]
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi")
    for f in files:
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
