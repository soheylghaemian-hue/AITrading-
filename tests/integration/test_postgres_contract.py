"""Phase B.5 — REAL PostgreSQL contract validation.

These prove the exact persistence contract against PostgreSQL itself (not SQLite): migrations,
NUMERIC precision, transactions/rollback, UNIQUE idempotency, concurrency + row locking, reconnect,
connection failure, and crash recovery. They are SKIPPED unless a real Postgres is provided:

    export ATP_TEST_POSTGRES_DSN="postgresql://atp:atp@localhost:5432/atp_test"
    PYTHONPATH=src python3 -m pytest tests/integration -q

Bring up a disposable Postgres with `docker compose -f docker-compose.postgres.yml up -d`
(see docs/POSTGRES_VALIDATION.md). We NEVER fake these with SQLite.
"""

import os
import threading
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")            # skip if psycopg not installed
DSN = os.environ.get("ATP_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATP_TEST_POSTGRES_DSN to run Postgres tests")

from atp.runtime import LifecycleManager, OrderManager, RuntimeStatus, TradingGate, reconstruct_positions
from atp.store import D, open_store
from atp.store.base import FillRow, utcnow_iso
from atp.store.postgres_store import PostgresStore

_TABLES = [
    "paper_reconciliations", "paper_order_events", "paper_fills", "paper_positions",
    "paper_orders", "paper_accounts", "paper_canary_runs",
    "research_validation_metrics", "research_validation_runs",
    "research_intel_collection_events", "research_intel_outcomes",
    "research_intel_snapshot_inputs", "research_intel_snapshots",
    "research_dataset_events", "research_ohlc_bars", "research_datasets",
    "backtest_events", "backtest_metrics", "backtest_equity_points", "backtest_trades",
    "backtest_decisions", "backtest_runs", "risk_events", "risk_control_policy",
    "insider_clusters", "insider_transactions", "institutional_position_changes",
    "macro_snapshots", "data_completeness_snapshots", "ai_governance_results",
    "ai_prediction_outcomes", "ai_predictions", "ai_assessment_components", "ai_assessments",
    "options_flow", "options_snapshot", "analyst_estimates", "valuation",
    "financial_metrics", "companies", "trader_positions", "trader_performance", "traders",
    "news_items", "ohlc_bars", "market_data_health", "service_heartbeats", "audit_events",
    "decisions", "trades", "fills", "orders", "positions", "daily_loss_lock", "daily_pnl",
    "kill_switch", "risk_state", "risk_config", "runtime_state", "accounts",
    "schema_migrations",
]


def _reset():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        for t in _TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.close()


@pytest.fixture()
def store():
    _reset()
    s = open_store(DSN)                              # runs migrations on real Postgres
    yield s
    s.close()


def _new_store():
    return open_store(DSN, migrate=False)           # a second/"restart" connection


# ---------------------------------------------------------------- migrations + NUMERIC precision
def test_migrations_applied(store):
    versions = sorted(r[0] for r in store._all("SELECT version FROM schema_migrations"))
    assert versions == list(range(1, 23))


def test_numeric_types_in_information_schema(store):
    rows = store._all(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_name IN ('risk_config','positions','fills','daily_pnl','orders',"
        "'paper_accounts','paper_positions','paper_fills','paper_orders') "
        "AND column_name IN ('capital','avg_price','realized_pnl','price','commission',"
        "'unrealized_pnl','quantity','notional','monetary_risk','slippage','fees')")
    assert rows, "expected money columns present"
    for _, col, dtype in rows:
        assert dtype == "numeric", f"{col} is {dtype}, expected numeric"


def test_numeric_precision_exact(store):
    store.upsert_risk_config(capital=D("1234567.89"), risk_per_trade_pct=D("1"),
                             max_daily_loss_pct=D("3"))
    assert store.get_risk_config().capital == Decimal("1234567.89000000")
    store.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                           realized_pnl=D("-0.07"), unrealized_pnl=D("0"))
    assert store.get_daily_pnl("2026-08-14").realized_pnl == Decimal("-0.07000000")


# ---------------------------------------------------------------- transactions + rollback
def test_rollback_leaves_no_partial_write(store):
    fill = FillRow("fl1", "coX", "AAPL", "BUY", D("10"), D("100"), D("1"), utcnow_iso())
    with pytest.raises(Exception):
        store.apply_fill_atomic(fill=fill, compute=lambda cur: (_ for _ in ()).throw(RuntimeError("boom")))
    assert store.list_fills("AAPL") == []
    assert store.get_position("AAPL") is None


