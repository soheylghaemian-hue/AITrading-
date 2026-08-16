"""Phase G2.5 — Trader Intelligence Layer (read-only intelligence input).

Covers: provider interface, data persistence, deterministic quality scoring, quality-weighted
consensus, missing-data → NO DATA, read-model shape, and NO execution side effects. Touches no
Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atp.traders.collector import TraderCollector
from atp.traders.consensus import compute_consensus
from atp.traders.provider import (
    NullTraderProvider, StrategyMetadata, TraderInfo, TraderPerformance, TraderPosition,
    TraderProvider, resolve_provider,
)
from atp.traders.quality import quality_breakdown, quality_score
from atp.traders.readmodel import build_symbol_consensus, build_trader_profile
from atp.store import open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates trader tables (migration 5)


# Illustrative traders per the spec: A = high return but deep drawdown; B = modest return, tiny drawdown.
PERF = {
    "A": TraderPerformance("A", total_return=1.00, annualized_return=1.00, win_rate=0.45,
                           max_drawdown=-0.60, sharpe_ratio=0.8, sortino_ratio=1.0,
                           average_holding_period=3, number_of_trades=500),
    "B": TraderPerformance("B", total_return=0.35, annualized_return=0.35, win_rate=0.68,
                           max_drawdown=-0.08, sharpe_ratio=2.4, sortino_ratio=3.2,
                           average_holding_period=20, number_of_trades=120),
    "C": TraderPerformance("C", total_return=0.10, annualized_return=0.10, win_rate=0.40,
                           max_drawdown=-0.40, sharpe_ratio=0.3, sortino_ratio=0.4,
                           average_holding_period=1, number_of_trades=900),
}
TRADERS = {
    "A": TraderInfo("A", "AlphaAggressive", "test", "US Technology", "US Technology", 400),
    "B": TraderInfo("B", "BetaSteady", "test", "US Technology", "US Technology", 650),
    "C": TraderInfo("C", "GammaNoise", "test", "US Technology", "US Technology", 30),
}


class FakeProvider(TraderProvider):
    """A synthetic, test-only provider (NOT a real integration and never persisted to prod)."""
    name = "fake"

    def __init__(self, positions): self._positions = positions
    @property
    def configured(self): return True
    def get_traders(self): return list(TRADERS.values())
    def get_performance(self, tid): return PERF.get(tid)
    def get_positions(self, tid): return [p for p in self._positions if p.trader_id == tid]
    def get_strategy_metadata(self, tid): return StrategyMetadata(tid, "US Technology", "US Technology")


def test_provider_interface_default_is_null_no_data():
    p = resolve_provider()                                 # ATP_TRADER_PROVIDER unset → Null
    assert isinstance(p, NullTraderProvider)
    assert p.configured is False
    assert p.get_traders() == [] and p.get_positions("x") == []
    assert p.get_performance("x") is None and p.get_strategy_metadata("x") is None


def test_quality_score_weights_low_drawdown_higher():
    qa = quality_score(PERF["A"], TRADERS["A"].track_record_days)   # +100% but -60% drawdown
    qb = quality_score(PERF["B"], TRADERS["B"].track_record_days)   # +35% but only -8% drawdown
    assert qa is not None and qb is not None
    assert qb > qa                                          # the AI must weight B higher
    assert qa < 50 and qb > 80                              # deep drawdown tanks A; steady B scores high
    # a -60% drawdown scores 0 on the drawdown component (never fabricated up)
    assert quality_breakdown(PERF["A"], 400)["drawdown"] == 0
    assert quality_score(None, 100) is None                # no performance → NO DATA


def test_consensus_is_quality_weighted():
    # 2 high-quality LONG (A,B) + 1 low-quality SHORT (C). Naively 66% long; quality-weighted ≫ that.
    qa, qb, qc = (quality_score(PERF[k], TRADERS[k].track_record_days) for k in ("A", "B", "C"))
    res = compute_consensus([("LONG", qa), ("LONG", qb), ("SHORT", qc)])
    assert res.consensus == "BULLISH"
    assert res.long_percent > 80 and res.short_percent < 20   # low-quality short barely counts
    assert res.contributor_count == 3
    assert compute_consensus([]).consensus is None            # no positions → NO DATA


def test_persistence_and_readmodel(store):
    positions = [
        TraderPosition("A", "NVDA", "LONG", entry_price=210.0, position_size=1000, timestamp="2026-08-16T10:00:00Z"),
        TraderPosition("B", "NVDA", "LONG", entry_price=205.0, position_size=500, timestamp="2026-08-16T09:00:00Z"),
        TraderPosition("C", "NVDA", "SHORT", entry_price=230.0, position_size=300, timestamp="2026-08-16T08:00:00Z"),
    ]
    assert TraderCollector(store, FakeProvider(positions)).collect() == 3
    assert store.count_traders() == 3

    con = build_symbol_consensus(store, "NVDA")
    assert con["consensus"] == "BULLISH" and con["long_percent"] > 80
    assert con["contributor_count"] == 3
    assert con["contributors"][0]["name"] == "BetaSteady"      # ranked by quality (B highest)
    assert con["contributors"][0]["quality"] > 80

    prof = build_trader_profile(store, "B")
    assert prof["name"] == "BetaSteady" and prof["quality"] > 80
    assert prof["performance"]["total_return"] == 0.35
    assert prof["risk"]["max_drawdown"] == -0.08
    assert prof["strategy"]["strategy_type"] == "US Technology"
    assert build_trader_profile(store, "unknown") is None      # unknown trader → None

    # re-collect → idempotent (no duplicate traders/positions)
    TraderCollector(store, FakeProvider(positions)).collect()
    assert store.count_traders() == 3


def test_missing_data_is_no_data(store):
    assert build_symbol_consensus(store, "NVDA") == {
        "symbol": "NVDA", "consensus": None, "long_percent": None, "short_percent": None,
        "neutral_percent": None, "weighted_score": None, "contributor_count": 0, "contributors": [],
    }
    assert TraderCollector(store, NullTraderProvider()).collect() == 0
    assert store.count_traders() == 0


def test_no_execution_side_effects():
    # The whole trader-intelligence package must contain no order/execution/broker/IBKR access.
    pkg = Path(__file__).resolve().parents[2] / "src" / "atp" / "traders"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(")
    for f in pkg.glob("*.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
