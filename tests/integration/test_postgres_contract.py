"""Phase B.5 — REAL PostgreSQL contract validation.

These prove the exact persistence contract against PostgreSQL itself (not SQLite): migrations,
NUMERIC precision, transactions/rollback, UNIQUE idempotency, concurrency + row locking, reconnect,
connection failure, and crash recovery. They are SKIPPED unless a real Postgres is provided:

    export ATP_TEST_POSTGRES_DSN="postgresql://atp:atp@localhost:5432/atp_test"
    PYTHONPATH=src python3 -m pytest tests/integration -q

Bring up a disposable Postgres with `docker compose -f docker-compose.postgres.yml up -d`
(see docs/POSTGRES_VALIDATION.md). We NEVER fake these with SQLite.
"""

import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")            # skip if psycopg not installed
DSN = os.environ.get("ATP_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATP_TEST_POSTGRES_DSN to run Postgres tests")

from atp.runtime import LifecycleManager, OrderManager, RuntimeStatus, TradingGate, reconstruct_positions
from atp.store import D, open_store, paper_canary_config_checksum
from atp.store.base import (
    FillRow,
    PaperCanarySafetyError,
    PaperCanaryStateError,
    utcnow_iso,
)
from atp.store.postgres_store import PostgresStore

_TABLES = [
    "paper_daily_loss_state",
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


def _paper_config_json():
    return json.dumps(
        {
            "asset_class": "EQUITY",
            "commission_per_unit": "0.01000000",
            "instrument": "AAPL",
            "max_daily_turnover": "2.00000000",
            "max_gross_notional": "1.00000000",
            "max_order_notional": "1.00000000",
            "max_orders": 2,
            "min_commission": "0.01000000",
            "mode": "paper",
            "quote_max_age_s": "60.00000000",
            "slippage_bps": "0.00000000",
            "starting_cash": "10.00000000",
            "tag": "atp.paper-canary.config.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _prepare_running_paper_runtime(store):
    store.upsert_risk_config(
        capital=D("10"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as cur:
        store._exec(
            cur,
            "INSERT INTO risk_control_policy "
            "(id,risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,"
            "max_drawdown_pct,config_version,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
            ("policy", 1, "USD", D("80"), D("50"), D("20"), 1, now, "test"),
        )
    store.transition(new_status="DISABLED", actor="test", reason="postgres paper race")
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=now, quote_ts=now,
    )
    config_json = _paper_config_json()
    risk_token = store.current_paper_risk_config_checksum()
    store.prepare_paper_runtime(
        config_json=config_json,
        commit_sha="a" * 40,
        expected_config_checksum=paper_canary_config_checksum(config_json),
        expected_risk_config_checksum=risk_token,
        actor="operator",
        reason="postgres create versus disable race",
    )
    life = LifecycleManager(store)
    life.arm(actor="operator")
    life.start(confirm=True, actor="operator")
    return config_json, risk_token


# ---------------------------------------------------------------- migrations + NUMERIC precision
def test_migrations_applied(store):
    versions = sorted(r[0] for r in store._all("SELECT version FROM schema_migrations"))
    assert versions == list(range(1, 28))


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


def test_postgres_paper_account_checks_allow_signed_cash_after_v25(store):
    rows = store._all(
        "SELECT conname FROM pg_constraint c "
        "JOIN pg_class t ON t.oid=c.conrelid "
        "WHERE t.relname='paper_accounts' AND c.contype='c' ORDER BY conname"
    )
    names = {row[0] for row in rows}
    assert "paper_accounts_cash_check" not in names
    assert "paper_accounts_starting_cash_check" in names
    assert "paper_accounts_gross_exposure_check" in names


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


def test_owner_create_and_atomic_disable_serialize_on_the_runtime_binding(store):
    config_json, risk_token = _prepare_running_paper_runtime(store)
    barrier = threading.Barrier(2)
    outcomes = {}

    def create_run():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            outcomes["create"] = connection.create_paper_run(
                run_id="postgres-race-run",
                config_json=config_json,
                risk_config_checksum=risk_token,
                commit_sha="a" * 40,
                starting_cash=D("10"),
                status="READY_FOR_ARM",
                require_prepared=True,
            )
        except BaseException as exc:  # pragma: no cover - asserted after both threads join
            outcomes["create_error"] = exc
        finally:
            connection.close()

    def disable_runtime():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            outcomes["disable"] = connection.disable_paper_runtime_if_no_active(
                actor="operator", reason="postgres owner-create race",
            )
        except BaseException as exc:  # pragma: no cover - asserted after both threads join
            outcomes["disable_error"] = exc
        finally:
            connection.close()

    create_thread = threading.Thread(target=create_run, daemon=True)
    disable_thread = threading.Thread(target=disable_runtime, daemon=True)
    create_thread.start()
    disable_thread.start()
    create_thread.join(timeout=20)
    disable_thread.join(timeout=20)
    assert not create_thread.is_alive() and not disable_thread.is_alive(), "runtime row lock deadlocked"

    create_won = "create" in outcomes
    disable_won = "disable" in outcomes
    assert create_won is not disable_won, outcomes
    binding = store.get_paper_runtime_binding()
    run = store.get_paper_run("postgres-race-run")
    if create_won:
        assert isinstance(outcomes.get("disable_error"), PaperCanarySafetyError)
        assert run is not None and run.active_slot == 1
        assert binding["status"] == "RUNNING" and binding["run_id"] == run.run_id
        assert (binding["commit_sha"], binding["config_checksum"],
                binding["risk_config_checksum"]) == (
            "a" * 40, paper_canary_config_checksum(config_json), risk_token,
        )
    else:
        assert isinstance(outcomes.get("create_error"), PaperCanarySafetyError)
        assert outcomes["disable"] == {
            "status": "DISABLED", "previous_status": "RUNNING", "changed": True,
        }
        assert run is None
        assert binding == {
            "status": "DISABLED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }


def test_atomic_fill_and_disable_share_one_postgres_lock_order(store):
    config_json, risk_token = _prepare_running_paper_runtime(store)
    run = store.create_paper_run(
        run_id="postgres-fill-disable-run",
        config_json=config_json,
        risk_config_checksum=risk_token,
        commit_sha="a" * 40,
        starting_cash=D("10"),
        status="READY_FOR_ARM",
        require_prepared=True,
    )
    run = store.transition_paper_run(
        run_id=run.run_id,
        expected_status="READY_FOR_ARM",
        expected_version=run.version,
        new_status="RUNNING",
    )
    quote_ts = datetime.now(timezone.utc).isoformat()
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=quote_ts, quote_ts=quote_ts,
    )
    order = store.get_or_create_paper_intent(
        run_id=run.run_id,
        idempotency_key="postgres-fill-disable-idempotency",
        decision_id="postgres-fill-disable-decision",
        client_order_id="postgres-fill-disable-order",
        instrument="AAPL",
        side="BUY",
        quantity=D("0.01"),
        quote_bid=D("99.99"),
        quote_ask=D("100"),
        quote_ts=quote_ts,
        risk_config_checksum=risk_token,
    )
    order = store.transition_paper_order(
        client_order_id=order.client_order_id,
        expected_status="INTENT",
        expected_version=order.version,
        new_status="AUTHORIZED",
    )
    barrier = threading.Barrier(2)
    outcomes = {}

    def fill_order():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            fill_ts = datetime.now(timezone.utc).isoformat()
            outcomes["fill"] = connection.commit_paper_fill_atomic(
                run_id=run.run_id,
                client_order_id=order.client_order_id,
                expected_order_version=order.version,
                fill_id="postgres-fill-disable-fill",
                broker_order_id="postgres-fill-disable-broker-order",
                broker_fill_id="postgres-fill-disable-broker-fill",
                instrument="AAPL",
                side="BUY",
                quantity=D("0.01"),
                price=D("100"),
                commission=D("0.01"),
                multiplier=D("1"),
                quote_ts=quote_ts,
                ts=fill_ts,
            )
        except BaseException as exc:  # pragma: no cover - asserted after join
            outcomes["fill_error"] = exc
        finally:
            connection.close()

    def disable_runtime():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            outcomes["disable"] = connection.disable_paper_runtime_if_no_active(
                actor="operator",
                reason="postgres fill versus disable race",
                expected_run_id=run.run_id,
            )
        except BaseException as exc:  # pragma: no cover - asserted after join
            outcomes["disable_error"] = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=fill_order, daemon=True),
        threading.Thread(target=disable_runtime, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads), "fill/disable row locks deadlocked"
    assert "fill" in outcomes and "fill_error" not in outcomes, outcomes
    assert isinstance(outcomes.get("disable_error"), PaperCanaryStateError), outcomes
    assert store.get_paper_fill(order.client_order_id) is not None
    assert store.get_runtime_state().status == "RUNNING"


def test_emergency_kill_cannot_be_overwritten_by_concurrent_start(store):
    life = LifecycleManager(store)
    life.recover(actor="test")
    life.mark_ready(actor="test")
    life.arm(actor="test")
    barrier = threading.Barrier(2)
    outcomes = {}

    def start_runtime():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            outcomes["start"] = LifecycleManager(connection).start(
                confirm=True, actor="operator",
            )
        except BaseException as exc:  # pragma: no cover - either race result is valid
            outcomes["start_error"] = exc
        finally:
            connection.close()

    def kill_runtime():
        connection = _new_store()
        try:
            barrier.wait(timeout=10)
            outcomes["kill"] = LifecycleManager(connection).kill(
                actor="operator", reason="postgres start versus kill race",
            )
        except BaseException as exc:  # pragma: no cover - asserted after join
            outcomes["kill_error"] = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=start_runtime, daemon=True),
        threading.Thread(target=kill_runtime, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads), "start/kill row locks deadlocked"
    assert outcomes.get("kill") is RuntimeStatus.KILLED and "kill_error" not in outcomes, outcomes
    assert store.get_kill_switch().engaged is True
    assert store.get_runtime_state().status == "KILLED"


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
