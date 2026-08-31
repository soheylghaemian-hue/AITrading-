"""Durable PAPER-canary Store contract: CAS, idempotency, safety, and crash atomicity."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier

import pytest

from atp.runtime.lifecycle import LifecycleManager
from atp.runtime.paper_canary import (
    DurablePaperCanary,
    PaperCanaryConfig,
    paper_canary_order_ids,
)
from atp.store import base as store_base
from atp.store import open_store, paper_canary_config_checksum
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


def _seed(path, *, config=None, starting_cash=DEFAULT_STARTING_CASH,
          risk_capital=D("10000"), max_daily_loss_pct=D("5")):
    store = open_store(str(path))
    store.upsert_risk_config(
        capital=risk_capital, risk_per_trade_pct=D("1"),
        max_daily_loss_pct=max_daily_loss_pct,
    )
    store.upsert_risk_state(
        day_start_equity=risk_capital, peak_equity=risk_capital, halted=False, killed=False,
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
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=QUOTE_TS, quote_ts=QUOTE_TS,
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


def _intent(store, token, suffix="1", *, run_id="paper-run", side="BUY", quantity=DEFAULT_QUANTITY,
            bid=DEFAULT_BID, ask=DEFAULT_ASK, quote_ts=QUOTE_TS):
    return store.get_or_create_paper_intent(
        run_id=run_id, idempotency_key=f"idem-{suffix}", decision_id=f"decision-{suffix}",
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


def _bound_authorized(store, token, decision_id, *, run_id="paper-run", side="BUY",
                      quantity=DEFAULT_QUANTITY, bid=DEFAULT_BID, ask=DEFAULT_ASK,
                      quote_ts=QUOTE_TS):
    ids = paper_canary_order_ids(run_id, decision_id)
    order = store.get_or_create_paper_intent(
        run_id=run_id,
        idempotency_key=ids.idempotency_key,
        decision_id=decision_id,
        client_order_id=ids.client_order_id,
        instrument="AAPL",
        side=side,
        quantity=quantity,
        quote_bid=bid,
        quote_ask=ask,
        quote_ts=quote_ts,
        risk_config_checksum=token,
        correlation_id=ids.correlation_id,
    )
    return store.transition_paper_order(
        client_order_id=order.client_order_id,
        expected_status="INTENT",
        expected_version=order.version,
        new_status="AUTHORIZED",
    )


def _prepare_seed(path, *, with_risk_state=False):
    store = open_store(str(path))
    store.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    with store.tx() as cur:
        store._exec(
            cur,
            "INSERT INTO risk_control_policy "
            "(id,risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,"
            "max_drawdown_pct,config_version,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
            ("policy", 1, "USD", "80.00000000", "50.00000000", "20.00000000", 1,
             datetime.now(timezone.utc).isoformat(), "test"),
        )
    if with_risk_state:
        store.upsert_risk_state(
            day_start_equity=D("10000"), peak_equity=D("10000"), halted=False, killed=False,
        )
    store.transition(new_status="DISABLED", actor="test", reason="paper prepare")
    md_now = FILL_TS
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=md_now, quote_ts=md_now,
    )
    config_json = json.dumps(_config(), sort_keys=True, separators=(",", ":"))
    return store, config_json, store.current_paper_risk_config_checksum()


def _prepare(store, config_json, risk_token):
    return store.prepare_paper_runtime(
        config_json=config_json,
        commit_sha="a" * 40,
        expected_config_checksum=paper_canary_config_checksum(config_json),
        expected_risk_config_checksum=risk_token,
        actor="operator",
        reason="bounded test prepare",
    )


def test_prepare_runtime_is_atomic_nonactivating_and_preserves_missing_pnl(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "prepare.db")
    try:
        result = _prepare(store, config_json, risk_token)
        assert result["status"] == "READY_FOR_ARM"
        assert result["risk_state_initialized"] is True
        assert result["config_checksum"] == paper_canary_config_checksum(config_json)
        assert store.get_runtime_state().status == "READY_FOR_ARM"
        risk = store.get_risk_state()
        assert (risk.day_start_equity, risk.peak_equity, risk.halted, risk.killed) == (
            D("10000"), D("10000"), False, False,
        )
        today = datetime.now(timezone.utc).date().isoformat()
        assert store.get_daily_loss_lock(today).engaged is False
        row = store._one(
            "SELECT risk_capital_baseline,cumulative_equity_delta,version "
            "FROM paper_daily_loss_state WHERE trade_date=?",
            (today,),
        )
        assert (D(str(row[0])), D(str(row[1])), int(row[2])) == (D("10000"), D("0"), 0)
        assert store.get_daily_pnl(today) is None
        assert store.list_orders() == [] and store.list_fills() == [] and store.list_positions() == []
        assert store.recent_audit(1)[0].action == "PAPER_CANARY_READY"
        with pytest.raises(PaperCanaryStateError, match="requires global DISABLED"):
            _prepare(store, config_json, risk_token)
    finally:
        store.close()


@pytest.mark.parametrize("failure", [
    "halted", "daily_loss", "stale_market", "stale_quote", "risk_drift",
])
def test_prepare_runtime_failures_roll_back_every_baseline_write(tmp_path, failure):
    store, config_json, risk_token = _prepare_seed(tmp_path / f"prepare-{failure}.db")
    try:
        if failure == "halted":
            store.upsert_risk_state(
                day_start_equity=D("10000"), peak_equity=D("10000"), halted=True, killed=False,
            )
        elif failure == "daily_loss":
            today = datetime.now(timezone.utc).date().isoformat()
            store.upsert_daily_pnl(
                trade_date=today, day_start_equity=D("10000"),
                realized_pnl=D("-500"), unrealized_pnl=D("0"),
            )
        elif failure == "stale_market":
            store.upsert_md_health(
                symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
                ts="2020-01-01T00:00:00+00:00", quote_ts="2020-01-01T00:00:00+00:00",
            )
        elif failure == "stale_quote":
            store.upsert_md_health(
                symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
                ts=datetime.now(timezone.utc).isoformat(),
                quote_ts="2020-01-01T00:00:00+00:00",
            )
        else:
            risk_token = "0" * 20
        before_audit = len(store.recent_audit(100))
        with pytest.raises(PaperCanarySafetyError):
            _prepare(store, config_json, risk_token)
        assert store.get_runtime_state().status == "DISABLED"
        if failure != "halted":
            assert store.get_risk_state() is None
        today = datetime.now(timezone.utc).date().isoformat()
        assert store.get_daily_loss_lock(today).updated_at is None
        assert len(store.recent_audit(100)) == before_audit
    finally:
        store.close()


def _binding_tuple(store):
    binding = store.get_paper_runtime_binding()
    return (
        binding["commit_sha"],
        binding["config_checksum"],
        binding["risk_config_checksum"],
    )


def test_prepared_binding_survives_arm_start_and_clears_on_safe_terminal_states(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "prepare-binding-lifecycle.db")
    expected = ("a" * 40, paper_canary_config_checksum(config_json), risk_token)
    life = LifecycleManager(store)
    try:
        _prepare(store, config_json, risk_token)
        prepared_at = store.get_paper_runtime_binding()["prepared_at"]
        assert _binding_tuple(store) == expected

        life.arm(actor="operator")
        assert store.get_paper_runtime_binding() == {
            "status": "ARMED",
            "commit_sha": expected[0],
            "config_checksum": expected[1],
            "risk_config_checksum": expected[2],
            "prepared_at": prepared_at,
            "run_id": None,
        }
        life.start(confirm=True, actor="operator")
        assert store.get_paper_runtime_binding() == {
            "status": "RUNNING",
            "commit_sha": expected[0],
            "config_checksum": expected[1],
            "risk_config_checksum": expected[2],
            "prepared_at": prepared_at,
            "run_id": None,
        }

        life.stop(actor="operator")
        life.disarm(actor="operator")
        assert store.get_paper_runtime_binding() == {
            "status": "DISABLED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }

        _prepare(store, config_json, risk_token)
        life.kill(actor="operator", reason="binding-clear-test")
        assert store.get_paper_runtime_binding() == {
            "status": "KILLED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }

        life.reset_kill(actor="operator")
        _prepare(store, config_json, risk_token)
        life.arm(actor="operator")
        life.start(confirm=True, actor="operator")
        assert LifecycleManager(store).recover(actor="restart").value == "RECOVERY_REQUIRED"
        assert store.get_paper_runtime_binding() == {
            "status": "RECOVERY_REQUIRED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }
    finally:
        store.close()


def test_create_run_requires_running_with_the_exact_prepared_binding(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "prepared-create-success.db")
    life = LifecycleManager(store)
    try:
        _prepare(store, config_json, risk_token)
        kwargs = {
            "run_id": "prepared-run",
            "config_json": config_json,
            "risk_config_checksum": risk_token,
            "commit_sha": "a" * 40,
            "starting_cash": DEFAULT_STARTING_CASH,
            "status": "READY_FOR_ARM",
            "require_prepared": True,
        }
        with pytest.raises(PaperCanarySafetyError, match="not RUNNING"):
            store.create_paper_run(**kwargs)
        life.arm(actor="operator")
        with pytest.raises(PaperCanarySafetyError, match="not RUNNING"):
            store.create_paper_run(**kwargs)
        life.start(confirm=True, actor="operator")
        run = store.create_paper_run(**kwargs)
        assert run.run_id == "prepared-run"
        assert (run.commit_sha, run.config_checksum, run.risk_config_checksum) == (
            "a" * 40,
            paper_canary_config_checksum(config_json),
            risk_token,
        )
        assert store.get_paper_runtime_binding()["run_id"] == "prepared-run"
        stopped = store.transition_paper_run(
            run_id=run.run_id,
            expected_status="READY_FOR_ARM",
            expected_version=run.version,
            new_status="STOPPED",
            reason="one run consumed this prepare",
        )
        assert stopped.active_slot is None
        with pytest.raises(PaperCanarySafetyError, match="consumed by another run"):
            store.create_paper_run(**{**kwargs, "run_id": "second-run"})
    finally:
        store.close()


@pytest.mark.parametrize("drift", ["commit", "config", "risk"])
def test_create_run_rejects_prepared_binding_drift(tmp_path, drift):
    store, config_json, risk_token = _prepare_seed(tmp_path / f"prepared-create-{drift}.db")
    life = LifecycleManager(store)
    try:
        _prepare(store, config_json, risk_token)
        life.arm(actor="operator")
        life.start(confirm=True, actor="operator")

        commit_sha = "a" * 40
        candidate_config = config_json
        candidate_risk = risk_token
        if drift == "commit":
            commit_sha = "b" * 40
        elif drift == "config":
            candidate_config = json.dumps(
                _config(max_orders=9), sort_keys=True, separators=(",", ":"),
            )
        else:
            store.upsert_risk_config(
                capital=D("10000"), risk_per_trade_pct=D("2"), max_daily_loss_pct=D("5"),
            )
            candidate_risk = store.current_paper_risk_config_checksum()
            assert candidate_risk != risk_token

        with pytest.raises(PaperCanarySafetyError, match="exact prepared Paper binding"):
            store.create_paper_run(
                run_id=f"drift-{drift}",
                config_json=candidate_config,
                risk_config_checksum=candidate_risk,
                commit_sha=commit_sha,
                starting_cash=DEFAULT_STARTING_CASH,
                status="READY_FOR_ARM",
                require_prepared=True,
            )
        assert store.get_paper_run(f"drift-{drift}") is None
    finally:
        store.close()


def test_prepare_rejects_an_existing_active_paper_run_before_baseline_writes(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "prepare-active-run.db")
    try:
        # Seed a deliberately inconsistent crash residue through the legacy direct Store path:
        # an active run remains while the global runtime has already fallen back to DISABLED.
        store.transition(new_status="RUNNING", actor="test", reason="seed active residue")
        active = store.create_paper_run(
            run_id="already-active",
            config_json=config_json,
            risk_config_checksum=risk_token,
            commit_sha="a" * 40,
            starting_cash=DEFAULT_STARTING_CASH,
            status="READY_FOR_ARM",
        )
        assert active.active_slot == 1
        store.transition(new_status="DISABLED", actor="test", reason="simulate partial shutdown")
        before_audit = len(store.recent_audit(100))

        with pytest.raises(PaperCanaryStateError, match="active Paper Canary run"):
            _prepare(store, config_json, risk_token)

        assert store.get_runtime_state().status == "DISABLED"
        assert store.get_paper_runtime_binding() == {
            "status": "DISABLED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }
        assert store.get_risk_state() is None
        today = datetime.now(timezone.utc).date().isoformat()
        assert store.get_daily_loss_lock(today).updated_at is None
        assert len(store.recent_audit(100)) == before_audit
        assert store.get_paper_run("already-active") == active
    finally:
        store.close()


def test_atomic_paper_disable_is_idempotent_and_clears_the_prepared_binding(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "atomic-disable-idempotent.db")
    try:
        _prepare(store, config_json, risk_token)
        assert _binding_tuple(store) == (
            "a" * 40, paper_canary_config_checksum(config_json), risk_token,
        )
        before = len(store.recent_audit(100))

        first = store.disable_paper_runtime_if_no_active(
            actor="operator", reason="bounded canary did not create a run",
        )
        assert first == {
            "status": "DISABLED", "previous_status": "READY_FOR_ARM", "changed": True,
        }
        assert store.get_paper_runtime_binding() == {
            "status": "DISABLED",
            "commit_sha": None,
            "config_checksum": None,
            "risk_config_checksum": None,
            "prepared_at": None,
            "run_id": None,
        }
        events = store.recent_audit(100)
        assert len(events) == before + 1
        disabled = [event for event in events if event.action == "PAPER_CANARY_DISABLE"]
        assert len(disabled) == 1
        assert (disabled[0].previous_state, disabled[0].new_state) == (
            "READY_FOR_ARM", "DISABLED",
        )

        second = store.disable_paper_runtime_if_no_active(
            actor="operator", reason="idempotent disable retry",
        )
        assert second == {
            "status": "DISABLED", "previous_status": "DISABLED", "changed": False,
        }
        assert len(store.recent_audit(100)) == len(events)
    finally:
        store.close()


def test_atomic_paper_disable_blocks_an_active_run_and_preserves_its_binding(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "atomic-disable-active.db")
    life = LifecycleManager(store)
    try:
        prepared = _prepare(store, config_json, risk_token)
        life.arm(actor="operator")
        life.start(confirm=True, actor="operator")
        run = store.create_paper_run(
            run_id="owned-active-run",
            config_json=config_json,
            risk_config_checksum=risk_token,
            commit_sha="a" * 40,
            starting_cash=DEFAULT_STARTING_CASH,
            status="READY_FOR_ARM",
            require_prepared=True,
        )
        before = len(store.recent_audit(100))

        with pytest.raises(PaperCanaryStateError, match="active Paper Canary run"):
            store.disable_paper_runtime_if_no_active(
                actor="operator", reason="must not race past the active owner",
                expected_run_id=run.run_id,
            )

        assert store.get_paper_runtime_binding() == {
            "status": "RUNNING",
            "commit_sha": prepared["commit_sha"],
            "config_checksum": prepared["config_checksum"],
            "risk_config_checksum": prepared["risk_config_checksum"],
            "prepared_at": store.get_paper_runtime_binding()["prepared_at"],
            "run_id": run.run_id,
        }
        assert store.get_paper_run(run.run_id) == run
        assert len(store.recent_audit(100)) == before
    finally:
        store.close()


def test_atomic_paper_disable_requires_the_exact_consuming_run_binding(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "atomic-disable-binding.db")
    life = LifecycleManager(store)
    try:
        _prepare(store, config_json, risk_token)
        life.arm(actor="operator")
        life.start(confirm=True, actor="operator")
        run = store.create_paper_run(
            run_id="current-bound-run",
            config_json=config_json,
            risk_config_checksum=risk_token,
            commit_sha="a" * 40,
            starting_cash=DEFAULT_STARTING_CASH,
            status="READY_FOR_ARM",
            require_prepared=True,
        )
        store.transition_paper_run(
            run_id=run.run_id,
            expected_status="READY_FOR_ARM",
            expected_version=run.version,
            new_status="STOPPED",
            reason="terminal proof is checked by Control",
        )

        with pytest.raises(PaperCanarySafetyError, match="run-less disable"):
            store.disable_paper_runtime_if_no_active(
                actor="operator", reason="must not omit the bound run",
            )
        with pytest.raises(PaperCanarySafetyError, match="does not match"):
            store.disable_paper_runtime_if_no_active(
                actor="operator", reason="must not prove an older run",
                expected_run_id="historical-clean-run",
            )
        assert store.get_runtime_state().status == "RUNNING"

        result = store.disable_paper_runtime_if_no_active(
            actor="operator", reason="exact terminal run proof supplied",
            expected_run_id=run.run_id,
        )
        assert result["status"] == "DISABLED"
    finally:
        store.close()


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


def _bound_fill(store, order, decision_id, *, price=None, commission=DEFAULT_COMMISSION,
                fill_ts=FILL_TS):
    ids = paper_canary_order_ids(order.run_id, decision_id)
    px = price if price is not None else (order.quote_ask if order.side == "BUY" else order.quote_bid)
    return store.commit_paper_fill_atomic(
        run_id=order.run_id,
        client_order_id=order.client_order_id,
        expected_order_version=order.version,
        fill_id=ids.fill_id,
        broker_order_id=ids.broker_order_id,
        broker_fill_id=ids.broker_fill_id,
        instrument=order.instrument,
        side=order.side,
        quantity=order.quantity,
        price=px,
        commission=commission,
        multiplier=D("1"),
        quote_ts=order.quote_ts,
        ts=fill_ts,
    )


def test_fill_uses_durable_paper_daily_loss_authority_when_global_pnl_is_missing(tmp_path):
    config = _config(
        starting_cash="10.00000000",
        max_order_notional="5.00000000",
        max_gross_notional="5.00000000",
        max_daily_turnover="10.00000000",
    )
    store, run, token = _seed(
        tmp_path / "paper-loss-authority.db",
        config=config,
        starting_cash=D("10"),
    )
    try:
        assert store.get_daily_pnl("2026-08-31") is None
        order = _authorized(store, token, quantity=D("0.01"))
        with pytest.raises(PaperCanarySafetyError, match="Paper daily loss limit"):
            _fill(store, order)
        assert store.list_paper_fills(run_id=run.run_id) == []
        assert store.get_paper_order(order.client_order_id).state == "AUTHORIZED"
        account = store.get_paper_account(run.run_id)
        assert account.cash == D("10") and account.equity == D("10")
        assert _paper_daily_state(store) is None
    finally:
        store.close()


def _paper_daily_state(store, trade_date=FILL_TS[:10]):
    row = store._one(
        "SELECT risk_capital_baseline,cumulative_equity_delta,version "
        "FROM paper_daily_loss_state WHERE trade_date=?",
        (trade_date,),
    )
    return None if row is None else (D(str(row[0])), D(str(row[1])), int(row[2]))


def _paper_replay_breaks(store, run_id="paper-run"):
    run = store.get_paper_run(run_id)
    config = PaperCanaryConfig.from_canonical_json(run.config_json)
    _replay, breaks = DurablePaperCanary._replay(
        run=run,
        config=config,
        orders=tuple(store.list_paper_orders(run_id=run_id)),
        fills=tuple(store.list_paper_fills(run_id=run_id)),
        positions=tuple(store.list_paper_positions(run_id=run_id)),
        account=store.get_paper_account(run_id),
    )
    return breaks


def _prepared_running_run(store, config_json, risk_token, run_id):
    md_now = FILL_TS
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=md_now, quote_ts=md_now,
    )
    _prepare(store, config_json, risk_token)
    life = LifecycleManager(store)
    life.arm(actor="operator")
    life.start(confirm=True, actor="operator")
    run = store.create_paper_run(
        run_id=run_id,
        config_json=config_json,
        risk_config_checksum=risk_token,
        commit_sha="a" * 40,
        starting_cash=D(json.loads(config_json)["starting_cash"]),
        status="READY_FOR_ARM",
        require_prepared=True,
    )
    running = store.transition_paper_run(
        run_id=run.run_id,
        expected_status="READY_FOR_ARM",
        expected_version=run.version,
        new_status="RUNNING",
    )
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=QUOTE_TS, quote_ts=QUOTE_TS,
    )
    return running


def test_prepare_rejects_drift_from_the_utc_day_risk_capital_baseline(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "paper-loss-baseline-drift.db")
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with store.tx() as cur:
            store._exec(
                cur,
                "INSERT INTO paper_daily_loss_state "
                "(trade_date,risk_capital_baseline,cumulative_equity_delta,version,updated_at) "
                "VALUES (?,?,?,?,?)",
                (today, "9000.00000000", "-1.00000000", 7, datetime.now(timezone.utc).isoformat()),
            )

        with pytest.raises(PaperCanarySafetyError, match="capital baseline changed"):
            _prepare(store, config_json, risk_token)

        assert store.get_runtime_state().status == "DISABLED"
        assert _paper_daily_state(store, today) == (D("9000"), D("-1"), 7)
        assert store.get_daily_loss_lock(today).updated_at is None
    finally:
        store.close()


def test_daily_loss_aggregate_survives_sequential_runs_and_rejected_buy_is_atomic(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "paper-loss-sequential.db")
    config = _config(
        starting_cash="10000.00000000",
        min_commission="200.00000000",
        max_order_notional="500.00000000",
        max_gross_notional="500.00000000",
        max_daily_turnover="1000.00000000",
    )
    config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
    first = _prepared_running_run(store, config_json, risk_token, "paper-loss-run-1")
    try:
        buy = _authorized(store, risk_token, "loss-run-1-buy", run_id=first.run_id)
        _fill(store, buy, "loss-run-1-buy", commission=D("200"))
        sell = _authorized(
            store, risk_token, "loss-run-1-sell", run_id=first.run_id, side="SELL",
        )
        _fill(store, sell, "loss-run-1-sell", commission=D("200"))
        assert _paper_daily_state(store) == (D("10000"), D("-400.02000000"), 2)

        first = store.transition_paper_run(
            run_id=first.run_id,
            expected_status="RUNNING",
            expected_version=first.version,
            new_status="COMPLETED",
        )
        store.disable_paper_runtime_if_no_active(
            actor="operator", reason="sequential daily-loss proof", expected_run_id=first.run_id,
        )
        second = _prepared_running_run(store, config_json, risk_token, "paper-loss-run-2")
        rejected = _authorized(
            store, risk_token, "loss-run-2-buy", run_id=second.run_id,
        )
        account_before = store.get_paper_account(second.run_id)
        aggregate_before = _paper_daily_state(store)

        with pytest.raises(PaperCanarySafetyError, match="Paper daily loss limit"):
            _fill(store, rejected, "loss-run-2-buy", commission=D("200"))

        assert store.get_paper_order(rejected.client_order_id).state == "AUTHORIZED"
        assert store.get_paper_account(second.run_id) == account_before
        assert store.list_paper_fills(run_id=second.run_id) == []
        assert _paper_daily_state(store) == aggregate_before
    finally:
        store.close()


def test_loss_crossing_sell_flattens_and_atomically_latches_the_utc_day(tmp_path):
    store, config_json, risk_token = _prepare_seed(tmp_path / "paper-loss-sell-flatten.db")
    config = _config(
        starting_cash="10000.00000000",
        min_commission="300.00000000",
        max_order_notional="500.00000000",
        max_gross_notional="500.00000000",
        max_daily_turnover="1000.00000000",
    )
    config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
    run = _prepared_running_run(store, config_json, risk_token, "paper-loss-flatten")
    try:
        buy = _authorized(store, risk_token, "loss-flatten-buy", run_id=run.run_id)
        _fill(store, buy, "loss-flatten-buy", commission=D("300"))
        assert store.get_daily_loss_lock(FILL_TS[:10]).engaged is False

        sell = _authorized(
            store, risk_token, "loss-flatten-sell", run_id=run.run_id, side="SELL",
        )
        audits_before = [
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]
        committed = _fill(store, sell, "loss-flatten-sell", commission=D("300"))
        assert committed.position.quantity == D("0")
        assert committed.account.gross_exposure == D("0")
        assert _paper_daily_state(store) == (D("10000"), D("-600.02000000"), 2)
        assert store.get_daily_loss_lock(FILL_TS[:10]).engaged is True
        audits_after = [
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]
        assert len(audits_after) == len(audits_before) + 1
        assert _fill(store, sell, "loss-flatten-sell", commission=D("300")) == committed
        assert len([
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]) == len(audits_after)

        later_buy = _authorized(store, risk_token, "loss-after-latch", run_id=run.run_id)
        with pytest.raises(PaperCanarySafetyError, match="daily loss lock"):
            _fill(store, later_buy, "loss-after-latch", commission=D("300"))
        assert store.get_paper_order(later_buy.client_order_id).state == "AUTHORIZED"

        run = store.transition_paper_run(
            run_id=run.run_id,
            expected_status="RUNNING",
            expected_version=run.version,
            new_status="COMPLETED",
        )
        store.disable_paper_runtime_if_no_active(
            actor="operator", reason="prove prepare remains latched", expected_run_id=run.run_id,
        )
        with pytest.raises(PaperCanarySafetyError, match="daily loss lock"):
            _prepare(store, config_json, risk_token)
    finally:
        store.close()


def test_engaged_daily_loss_lock_still_allows_a_risk_reducing_sell(tmp_path):
    store, run, risk_token = _seed(tmp_path / "paper-loss-engaged-flatten.db")
    try:
        buy = _authorized(store, risk_token, "engaged-flatten-buy", run_id=run.run_id)
        _fill(store, buy, "engaged-flatten-buy")
        store.set_daily_loss_lock(
            trade_date=FILL_TS[:10], engaged=True, reason="loss authority engaged", actor="risk",
        )
        sell = _authorized(
            store, risk_token, "engaged-flatten-sell", run_id=run.run_id, side="SELL",
        )

        committed = _fill(store, sell, "engaged-flatten-sell")

        assert committed.position.quantity == D("0")
        assert committed.account.gross_exposure == D("0")
        assert store.get_daily_loss_lock(FILL_TS[:10]).engaged is True
        assert _paper_daily_state(store) == (D("10000"), D("-2.02000000"), 2)
    finally:
        store.close()


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
    store, _config_json, token = _prepare_seed(tmp_path / "capital-create.db")
    try:
        store.transition(new_status="RUNNING", actor="test", reason="direct create boundary")
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
    assert _paper_daily_state(store) == (D("10000"), D("-1"), 1)
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
        quote_ts=QUOTE_TS,
    )
    commit_times = iter((
        FILL_TS,
        "2026-08-31T14:01:01.000001+00:00",
        "2026-08-31T14:01:01.000001+00:00",
    ))
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: next(commit_times))

    with pytest.raises(PaperCanarySafetyError, match="became stale"):
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


@pytest.mark.parametrize(
    ("durable_quote_ts", "message"),
    [
        (None, "quote_ts"),
        ("2026-08-31T13:59:59+00:00", "predates the bound order quote"),
    ],
)
def test_atomic_fill_requires_current_durable_source_quote_timestamp(
    tmp_path, durable_quote_ts, message,
):
    store, _, token = _seed(tmp_path / f"durable-quote-{durable_quote_ts}.db")
    authorized = _authorized(store, token)
    store.upsert_md_health(
        symbol="AAPL",
        source="MASSIVE",
        status="READY",
        latency_ms=1,
        ts=FILL_TS,
        quote_ts=durable_quote_ts,
    )

    with pytest.raises(PaperCanarySafetyError, match=message):
        _fill(store, authorized)

    assert store.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
    assert store.list_paper_fills(run_id="paper-run") == []


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
        quote_ts=quote_time,
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


def test_gap_down_fee_insolvency_can_flatten_with_signed_cash_and_equity(tmp_path):
    config = _config(
        starting_cash="10.00000000",
        max_order_notional="10.00000000",
        max_gross_notional="10.00000000",
        max_daily_turnover="50.00000000",
        min_commission="2.00000000",
    )
    store, run, token = _seed(
        tmp_path / "insolvent-flatten.db",
        config=config,
        starting_cash=D("10"),
        risk_capital=D("10"),
        max_daily_loss_pct=D("30"),
    )
    try:
        buy = _bound_authorized(
            store, token, "insolvent-buy", bid=D("6.99"), ask=D("7"),
        )
        bought = _bound_fill(
            store, buy, "insolvent-buy", price=D("7"), commission=D("2"),
        )
        assert bought.account.cash == D("1")
        assert bought.account.equity == D("8")

        sell = _bound_authorized(
            store, token, "insolvent-sell", side="SELL", quantity=D("1"),
            bid=D("0.1"), ask=D("0.11"),
        )
        audits_before = [
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]
        flattened = _bound_fill(
            store, sell, "insolvent-sell", price=D("0.1"), commission=D("2"),
        )

        assert flattened.position.quantity == D("0")
        assert flattened.account.cash == D("-0.9")
        assert flattened.account.equity == D("-0.9")
        assert flattened.account.gross_exposure == D("0")
        assert flattened.account.net_exposure == D("0")
        assert store.get_daily_loss_lock(FILL_TS[:10]).engaged is True
        audits_after = [
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]
        assert len(audits_after) == len(audits_before) + 1
        assert _bound_fill(
            store, sell, "insolvent-sell", price=D("0.1"), commission=D("2"),
        ) == flattened
        assert len([
            event for event in store.recent_audit(100) if event.action == "DAILY_LOSS_LOCK"
        ]) == len(audits_after)
        assert _paper_replay_breaks(store, run.run_id) == []
        stopped = DurablePaperCanary(
            store, clock=lambda: datetime.fromisoformat(FILL_TS),
        ).stop(run_id=run.run_id, reason="signed insolvency ledger reconciled flat")
        assert stopped.ok is True
        assert stopped.run.status == "STOPPED"
        assert stopped.reconciliation.status == "PASS"
    finally:
        store.close()


def test_flatten_bypasses_exhausted_order_turnover_and_order_count_caps(tmp_path):
    config = _config(
        starting_cash="100.00000000",
        max_order_notional="50.00000000",
        max_gross_notional="50.00000000",
        max_daily_turnover="40.00000000",
        max_orders=1,
    )
    store, run, token = _seed(
        tmp_path / "exhausted-caps-flatten.db",
        config=config,
        starting_cash=D("100"),
        risk_capital=D("100"),
    )
    try:
        buy = _bound_authorized(store, token, "cap-buy", bid=D("39.99"), ask=D("40"))
        _bound_fill(store, buy, "cap-buy", price=D("40"))
        sell = _bound_authorized(
            store, token, "cap-sell", side="SELL", bid=D("100"), ask=D("100.01"),
        )
        flattened = _bound_fill(store, sell, "cap-sell", price=D("100"))

        assert flattened.fill.quantity * flattened.fill.price > D("50")
        assert flattened.position.quantity == D("0")
        assert flattened.account.gross_exposure == D("0")
        assert len(store.list_paper_fills(run_id=run.run_id)) == 2
        assert _paper_replay_breaks(store, run.run_id) == []

        later_buy = _authorized(
            store, token, "cap-buy-after-exit", quantity=D("0.1"),
            bid=D("99.99"), ask=D("100"),
        )
        with pytest.raises(PaperCanarySafetyError, match="max_daily_turnover"):
            _fill(store, later_buy, "cap-buy-after-exit", price=D("100"))
    finally:
        store.close()


def test_price_rise_partial_and_full_sells_reduce_exposure_above_entry_cap(tmp_path):
    config = _config(
        starting_cash="100.00000000",
        max_order_notional="50.00000000",
        max_gross_notional="50.00000000",
        max_daily_turnover="1000.00000000",
    )
    store, run, token = _seed(
        tmp_path / "price-rise-reduction.db",
        config=config,
        starting_cash=D("100"),
        risk_capital=D("100"),
    )
    try:
        buy = _bound_authorized(store, token, "rise-buy", bid=D("39.99"), ask=D("40"))
        _bound_fill(store, buy, "rise-buy", price=D("40"))
        partial = _bound_authorized(
            store, token, "rise-partial", side="SELL", quantity=D("0.25"),
            bid=D("100"), ask=D("100.01"),
        )
        reduced = _bound_fill(store, partial, "rise-partial", price=D("100"))
        assert reduced.position.quantity == D("0.75")
        assert reduced.account.gross_exposure == D("75")
        assert reduced.account.gross_exposure > D(config["max_gross_notional"])

        final = _bound_authorized(
            store, token, "rise-final", side="SELL", quantity=D("0.75"),
            bid=D("120"), ask=D("120.01"),
        )
        flattened = _bound_fill(store, final, "rise-final", price=D("120"))
        assert flattened.position.quantity == D("0")
        assert flattened.account.gross_exposure == D("0")
        assert flattened.account.net_exposure == D("0")
        assert _paper_replay_breaks(store, run.run_id) == []
    finally:
        store.close()


def test_long_reducing_sell_can_flatten_after_prepared_utc_day_rollover(
    tmp_path, monkeypatch,
):
    store, run, token = _seed(tmp_path / "overnight-flatten.db")
    try:
        prepared_at = "2026-08-31T23:59:00+00:00"
        buy_quote = "2026-08-31T23:59:58+00:00"
        buy_fill = "2026-08-31T23:59:59+00:00"
        with store.tx() as cur:
            store._exec(
                cur, "UPDATE runtime_state SET paper_prepared_at=? WHERE id=1",
                (prepared_at,),
            )
        monkeypatch.setattr(store_base, "utcnow_iso", lambda: buy_fill)
        store.upsert_md_health(
            symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
            ts=buy_quote, quote_ts=buy_quote,
        )
        buy = _bound_authorized(store, token, "overnight-buy", quote_ts=buy_quote)
        _bound_fill(store, buy, "overnight-buy", fill_ts=buy_fill)

        sell_quote = "2026-09-01T00:00:01+00:00"
        sell_fill = "2026-09-01T00:00:02+00:00"
        monkeypatch.setattr(store_base, "utcnow_iso", lambda: sell_fill)
        store.upsert_md_health(
            symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
            ts=sell_quote, quote_ts=sell_quote,
        )
        sell = _bound_authorized(
            store, token, "overnight-sell", side="SELL", quote_ts=sell_quote,
        )
        flattened = _bound_fill(store, sell, "overnight-sell", fill_ts=sell_fill)

        assert flattened.position.quantity == D("0")
        assert flattened.account.gross_exposure == D("0")
        assert _paper_replay_breaks(store, run.run_id) == []
    finally:
        store.close()


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