# ---------------------------------------------------------------- UNIQUE idempotency (concurrent)
def test_concurrent_same_idempotency_key_one_survives(store):
    key = "idem-concurrent"
    errors, ok = [], []

    def worker(coid):
        s = _new_store()
        try:
            s.insert_order_intent(client_order_id=coid, idempotency_key=key, instrument="AAPL",
                                  side="BUY", quantity=D("10"), order_type="MARKET", correlation_id="c")
            ok.append(coid)
        except Exception as e:                        # UNIQUE violation on idempotency_key
            errors.append(type(e).__name__)
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("co_a",))
    t2 = threading.Thread(target=worker, args=("co_b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    n = store._one("SELECT COUNT(*) FROM orders WHERE idempotency_key=?", (key,))[0]
    assert n == 1                                     # exactly one intent survived
    assert len(ok) == 1 and len(errors) == 1


# ---------------------------------------------------------------- concurrent fills consistent
def test_concurrent_fills_are_consistent(store):
    from atp.runtime.positions import apply_fill_to_position
    for coid in ("co1", "co2"):
        store.insert_order_intent(client_order_id=coid, idempotency_key=coid, instrument="AAPL",
                                  side="BUY", quantity=D("10"), order_type="MARKET", correlation_id="c")

    def do_fill(coid):
        s = _new_store()
        f = FillRow("fl_" + coid, coid, "AAPL", "BUY", D("10"), D("100"), D("1"), utcnow_iso())
        s.apply_fill_atomic(fill=f, compute=lambda cur: apply_fill_to_position(cur, f))
        s.close()

    t1 = threading.Thread(target=do_fill, args=("co1",))
    t2 = threading.Thread(target=do_fill, args=("co2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    pos = store.get_position("AAPL")
    assert pos.quantity == Decimal("20")              # row locking → no lost update
    assert len(store.list_fills("AAPL")) == 2


# ---------------------------------------------------------------- risk budget race (row locking)
def test_daily_risk_budget_not_exceeded_under_race(store):
    store.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                           realized_pnl=D("0"), unrealized_pnl=D("0"))
    results = []

    def reserve():
        s = _new_store()
        results.append(s.try_reserve_daily_risk(trade_date="2026-08-14", amount=D("20000"),
                                                limit=D("30000")))
        s.close()

    t1 = threading.Thread(target=reserve)
    t2 = threading.Thread(target=reserve)
    t1.start(); t2.start(); t1.join(); t2.join()
    # 20000 + 20000 would be 40000 > 30000 budget → exactly one reservation may succeed
    assert results.count(True) == 1
    assert store.get_daily_pnl("2026-08-14").realized_pnl == Decimal("-20000.00000000")


# ---------------------------------------------------------------- kill during auth → fail closed
def test_kill_switch_forces_fail_closed(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                            halted=False, killed=False)
    gate = TradingGate(store, life)
    assert gate.can_trade().allowed is True
    life.kill(actor="user", reason="panic")           # concurrent kill
    assert gate.can_trade().allowed is False


# ---------------------------------------------------------------- reconnect + connection failure
def test_reconnect_preserves_state(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.close()                                     # drop the connection
    s2 = _new_store()                                 # reconnect
    assert LifecycleManager(s2).recover() is RuntimeStatus.RECOVERY_REQUIRED
    s2.close()


def test_connection_failure_fails_closed(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                            halted=False, killed=False)
    gate = TradingGate(store, life)
    assert gate.can_trade().allowed is True
    store.close()                                     # database unavailable
    r = gate.can_trade()
    assert r.allowed is False and "database" in r.reason


# ---------------------------------------------------------------- crash recovery on real Postgres
def test_recovery_running_to_recovery_required(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.close()
    s2 = _new_store()
    assert LifecycleManager(s2).recover() is RuntimeStatus.RECOVERY_REQUIRED
    s2.close()


def test_recovery_kill_and_daily_loss_persist(store):
    store.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                           realized_pnl=D("-28000"), unrealized_pnl=D("0"))
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm()
    life.start(confirm=True); life.kill()
    store.close()
    s2 = _new_store()
    assert LifecycleManager(s2).recover() is RuntimeStatus.KILLED
    assert s2.get_daily_pnl("2026-08-14").realized_pnl == Decimal("-28000.00000000")
    s2.close()


def test_recovery_open_position_and_pending_order(store):
    om = OrderManager(store)
    ack = {"broker_order_id": "bk1", "price": "100.00", "commission": "1.00", "filled_qty": "10"}
    om.place(idempotency_key="k1", instrument="AAPL", side="BUY", quantity=D("10"),
             correlation_id="c", authorize=lambda: (True, "ok"), fill=lambda coid: ack)
    # a pending (AUTHORIZED-but-unfilled) order
    store.insert_order_intent(client_order_id="co_p", idempotency_key="k2", instrument="NVDA",
                              side="BUY", quantity=D("5"), order_type="MARKET", correlation_id="c")
    store.update_order_state(client_order_id="co_p", state="AUTHORIZED", reason="risk approved")

    s2 = _new_store()                                 # restart
    assert reconstruct_positions(s2)["AAPL"].quantity == Decimal("10")     # open position restored
    # resume the pending order — filled exactly once, no duplicate
    calls = {"n": 0}
    def fill(coid):
        calls["n"] += 1
        return {"broker_order_id": "bk2", "price": "50.00", "commission": "0.50", "filled_qty": "5"}
    OrderManager(s2).place(idempotency_key="k2", instrument="NVDA", side="BUY", quantity=D("5"),
                           correlation_id="c", authorize=lambda: (True, "ok"), fill=fill)
    assert calls["n"] == 1
    assert len(s2.list_fills("NVDA")) == 1
    s2.close()
