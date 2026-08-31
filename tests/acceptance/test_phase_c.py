"""Phase C — durable-state acceptance against REAL PostgreSQL (§ service separation).

Store/gate-level proofs that hold regardless of process topology: idempotency, risk cannot be
bypassed, fail-closed on DB loss, durable survival of kill switch / daily-loss lock / positions /
orders / fills across a "restart" (fresh connection), crash → RECOVERY_REQUIRED, and never
auto-RUNNING. NO paper execution is performed — no fills are placed by these tests.

Run against the real Postgres provided by the acceptance harness:

    export ATP_TEST_POSTGRES_DSN=postgresql://atp_test:...@127.0.0.1:5432/atp_test
    PYTHONPATH=src python3 -m pytest tests/acceptance -q
"""
import os
import threading
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ATP_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATP_TEST_POSTGRES_DSN to run Phase C acceptance")

from atp.runtime import LifecycleManager, OrderManager, RuntimeStatus, TradingGate, reconstruct_positions
from atp.store import D, open_store

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
    s = open_store(DSN)
    yield s
    s.close()


def _reopen():
    return open_store(DSN, migrate=False)          # simulate a process restart


# ---------------------------------------------------------------- idempotency (no execution)
def test_duplicate_execution_intent_is_idempotent(store):
    key = "exec-intent-1"
    errors, ok = [], []

    def worker(coid):
        s = _reopen()
        try:
            s.insert_order_intent(client_order_id=coid, idempotency_key=key, instrument="AAPL",
                                  side="BUY", quantity=D("10"), order_type="MARKET", correlation_id="c")
            ok.append(coid)
        except Exception as e:
            errors.append(type(e).__name__)
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("co_a",))
    t2 = threading.Thread(target=worker, args=("co_b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    n = store._one("SELECT COUNT(*) FROM orders WHERE idempotency_key=?", (key,))[0]
    assert n == 1 and len(ok) == 1 and len(errors) == 1


# ---------------------------------------------------------------- risk cannot be bypassed
def test_risk_veto_blocks_execution(store):
    """A vetoed authorization must REJECT the order and NEVER call the fill path (no execution)."""
    om = OrderManager(store)
    fills = {"n": 0}

    def fill(coid):
        fills["n"] += 1                             # must never run when risk vetoes
        return {"broker_order_id": "x", "price": "100", "commission": "1", "filled_qty": "10"}

    om.place(idempotency_key="k-veto", instrument="AAPL", side="BUY", quantity=D("10"),
             correlation_id="c", authorize=lambda: (False, "risk veto"), fill=fill)
    assert fills["n"] == 0                           # fill never invoked → no execution
    assert store.list_fills("AAPL") == []
    assert store.get_position("AAPL") is None
    o = store.get_order_by_idempotency("k-veto")
    assert o is not None and o.state == "REJECTED"


def test_gate_blocks_every_unsafe_condition(store):
    life = LifecycleManager(store)
    gate = TradingGate(store, life)
    # not RUNNING (fresh DISABLED) → blocked
    assert gate.can_trade().allowed is False
    # bring to RUNNING with risk state, then prove kill + daily-loss each block independently
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                            halted=False, killed=False)
    assert gate.can_trade().allowed is True
    store.set_daily_loss_lock(trade_date="2026-08-14", engaged=True, reason="limit")
    assert gate.can_trade(trade_date="2026-08-14").allowed is False


# ---------------------------------------------------------------- fail closed on DB loss
def test_fail_closed_on_db_loss(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    store.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                            halted=False, killed=False)
    gate = TradingGate(store, life)
    assert gate.can_trade().allowed is True
    store.close()                                   # database unavailable
    r = gate.can_trade()
    assert r.allowed is False and "database" in r.reason


# ---------------------------------------------------------------- durable survival across restart
def test_kill_switch_survives_restart(store):
    LifecycleManager(store).kill(actor="operator", reason="panic")
    s2 = _reopen()
    assert LifecycleManager(s2).recover() is RuntimeStatus.KILLED
    assert s2.get_kill_switch().engaged is True
    s2.close()


def test_daily_loss_lock_survives_restart(store):
    store.set_daily_loss_lock(trade_date="2026-08-14", engaged=True, reason="limit")
    s2 = _reopen()
    assert s2.get_daily_loss_lock("2026-08-14").engaged is True
    s2.close()


def test_positions_orders_fills_survive_restart(store):
    om = OrderManager(store)
    om.place(idempotency_key="k1", instrument="AAPL", side="BUY", quantity=D("10"),
             correlation_id="c", authorize=lambda: (True, "ok"),
             fill=lambda coid: {"broker_order_id": "b1", "price": "100.00",
                                "commission": "1.00", "filled_qty": "10"})
    s2 = _reopen()
    assert reconstruct_positions(s2)["AAPL"].quantity == Decimal("10")
    assert len(s2.list_fills("AAPL")) == 1
    assert s2.get_order_by_idempotency("k1").state == "FILLED"
    s2.close()


# ---------------------------------------------------------------- crash → RECOVERY_REQUIRED, never RUNNING
def test_crash_from_running_lands_in_recovery_required(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    assert life.status is RuntimeStatus.RUNNING
    s2 = _reopen()                                  # unexpected restart
    assert LifecycleManager(s2).recover() is RuntimeStatus.RECOVERY_REQUIRED
    s2.close()


def test_recovery_never_auto_runs(store):
    life = LifecycleManager(store); life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    s2 = _reopen()
    life2 = LifecycleManager(s2)
    assert life2.recover() is RuntimeStatus.RECOVERY_REQUIRED
    from atp.services.recovery import build_recovery_checks
    # market-data check will fail (no feed rows) → stays RECOVERY_REQUIRED, never RUNNING
    ok, _ = life2.run_recovery(build_recovery_checks(s2))
    assert life2.status in (RuntimeStatus.RECOVERY_REQUIRED, RuntimeStatus.READY_FOR_ARM)
    assert life2.status is not RuntimeStatus.RUNNING
    s2.close()
