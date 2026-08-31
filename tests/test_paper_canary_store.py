"""Durable PAPER-canary Store contract: CAS, idempotency, safety, and crash atomicity."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from atp.store import base as store_base
from atp.store import open_store
from atp.store.base import (
    PaperCanaryConflict,
    PaperCanarySafetyError,
    PaperCanaryStateError,
)

D = Decimal
DEFAULT_STARTING_CASH = D("10000")
DEFAULT_QUANTITY = D("1")
DEFAULT_BID = D("99.99")
DEFAULT_ASK = D("100.01")
DEFAULT_COMMISSION = D("1")
QUOTE_TS = "2026-08-31T14:00:00+00:00"
FILL_TS = "2026-08-31T14:00:01+00:00"


@pytest.fixture(autouse=True)
def _fixed_store_clock(monkeypatch):
    """Keep deterministic fixture quotes fresh at the Store-owned commit boundary."""
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: FILL_TS)


def _config(**overrides):
    config = {
        "asset_class": "EQUITY",
        "commission_per_unit": "0.00000000",
        "instrument": "AAPL",
        "max_daily_turnover": "5000.00000000",
        "max_gross_notional": "2000.00000000",
        "max_order_notional": "1000.00000000",
        "max_orders": 10,
        "min_commission": "1.00000000",
        "mode": "paper",
        "quote_max_age_s": "60.00000000",
        "slippage_bps": "0.00000000",
        "starting_cash": "10000.00000000",
        "tag": "atp.paper-canary.config.v1",
    }
    config.update(overrides)
    return config


def _seed(path, *, config=None, starting_cash=DEFAULT_STARTING_CASH):
    store = open_store(str(path))
    store.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    store.upsert_risk_state(
        day_start_equity=D("10000"), peak_equity=D("10000"), halted=False, killed=False,
    )
    with store.tx() as cur:
        store._exec(
            cur,
            "INSERT INTO risk_control_policy "
            "(id,risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,"
            "max_drawdown_pct,config_version,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
            ("policy", 1, "USD", "80.00000000", "50.00000000", "20.00000000", 1,
             QUOTE_TS, "test"),
        )
    store.transition(new_status="RUNNING", actor="test", reason="paper canary")
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1, ts=QUOTE_TS,
    )
    token = store.current_paper_risk_config_checksum()
    canonical_config = config or _config(starting_cash=f"{starting_cash:.8f}")
    run = store.create_paper_run(
        run_id="paper-run", config_json=canonical_config, risk_config_checksum=token,
        commit_sha="commit-sha", starting_cash=starting_cash, status="READY_FOR_ARM",
    )
    run = store.transition_paper_run(
        run_id=run.run_id, expected_status="READY_FOR_ARM", expected_version=run.version,
        new_status="RUNNING",
    )
    return store, run, token


def _intent(store, token, suffix="1", *, side="BUY", quantity=DEFAULT_QUANTITY,
            bid=DEFAULT_BID, ask=DEFAULT_ASK, quote_ts=QUOTE_TS):
    return store.get_or_create_paper_intent(
        run_id="paper-run", idempotency_key=f"idem-{suffix}", decision_id=f"decision-{suffix}",
        client_order_id=f"order-{suffix}", instrument="AAPL", side=side, quantity=quantity,
        quote_bid=bid, quote_ask=ask, quote_ts=quote_ts, risk_config_checksum=token,
        correlation_id=f"correlation-{suffix}",
    )


def _authorized(store, token, suffix="1", **intent_kwargs):
    order = _intent(store, token, suffix, **intent_kwargs)
    return store.transition_paper_order(
        client_order_id=order.client_order_id, expected_status="INTENT",
        expected_version=order.version, new_status="AUTHORIZED",
    )


def _fill(store, order, suffix="1", *, price=None, commission=DEFAULT_COMMISSION,
          fill_ts=FILL_TS):
    px = price if price is not None else (order.quote_ask if order.side == "BUY" else order.quote_bid)
    return store.commit_paper_fill_atomic(
        run_id=order.run_id, client_order_id=order.client_order_id,
        expected_order_version=order.version, fill_id=f"fill-{suffix}",
        broker_order_id=f"broker-order-{suffix}", broker_fill_id=f"broker-fill-{suffix}",
        instrument=order.instrument, side=order.side, quantity=order.quantity, price=px,
        commission=commission, multiplier=D("1"), quote_ts=order.quote_ts, ts=fill_ts,
    )


def test_run_and_order_cas_and_idempotent_intent(tmp_path):
    store, run, token = _seed(tmp_path / "cas.db")
    assert run.status == "RUNNING"
    assert run.active_slot == 1
    assert run.version == 1

    first = _intent(store, token)
    assert _intent(store, token) == first
    assert len(store.list_paper_order_events(first.client_order_id)) == 1
    with pytest.raises(PaperCanaryConflict):
        store.get_or_create_paper_intent(
            run_id=run.run_id, idempotency_key=first.idempotency_key,
            decision_id=first.decision_id, client_order_id=first.client_order_id,
            instrument="AAPL", side="BUY", quantity=D("2"), quote_bid=D("99.99"),
            quote_ask=D("100.01"), quote_ts=QUOTE_TS, risk_config_checksum=token,
            correlation_id=first.correlation_id,
        )
    with pytest.raises(PaperCanaryConflict):
        store.get_or_create_paper_intent(
            run_id=run.run_id, idempotency_key="another-key", decision_id=first.decision_id,
            client_order_id="another-order", instrument="AAPL", side="BUY", quantity=D("1"),
            quote_bid=D("99.99"), quote_ask=D("100.01"), quote_ts=QUOTE_TS,
            risk_config_checksum=token, correlation_id="another-correlation",
        )

    authorized = store.transition_paper_order(
        client_order_id=first.client_order_id, expected_status="INTENT",
        expected_version=first.version, new_status="AUTHORIZED",
    )
    assert authorized.state == "AUTHORIZED"
    assert authorized.version == 1
    with pytest.raises(PaperCanaryStateError):
        store.transition_paper_order(
            client_order_id=first.client_order_id, expected_status="INTENT",
            expected_version=first.version, new_status="AUTHORIZED",
        )
    with pytest.raises(PaperCanaryStateError):
        store.transition_paper_run(
            run_id=run.run_id, expected_status="READY_FOR_ARM", expected_version=0,
            new_status="RUNNING",
        )


def test_concurrent_same_intent_converges_without_exception(tmp_path):
    path = tmp_path / "concurrent.db"
    owner, _, token = _seed(path)
    barrier = Barrier(2)

    def create():
        store = open_store(str(path))
        try:
            barrier.wait(timeout=5)
            return _intent(store, token)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: create(), range(2)))
    assert rows[0] == rows[1]
    assert len(owner.list_paper_orders(run_id="paper-run")) == 1
    assert len(owner.list_paper_order_events(rows[0].client_order_id)) == 1


def test_run_creation_rejects_canary_capital_above_canonical_risk_capital(tmp_path):
    store, _run, token = _seed(tmp_path / "capital-create.db")
    try:
        oversized = _config(starting_cash="20000.00000000")
        with pytest.raises(PaperCanarySafetyError, match="exceeds canonical risk capital"):
            store.create_paper_run(
                run_id="oversized-run",
                config_json=oversized,
                risk_config_checksum=token,
                commit_sha="commit-sha",
                starting_cash=D("20000"),
                status="READY_FOR_ARM",
            )
        assert store.get_paper_run("oversized-run") is None
    finally:
        store.close()


def test_atomic_fill_rechecks_capital_and_daily_loss_lock(tmp_path, monkeypatch):
    store, _run, token = _seed(tmp_path / "capital-fill.db")
    try:
        capital_order = _authorized(store, token, "capital")
        store.upsert_risk_config(
            capital=D("5000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
        )
        # Isolate the independent capital bound: even if a hostile/stale token oracle said unchanged,
        # the atomic transaction still reads and bounds the locked canonical capital row itself.
        checksum_method = store._paper_risk_checksum_in_tx
        monkeypatch.setattr(store, "_paper_risk_checksum_in_tx", lambda _cur: token)
        with pytest.raises(PaperCanarySafetyError, match="exceeds canonical risk capital"):
            _fill(store, capital_order, "capital")
        assert store.get_paper_fill(capital_order.client_order_id) is None

        monkeypatch.setattr(store, "_paper_risk_checksum_in_tx", checksum_method)
        store.upsert_risk_config(
            capital=D("10000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
        )
        token = store.current_paper_risk_config_checksum()
        with store.tx() as cur:
            store._exec(cur, "UPDATE paper_canary_runs SET risk_config_checksum=? WHERE run_id=?",
                        (token, "paper-run"))
        daily_order = _authorized(store, token, "daily")
        store.set_daily_loss_lock(
            trade_date=FILL_TS[:10], engaged=True, reason="daily limit", actor="test",
        )
        with pytest.raises(PaperCanarySafetyError, match="daily loss lock"):
            _fill(store, daily_order, "daily")
        assert store.get_paper_fill(daily_order.client_order_id) is None
        assert store.get_paper_order(daily_order.client_order_id).state == "AUTHORIZED"
    finally:
        store.close()

def test_fill_is_atomic_exact_and_retry_idempotent(tmp_path):
    store, _, token = _seed(tmp_path / "fill.db")
    authorized = _authorized(store, token, quantity=D("2"))
    first = _fill(store, authorized)
    retry = _fill(store, authorized)
    assert retry.fill == first.fill
    assert retry.fill.ledger_seq == 1
    assert retry.order.state == "FILLED"
    assert len(store.list_paper_fills(run_id="paper-run")) == 1
    assert [event.new_state for event in store.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED", "FILLED",
    ]
    assert first.position.quantity == D("2.00000000")
    assert first.position.avg_price == D("100.01000000")
    assert first.account.cash == D("9798.98000000")
    assert first.account.gross_exposure == D("200.02000000")
    assert first.account.equity == D("9999.00000000")
    with pytest.raises(PaperCanaryConflict):
        store.commit_paper_fill_atomic(
            run_id=authorized.run_id, client_order_id=authorized.client_order_id,
            expected_order_version=authorized.version, fill_id="different-fill",
            broker_order_id="broker-order-1", broker_fill_id="broker-fill-1",
            instrument="AAPL", side="BUY", quantity=D("2"), price=D("100.01"),
            commission=D("1"), multiplier=D("1"), quote_ts=QUOTE_TS, ts=FILL_TS,
        )


def test_concurrent_identical_fill_commits_exactly_once(tmp_path):
    path = tmp_path / "concurrent-fill.db"
    owner, _, token = _seed(path)
    authorized = _authorized(owner, token)
    barrier = Barrier(10)

    def commit():
        store = open_store(str(path))
        try:
            barrier.wait(timeout=5)
            return _fill(store, authorized)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: commit(), range(10)))
    assert all(result == results[0] for result in results)
    assert len(owner.list_paper_fills(run_id="paper-run")) == 1
    assert owner.get_paper_order(authorized.client_order_id).version == authorized.version + 1
    assert owner.get_paper_account("paper-run").version == 1
    assert owner.get_paper_position(run_id="paper-run", instrument="AAPL").version == 0
    assert [event.new_state for event in owner.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED", "FILLED",
    ]


def test_fill_rechecks_freshness_after_waiting_for_transaction_locks(tmp_path, monkeypatch):
    store, _, token = _seed(tmp_path / "lock-wait-stale.db")
    authorized = _authorized(store, token)
    store.upsert_md_health(
        symbol="AAPL",
        source="MASSIVE",
        status="READY",
        latency_ms=1,
        ts="2026-08-31T14:01:01+00:00",
    )
    commit_times = iter((
        FILL_TS,
        "2026-08-31T14:01:01.000001+00:00",
        "2026-08-31T14:01:01.000001+00:00",
    ))
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: next(commit_times))

    with pytest.raises(PaperCanarySafetyError, match="became stale while waiting"):
        _fill(store, authorized)

    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.list_paper_fills(run_id="paper-run") == []
    assert [event.new_state for event in store.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED",
    ]


@pytest.mark.parametrize(
    ("after_daily_lock", "message"),
    [
        ("2026-08-31T14:02:00+00:00", "market-data health became stale"),
        ("2026-09-01T00:00:00+00:00", "share one UTC trading day"),
    ],
)
def test_fill_rechecks_final_store_time_after_daily_lock_wait(
    tmp_path, monkeypatch, after_daily_lock, message,
):
    store, _, token = _seed(tmp_path / f"post-daily-{after_daily_lock[:10]}.db")
    authorized = _authorized(store, token)
    state = {"daily_locked": False}
    execute = store._exec

    def observe_daily_lock(cur, sql, params=()):
        result = execute(cur, sql, params)
        if "SELECT engaged FROM daily_loss_lock" in sql:
            state["daily_locked"] = True
        return result

    def clock():
        return after_daily_lock if state["daily_locked"] else FILL_TS

    monkeypatch.setattr(store, "_exec", observe_daily_lock)
    monkeypatch.setattr(store_base, "utcnow_iso", clock)
    account_before = store.get_paper_account("paper-run")

    with pytest.raises(PaperCanarySafetyError, match=message):
        _fill(store, authorized)

    assert state["daily_locked"] is True
    assert store.get_daily_loss_lock("2026-08-31").updated_at is None
    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.get_paper_account("paper-run") == account_before
    assert store.get_paper_position(run_id="paper-run", instrument="AAPL") is None
    assert store.list_paper_fills(run_id="paper-run") == []
    assert [event.new_state for event in store.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED",
    ]


@pytest.mark.parametrize(
    ("source", "status"),
    [("OTHER", "READY"), ("MASSIVE", "STALE")],
)
def test_atomic_fill_revalidates_exact_market_health_after_authorization(
    tmp_path, source, status,
):
    store, _, token = _seed(tmp_path / f"market-health-{source}-{status}.db")
    authorized = _authorized(store, token)
    store.upsert_md_health(
        symbol="AAPL", source=source, status=status, latency_ms=1, ts=FILL_TS,
    )

    with pytest.raises(PaperCanarySafetyError, match="MASSIVE/READY/REALTIME"):
        _fill(store, authorized)

    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.list_paper_fills(run_id="paper-run") == []
    assert store.get_paper_position(run_id="paper-run", instrument="AAPL") is None
    assert [event.new_state for event in store.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED",
    ]


def test_atomic_fill_rejects_utc_midnight_split_without_ledger_projection(tmp_path, monkeypatch):
    store, _, token = _seed(tmp_path / "midnight.db")
    commit_time = "2026-09-01T00:00:01+00:00"
    quote_time = "2026-08-31T23:59:59+00:00"
    caller_fill_time = "2026-08-31T23:59:59.500000+00:00"
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: commit_time)
    store.upsert_md_health(
        symbol="AAPL",
        source="MASSIVE",
        status="READY",
        latency_ms=1,
        ts="2026-09-01T00:00:00+00:00",
    )
    authorized = _authorized(store, token, quote_ts=quote_time)
    account_before = store.get_paper_account("paper-run")

    with pytest.raises(PaperCanarySafetyError, match="one UTC trading day"):
        _fill(store, authorized, fill_ts=caller_fill_time)

    assert store.get_daily_loss_lock("2026-09-01").updated_at is None
    assert store.get_daily_loss_lock("2026-08-31").updated_at is None
    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.get_paper_account("paper-run") == account_before
    assert store.get_paper_position(run_id="paper-run", instrument="AAPL") is None
    assert store.list_paper_fills(run_id="paper-run") == []
    assert [event.new_state for event in store.list_paper_order_events(authorized.client_order_id)] == [
        "INTENT", "AUTHORIZED",
    ]


def test_fill_rejects_orphans_improper_state_and_binding_mismatch(tmp_path):
    store, _, token = _seed(tmp_path / "binding.db")
    with pytest.raises(PaperCanaryStateError):
        store.commit_paper_fill_atomic(
            run_id="paper-run", client_order_id="missing", expected_order_version=0,
            fill_id="fill-x", broker_order_id="broker-x", broker_fill_id="broker-fill-x",
            instrument="AAPL", side="BUY", quantity=D("1"), price=D("100.01"),
            commission=D("0"), multiplier=D("1"), quote_ts=QUOTE_TS, ts=FILL_TS,
        )
    intent = _intent(store, token)
    with pytest.raises(PaperCanaryStateError):
        _fill(store, intent)
    authorized = store.transition_paper_order(
        client_order_id=intent.client_order_id, expected_status="INTENT",
        expected_version=intent.version, new_status="AUTHORIZED",
    )
    with pytest.raises(PaperCanaryConflict):
        store.commit_paper_fill_atomic(
            run_id="paper-run", client_order_id=authorized.client_order_id,
            expected_order_version=authorized.version, fill_id="fill-x",
            broker_order_id="broker-x", broker_fill_id="broker-fill-x", instrument="MSFT",
            side="BUY", quantity=D("1"), price=D("100.01"), commission=D("0"),
            multiplier=D("1"), quote_ts=QUOTE_TS, ts=FILL_TS,
        )


@pytest.mark.parametrize("drift", ["kill", "runtime", "risk", "config", "risk_state", "account"])
def test_fill_fails_closed_on_durable_guard_drift(tmp_path, drift):
    store, _, token = _seed(tmp_path / f"{drift}.db")
    authorized = _authorized(store, token)
    if drift == "kill":
        store.set_kill_switch(engaged=True, actor="test", reason="halt")
    elif drift == "runtime":
        store.transition(new_status="STOPPED", actor="test", reason="halt")
    elif drift == "risk":
        store.upsert_risk_config(
            capital=D("9999"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
        )
    elif drift == "risk_state":
        store.upsert_risk_state(
            day_start_equity=D("10000"), peak_equity=D("10000"), halted=True, killed=False,
        )
    elif drift == "config":
        with store.tx() as cur:
            store._exec(
                cur, "UPDATE paper_canary_runs SET config_json=? WHERE run_id=?",
                (json.dumps(_config(instrument="MSFT"), sort_keys=True, separators=(",", ":")),
                 "paper-run"),
            )
    else:
        with store.tx() as cur:
            store._exec(
                cur, "UPDATE paper_accounts SET starting_cash=? WHERE run_id=?",
                ("9999.00000000", "paper-run"),
            )
    with pytest.raises(PaperCanarySafetyError):
        _fill(store, authorized)
    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.list_paper_fills(run_id="paper-run") == []


@pytest.mark.parametrize(
    ("config", "quantity", "starting_cash"),
    [
        (_config(max_order_notional="50.00000000"), D("1"), D("10000")),
        (_config(max_daily_turnover="50.00000000"), D("1"), D("10000")),
    ],
)
def test_atomic_fill_caps_and_nonnegative_cash(tmp_path, config, quantity, starting_cash):
    store, _, token = _seed(tmp_path / f"cap-{config['max_order_notional']}-{starting_cash}.db",
                            config=config, starting_cash=starting_cash)
    authorized = _authorized(store, token, quantity=quantity)
    with pytest.raises(PaperCanarySafetyError):
        _fill(store, authorized)
    assert store.get_paper_account("paper-run").cash == starting_cash


def test_projected_gross_and_cash_are_checked_inside_fill_transaction(tmp_path):
    gross_config = _config(
        max_order_notional="50.00000000", max_gross_notional="50.00000000",
    )
    store, _, token = _seed(tmp_path / "gross.db", config=gross_config)
    first = _authorized(store, token, "first", bid=D("39.99"), ask=D("40"))
    _fill(store, first, "first", price=D("40"))
    second = _authorized(store, token, "second", bid=D("39.99"), ask=D("40"))
    with pytest.raises(PaperCanarySafetyError, match="max_gross_notional"):
        _fill(store, second, "second", price=D("40"))

    cash_config = _config(
        starting_cash="50.00000000", max_order_notional="50.00000000",
        max_gross_notional="50.00000000", max_daily_turnover="50.00000000",
        min_commission="20.00000000",
    )
    cash_store, _, cash_token = _seed(
        tmp_path / "cash.db", config=cash_config, starting_cash=D("50"),
    )
    cash_order = _authorized(cash_store, cash_token, bid=D("39.99"), ask=D("40"))
    with pytest.raises(PaperCanarySafetyError, match="cash"):
        _fill(cash_store, cash_order, price=D("40"), commission=D("20"))
    assert cash_store.get_paper_account("paper-run").cash == D("50.00000000")


def test_fill_terms_are_recomputed_from_slippage_and_commission_config(tmp_path):
    config = _config(
        commission_per_unit="0.25000000", min_commission="0.50000000",
        slippage_bps="10.00000000",
    )
    store, _, token = _seed(tmp_path / "terms.db", config=config)
    order = _authorized(store, token, quantity=D("2"))
    with pytest.raises(PaperCanaryConflict, match="deterministic config terms"):
        _fill(store, order, price=order.quote_ask, commission=D("0.5"))
    committed = _fill(store, order, price=D("100.11001"), commission=D("0.5"))
    assert committed.fill.price == D("100.11001000")
    assert committed.fill.commission == D("0.50000000")


def test_max_orders_long_only_and_sell_accounting(tmp_path):
    store, _, token = _seed(tmp_path / "account.db", config=_config(max_orders=2))
    buy = _authorized(store, token, "buy", quantity=D("2"))
    bought = _fill(store, buy, "buy")
    assert bought.position.quantity == D("2.00000000")

    sell = _authorized(
        store, token, "sell", side="SELL", quantity=D("1"),
        bid=D("99.99"), ask=D("100.01"),
    )
    sold = _fill(store, sell, "sell", commission=D("1"))
    assert sold.position.quantity == D("1.00000000")
    assert sold.position.avg_price == D("100.01000000")
    assert sold.position.realized_pnl == D("-1.02000000")
    assert sold.account.cash == D("9897.97000000")
    assert sold.account.equity == D("9997.96000000")

    third = _authorized(store, token, "third")
    with pytest.raises(PaperCanarySafetyError, match="max_orders"):
        _fill(store, third, "third")

    other, _, other_token = _seed(tmp_path / "short.db")
    short = _authorized(other, other_token, side="SELL")
    with pytest.raises(PaperCanarySafetyError, match="long-only"):
        _fill(other, short)


def test_failed_fill_rolls_back_order_account_position_and_event(tmp_path):
    store, _, token = _seed(tmp_path / "rollback.db")
    first = _fill(store, _authorized(store, token, "1"), "same")
    second = _authorized(store, token, "2")
    account_before = store.get_paper_account("paper-run")
    position_before = store.get_paper_position(run_id="paper-run", instrument="AAPL")
    with pytest.raises(sqlite3.IntegrityError):
        _fill(store, second, "same")
    assert store.get_paper_order(second.client_order_id).state == "AUTHORIZED"
    assert store.get_paper_account("paper-run") == account_before
    assert store.get_paper_position(run_id="paper-run", instrument="AAPL") == position_before
    assert len(store.list_paper_fills(run_id="paper-run")) == 1
    assert [event.new_state for event in store.list_paper_order_events(second.client_order_id)] == [
        "INTENT", "AUTHORIZED",
    ]
    assert first.fill.fill_id == "fill-same"


def test_recovery_cancellation_and_reconciliation_are_durable(tmp_path):
    store, run, token = _seed(tmp_path / "recovery.db")
    _intent(store, token, "intent")
    _authorized(store, token, "authorized")
    recovering = store.transition_paper_run(
        run_id=run.run_id, expected_status="RUNNING", expected_version=run.version,
        new_status="RECOVERY_REQUIRED", reason="restart",
    )
    assert recovering.active_slot == 1
    cancelled = store.cancel_paper_nonterminal_orders(run_id=run.run_id, reason="recovery")
    assert [row.state for row in cancelled] == ["CANCELLED", "CANCELLED"]
    reconciliation = store.record_paper_reconciliation(
        run_id=run.run_id, status="PASS", fills_checksum="fills-sha",
        positions_checksum="positions-sha", account_checksum="account-sha",
        open_order_count=0, breaks_json=[], checked_at=FILL_TS,
    )
    assert store.get_paper_reconciliation(reconciliation.reconciliation_id) == reconciliation
    assert store.list_paper_reconciliations(run_id=run.run_id) == [reconciliation]
    stopped = store.transition_paper_run(
        run_id=run.run_id, expected_status="RECOVERY_REQUIRED",
        expected_version=recovering.version, new_status="STOPPED",
    )
    assert stopped.active_slot is None


def test_exact_decimal_and_complete_risk_policy_are_required(tmp_path):
    store, _, token = _seed(tmp_path / "exact.db")
    with pytest.raises(TypeError):
        store.get_or_create_paper_intent(
            run_id="paper-run", idempotency_key="float", decision_id="float",
            instrument="AAPL", side="BUY", quantity=1.0, quote_bid=D("99.99"),
            quote_ask=D("100.01"), quote_ts=QUOTE_TS, risk_config_checksum=token,
        )

    incomplete = open_store(str(tmp_path / "incomplete.db"))
    incomplete.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    with pytest.raises(PaperCanarySafetyError, match="risk_control_policy"):
        incomplete.current_paper_risk_config_checksum()

    alias_store = open_store(str(tmp_path / "alias.db"))
    alias_store.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    alias_store.upsert_risk_state(
        day_start_equity=D("10000"), peak_equity=D("10000"), halted=False, killed=False,
    )
    with alias_store.tx() as cur:
        alias_store._exec(
            cur,
            "INSERT INTO risk_control_policy "
            "(id,risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,"
            "max_drawdown_pct,config_version,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
            ("policy", 1, "USD", "80", "50", "20", 1, QUOTE_TS, "test"),
        )
    alias_token = alias_store.current_paper_risk_config_checksum()
    with pytest.raises(ValueError, match="canonical 8dp"):
        alias_store.create_paper_run(
            run_id="alias", config_json=_config(commission_per_unit="0E-8"),
            risk_config_checksum=alias_token, commit_sha="commit", starting_cash=D("10000"),
        )
