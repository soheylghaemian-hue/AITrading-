"""Phase R1.4 — Insider Cluster Intelligence (read-only, intelligence only).

Covers: role weighting, cluster detection (ACCUMULATION / DISTRIBUTION / NONE), time windows (7/30/90),
BUY + SELL detection, single-insider is not a cluster, the 0-100 score, missing data → NO DATA (no
fabricated clusters), collector persistence + immutability, and no execution side effects. Touches no
Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.institutional.clusters import (
    ACCUMULATION, DISTRIBUTION, NONE, build_insider_cluster, detect_cluster, role_weight,
)
from atp.institutional.collector import InstitutionalCollector
from atp.store import open_store

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 15


def tx(name, title, ttype, shares, price, days_ago):
    return {"insider_name": name, "title": title, "transaction_type": ttype, "shares": shares,
            "price": price, "transaction_date": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")}


# ------------------------------------------------------------------ role weighting
def test_role_weights():
    assert role_weight("Chief Executive Officer") == 5
    assert role_weight("CFO") == 5
    assert role_weight("Chairman of the Board") == 4
    assert role_weight("Director") == 3
    assert role_weight("EVP, Operations") == 2
    assert role_weight("Chief Legal Officer") == 2
    assert role_weight(None) == 1                            # unknown / 10% owner


# ------------------------------------------------------------------ cluster detection
def test_accumulation_cluster():
    txns = [tx("CEO Jane", "Chief Executive Officer", "BUY", 20000, 100.0, 3),
            tx("Dir Bob", "Director", "BUY", 15000, 101.0, 5)]        # 2 buyers, >$1M
    c = detect_cluster(txns, 30, NOW)
    assert c["cluster_type"] == ACCUMULATION
    assert c["insider_count"] == 2
    assert c["score"] > 50                                    # bullish lean
    assert c["total_value"] >= 1_000_000


def test_distribution_cluster():
    txns = [tx("Chair Amy", "Chairman", "SELL", 30000, 300.0, 4),
            tx("Dir Ken", "Director", "SELL", 5000, 305.0, 10)]      # 2 sellers, >$1M
    c = detect_cluster(txns, 30, NOW)
    assert c["cluster_type"] == DISTRIBUTION
    assert c["score"] < 50                                    # bearish lean
    assert c["insider_count"] == 2


def test_single_insider_is_not_a_cluster():
    # One insider selling multiple times → NOT a cluster (a cluster needs multiple distinct insiders).
    txns = [tx("Solo Sam", "Director", "SELL", 100000, 200.0, 2),
            tx("Solo Sam", "Director", "SELL", 50000, 201.0, 6)]
    c = detect_cluster(txns, 30, NOW)
    assert c["cluster_type"] == NONE
    assert c["insider_count"] == 1
    assert c["score"] < 50                                    # still leans bearish, but no cluster


def test_below_value_threshold_is_none():
    txns = [tx("A", "Director", "BUY", 10, 5.0, 1), tx("B", "Officer", "BUY", 10, 5.0, 1)]  # tiny value
    assert detect_cluster(txns, 30, NOW)["cluster_type"] == NONE


def test_time_windows():
    txns = [tx("CEO Jane", "CEO", "BUY", 20000, 100.0, 2),      # inside 7d
            tx("Dir Bob", "Director", "BUY", 15000, 100.0, 45)]  # only inside 90d
    assert detect_cluster(txns, 7, NOW)["insider_count"] == 1    # only Jane in 7d → not a cluster
    assert detect_cluster(txns, 7, NOW)["cluster_type"] == NONE
    assert detect_cluster(txns, 90, NOW)["insider_count"] == 2   # both in 90d → cluster
    assert detect_cluster(txns, 90, NOW)["cluster_type"] == ACCUMULATION


def test_no_transactions_in_window_is_no_data():
    txns = [tx("A", "CEO", "BUY", 20000, 100.0, 200)]           # outside every window
    c = detect_cluster(txns, 30, NOW)
    assert c["cluster_type"] is None and c["score"] is None and c["insider_count"] == 0


def test_score_scale():
    strong_buy = detect_cluster([tx("A", "CEO", "BUY", 20000, 100, 1), tx("B", "CFO", "BUY", 20000, 100, 1),
                                 tx("C", "Director", "BUY", 20000, 100, 1)], 30, NOW)["score"]
    strong_sell = detect_cluster([tx("A", "CEO", "SELL", 20000, 100, 1), tx("B", "CFO", "SELL", 20000, 100, 1),
                                  tx("C", "Director", "SELL", 20000, 100, 1)], 30, NOW)["score"]
    assert strong_buy >= 80 and strong_sell <= 20             # buying high, selling low, on a 0-100 scale


# ------------------------------------------------------------------ read-model + persistence
def _seed(store, sym, txns):
    for i, t in enumerate(txns):
        store.insert_insider_transaction(id=f"{sym}:{i}", symbol=sym, insider_name=t["insider_name"],
            title=t["title"], transaction_type=t["transaction_type"], shares=t["shares"],
            price=t["price"], transaction_date=t["transaction_date"])


def test_build_insider_cluster(store):
    _seed(store, "AAPL", [tx("Chair Amy", "Chairman", "SELL", 30000, 300.0, 4),
                          tx("Dir Ken", "Director", "SELL", 5000, 305.0, 10)])
    r = build_insider_cluster(store, "AAPL", NOW)
    assert r["status"] == "COMPLETE"
    assert r["cluster_type"] == DISTRIBUTION
    assert r["score"] < 50 and r["insider_count"] == 2
    assert "distribution" in r["summary"].lower()
    assert set(r["windows"].keys()) == {7, 30, 90}


def test_missing_data_is_no_data(store):
    r = build_insider_cluster(store, "NVDA", NOW)
    assert r["status"] == "NO DATA"
    assert r["cluster_type"] is None and r["score"] is None   # never a fabricated cluster


def test_data_exists_but_no_recent_cluster(store):
    # Transactions exist but all OUTSIDE every window → COMPLETE + NONE (NOT "No Form 4 data").
    _seed(store, "AAPL", [tx("A", "Director", "SELL", 10000, 300.0, 200)])
    r = build_insider_cluster(store, "AAPL", NOW)
    assert r["status"] == "COMPLETE"                          # data exists → not NO DATA
    assert r["cluster_type"] == NONE and r["score"] is None
    assert "Form 4 data" not in r["summary"]                 # honest wording


def test_older_window_cluster_is_surfaced(store):
    # Two sellers ~60 days ago → inside 90d but not 30d → headline NONE, but the 90d cluster is surfaced.
    _seed(store, "AAPL", [tx("Chair Amy", "Chairman", "SELL", 30000, 300.0, 60),
                          tx("Dir Ken", "Director", "SELL", 5000, 305.0, 62)])
    r = build_insider_cluster(store, "AAPL", NOW)
    assert r["status"] == "COMPLETE" and r["cluster_type"] == NONE   # no CURRENT (30d) cluster
    assert r["windows"][90]["cluster_type"] == DISTRIBUTION
    assert "90d" in r["summary"] and "distribution" in r["summary"].lower()


def test_collector_persists_clusters_immutably(store):
    class FakeHoldings:
        _ciks = []
        def get_holdings_history(self, cik): return {"current": None, "previous": None}
    class FakeInsiders:
        _map = {"AAPL": "x"}
        def get_insider_transactions(self, sym):
            return [{"accession": "a", "insider_name": "Chair Amy", "title": "Chairman", "transaction_type": "SELL",
                     "shares": 30000, "price": 300.0, "transaction_date": (NOW - timedelta(days=3)).strftime("%Y-%m-%d"), "symbol": "AAPL"},
                    {"accession": "b", "insider_name": "Dir Ken", "title": "Director", "transaction_type": "SELL",
                     "shares": 5000, "price": 305.0, "transaction_date": (NOW - timedelta(days=8)).strftime("%Y-%m-%d"), "symbol": "AAPL"}]
    coll = InstitutionalCollector(store, FakeHoldings(), FakeInsiders())
    res = coll.collect_insiders()
    assert res == 2
    n = coll.collect_clusters(NOW)
    assert n >= 1 and store.count_insider_clusters() == n
    coll.collect_clusters(NOW)                               # same day → immutable, no duplicates
    assert store.count_insider_clusters() == n


# ------------------------------------------------------------------ security
def test_no_execution_side_effects(store):
    _seed(store, "AAPL", [tx("A", "Chairman", "SELL", 30000, 300.0, 4), tx("B", "Director", "SELL", 5000, 305.0, 10)])
    build_insider_cluster(store, "AAPL", NOW)
    assert store.list_positions() == [] and store.list_fills() == []


def test_source_has_no_broker_or_signal_tokens():
    f = Path(__file__).resolve().parents[2] / "src" / "atp" / "institutional" / "clusters.py"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi", "copy_trade")
    text = f.read_text()
    for token in forbidden:
        assert token not in text
