"""Bounded production-safety tests for the durable one-instrument Paper Canary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atp.runtime.lifecycle import CONFIRM_PHRASE, LifecycleError, LifecycleManager
from atp.runtime.paper_canary import (
    DurablePaperCanary,
    PaperCanaryConfig,
    PaperCanaryConfigurationError,
    PaperCanaryRequestError,
    PaperCanarySafetyError,
    PaperCanaryStateError,
    paper_canary_order_ids,
)
from atp.services.recovery import build_recovery_checks
from atp.store import base as store_base
from atp.store import open_store

D = Decimal
NOW = datetime(2026, 8, 31, 14, 0, 1, tzinfo=UTC)
QUOTE = NOW - timedelta(seconds=1)
COMMIT_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _fixed_store_clock(monkeypatch):
    """The runtime clock and Store-owned commit clock share one deterministic instant."""
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: NOW.isoformat())


def _config(**overrides) -> PaperCanaryConfig:
    values = {
        "mode": "paper",
        "allowed_instruments": ("AAPL",),
        "starting_cash": D("10000"),
        "max_order_notional": D("1000"),
        "max_gross_notional": D("5000"),
        "max_daily_turnover": D("9000"),
        "max_orders_per_day": 5,
        "commission_per_unit": D("0.01"),
        "min_commission": D("1"),
        "slippage_bps": D("5"),
        "quote_max_age_s": D("60"),
    }
    values.update(overrides)
    return PaperCanaryConfig(**values)


def _seed_store(path):
    store = open_store(str(path))
    # Risk-Control values are percentage points (1 means 1%), while canary caps are Decimal money.
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
            (
                "policy", 1, "USD", "80.00000000", "50.00000000", "20.00000000", 1,
                NOW.isoformat(), "test",
            ),
        )
    store.transition(new_status="RUNNING", actor="test", reason="test paper boundary")
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1, ts=NOW.isoformat(),
    )
    return store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: NOW.isoformat())
    value = _seed_store(tmp_path / "paper-canary.db")
    yield value
    value.close()


def _running(store, *, run_id="canary-run"):
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    config = _config()
    token = store.current_paper_risk_config_checksum()
    canary.create_run(
        run_id=run_id,
        config=config,
        commit_sha=COMMIT_SHA,
        risk_config_checksum=token,
    )
    run = canary.activate(run_id=run_id, confirm=CONFIRM_PHRASE)
    assert run.status == "RUNNING"
    return canary, config, token


def _request(token, *, run_id="canary-run", decision_id="decision-1", **overrides):
    values = {
        "run_id": run_id,
        "decision_id": decision_id,
        "instrument": "AAPL",
        "side": "BUY",
        "quantity": D("2"),
        "quote_bid": D("99.99"),
        "quote_ask": D("100"),
        "quote_ts": QUOTE,
        "risk_config_checksum": token,
    }
    values.update(overrides)
    return values


def test_config_is_lossless_tagged_and_uses_money_not_risk_percent_units(store):
    config = _config()
    payload = json.loads(config.canonical_json())
    assert payload["tag"] == "atp.paper-canary.config.v1"
    assert payload["instrument"] == "AAPL"
    assert "allowed_instruments" not in payload
    assert payload["max_orders"] == 5
    assert payload["max_order_notional"] == "1000.00000000"
    assert "risk_per_trade_pct" not in payload
    assert "max_daily_loss_pct" not in payload
    with pytest.raises(PaperCanaryConfigurationError, match="signed zero"):
        _config(slippage_bps=D("-0"))
    assert PaperCanaryConfig.from_canonical_json(config.canonical_json()) == config
    assert store_base.paper_canary_config_checksum(config.canonical_json()) == config.checksum
    risk_token = store.current_paper_risk_config_checksum()
    assert type(risk_token) is str and risk_token

    with pytest.raises(PaperCanaryConfigurationError):
        _config(allowed_instruments=("AAPL", "MSFT"))
    with pytest.raises(PaperCanaryConfigurationError):
        _config(max_orders_per_day=True)
    with pytest.raises(PaperCanaryConfigurationError):
        _config(mode="live")


def test_lifecycle_confirmation_rejects_hostile_equal_shapes(tmp_path):
    store = open_store(str(tmp_path / "lifecycle.db"))
    try:
        life = LifecycleManager(store)
        life.recover()
        life.mark_ready()
        life.arm()

        class EqualToAnything:
            def __eq__(self, _other):
                return True

        class ConfirmationSubclass(str):
            pass

        with pytest.raises(LifecycleError):
            life.start(confirm=EqualToAnything())
        with pytest.raises(LifecycleError):
            life.start(confirm=ConfirmationSubclass(CONFIRM_PHRASE))
        assert life.start(confirm=CONFIRM_PHRASE).value == "RUNNING"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"instrument": "MSFT"}, "instrument"),
        ({"asset_class": "OPTION"}, "EQUITY"),
        ({"multiplier": D("100")}, "multiplier"),
        ({"order_type": "LIMIT"}, "MARKET"),
    ],
)
def test_submit_rejects_scope_outside_exact_equity_market_canary(store, override, message):
    canary, _, token = _running(store)
    with pytest.raises(PaperCanaryRequestError, match=message):
        canary.submit(**_request(token, **override))
    assert store.list_paper_orders(run_id="canary-run") == []
    assert store.list_paper_fills(run_id="canary-run") == []


@pytest.mark.parametrize(
    "quote_ts",
    [NOW - timedelta(seconds=61), NOW + timedelta(microseconds=1), NOW.replace(tzinfo=None)],
)
def test_submit_requires_aware_utc_fresh_quote_before_persisting(store, quote_ts):
    canary, _, token = _running(store)
    with pytest.raises(PaperCanaryRequestError):
        canary.submit(**_request(token, quote_ts=quote_ts))
    assert store.list_paper_orders(run_id="canary-run") == []


def test_submit_rechecks_quote_after_authorization_and_rejects_if_it_aged(store):
    ticks = iter((NOW, NOW + timedelta(seconds=61)))
    canary = DurablePaperCanary(store, clock=lambda: next(ticks))
    token = store.current_paper_risk_config_checksum()
    canary.create_run(
        run_id="canary-run", config=_config(), commit_sha=COMMIT_SHA,
        risk_config_checksum=token,
    )
    canary.activate(run_id="canary-run", confirm=True)

    with pytest.raises(PaperCanaryRequestError, match="stale quote"):
        canary.submit(**_request(token))
    assert [row.state for row in store.list_paper_orders(run_id="canary-run")] == ["REJECTED"]
    assert store.list_paper_fills(run_id="canary-run") == []


def test_submit_is_deterministic_and_exact_retry_does_not_refill(store):
    canary, config, token = _running(store)
    first = canary.submit(**_request(token))
    assert first.replayed is False
    assert first.fill.price == D("100.05000000")
    assert first.fill.commission == D("1.00000000")
    assert first.position.quantity == D("2.00000000")
    assert first.account.cash == D("9798.90000000")
    assert first.account.equity == D("9999.00000000")
    assert first.order.request_checksum.startswith("sha256:")
    assert first.run.config_checksum == config.checksum

    aged_process = DurablePaperCanary(store, clock=lambda: NOW + timedelta(days=1))
    retry = aged_process.submit(**_request(token))
    assert retry.replayed is True
    assert retry.fill == first.fill
    assert retry.account == first.account
    assert len(store.list_paper_orders(run_id="canary-run")) == 1
    assert len(store.list_paper_fills(run_id="canary-run")) == 1

    with pytest.raises(store_base.PaperCanaryConflict):
        canary.submit(**_request(token, quote_ask=D("100.01")))
    with pytest.raises(store_base.PaperCanaryConflict):
        canary.submit(**_request("sha256:different-risk-token"))


def test_restart_requires_recovery_and_recovery_pass_never_auto_runs(store, monkeypatch):
    first_process, _, token = _running(store)
    first = first_process.submit(**_request(token))

    restarted = DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=2))
    with pytest.raises(PaperCanaryStateError, match="this process"):
        restarted.submit(**_request(token, decision_id="decision-after-restart"))

    recovery = restarted.recover(run_id="canary-run")
    assert recovery.ok is True
    assert recovery.run.status == "READY_FOR_ARM"
    assert recovery.cancelled_orders == ()
    assert store.get_paper_fill(first.order.client_order_id) == first.fill
    assert store.get_paper_account("canary-run") == first.account
    assert store.list_paper_positions(run_id="canary-run") == [first.position]
    assert store.list_paper_fills(run_id="canary-run") == [first.fill]

    with pytest.raises(PaperCanaryStateError):
        restarted.submit(**_request(token, decision_id="still-not-running"))
    restarted.activate(run_id="canary-run", confirm=True)
    monkeypatch.setattr(
        store_base, "utcnow_iso", lambda: (NOW + timedelta(seconds=2)).isoformat(),
    )
    assert restarted.submit(**_request(token, decision_id="explicitly-rearmed")).order.state == "FILLED"


def test_owner_stop_reconciles_then_makes_clean_run_terminal(store):
    canary, _, token = _running(store)
    first = canary.submit(**_request(token))

    stopped = canary.stop(run_id="canary-run", reason="operator completed canary")
    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"
    assert stopped.run.active_slot is None
    assert stopped.reconciliation.status == "PASS"
    assert store.get_paper_fill(first.order.client_order_id) == first.fill
    with pytest.raises(PaperCanaryStateError):
        canary.submit(**_request(token, decision_id="must-not-resume"))


def test_owner_stop_cancels_pending_and_does_not_resubmit(store):
    canary, _, token = _running(store)
    ids = paper_canary_order_ids("canary-run", "pending-on-stop")
    pending = store.get_or_create_paper_intent(
        run_id="canary-run",
        idempotency_key=ids.idempotency_key,
        decision_id="pending-on-stop",
        instrument="AAPL",
        side="BUY",
        quantity=D("2"),
        quote_bid=D("99.99"),
        quote_ask=D("100"),
        quote_ts=QUOTE.isoformat(),
        risk_config_checksum=token,
        correlation_id=ids.correlation_id,
        client_order_id=ids.client_order_id,
    )
    store.transition_paper_order(
        client_order_id=pending.client_order_id,
        expected_status="INTENT",
        expected_version=pending.version,
        new_status="AUTHORIZED",
    )

    stopped = canary.stop(run_id="canary-run")
    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"
    assert [row.state for row in stopped.cancelled_orders] == ["CANCELLED"]
    assert store.list_paper_fills(run_id="canary-run") == []


def test_owner_stop_leaves_corrupt_ledger_in_recovery_required(store):
    canary, _, token = _running(store)
    canary.submit(**_request(token))
    with store.tx() as cur:
        store._exec(
            cur,
            "UPDATE paper_accounts SET cash=? WHERE run_id=?",
            ("9999.00000000", "canary-run"),
        )

    stopped = canary.stop(run_id="canary-run")
    assert stopped.ok is False
    assert stopped.run.status == "RECOVERY_REQUIRED"
    assert stopped.reconciliation.status == "FAIL"
    assert stopped.breaks
    retried = canary.stop(run_id="canary-run")
    assert retried.ok is False
    assert retried.run.status == "RECOVERY_REQUIRED"
    assert retried.reconciliation.status == "FAIL"


def test_stop_after_process_restart_recovers_without_requiring_ownership(store):
    _running(store)

    stopped = DurablePaperCanary(store, clock=lambda: NOW).stop(run_id="canary-run")

    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"
    assert stopped.run.active_slot is None
    assert stopped.reconciliation.status == "PASS"


def test_reconciled_ready_run_can_be_stopped_by_new_process(store):
    owner, _, _ = _running(store)
    recovered = owner.recover(run_id="canary-run")
    assert recovered.ok is True
    assert recovered.run.status == "READY_FOR_ARM"

    stopped = DurablePaperCanary(store, clock=lambda: NOW).stop(run_id="canary-run")

    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"
    assert stopped.reconciliation == recovered.reconciliation


def test_owner_can_always_stop_after_risk_config_drift(store):
    canary, _, _ = _running(store)
    store.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("2"), max_daily_loss_pct=D("5"),
    )

    stopped = canary.stop(run_id="canary-run")
    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"


def test_recovery_cancels_authorized_order_and_never_resubmits(store):
    _, _, token = _running(store)
    ids = paper_canary_order_ids("canary-run", "pending-decision")
    pending = store.get_or_create_paper_intent(
        run_id="canary-run",
        idempotency_key=ids.idempotency_key,
        decision_id="pending-decision",
        instrument="AAPL",
        side="BUY",
        quantity=D("2"),
        quote_bid=D("99.99"),
        quote_ask=D("100"),
        quote_ts=QUOTE.isoformat(),
        risk_config_checksum=token,
        correlation_id=ids.correlation_id,
        client_order_id=ids.client_order_id,
    )
    store.transition_paper_order(
        client_order_id=pending.client_order_id,
        expected_status="INTENT",
        expected_version=pending.version,
        new_status="AUTHORIZED",
    )

    restarted = DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=2))
    recovery = restarted.recover(run_id="canary-run")
    assert recovery.ok is True
    assert recovery.run.status == "READY_FOR_ARM"
    assert [row.state for row in recovery.cancelled_orders] == ["CANCELLED"]
    assert store.get_paper_order(ids.client_order_id).state == "CANCELLED"
    assert store.list_paper_fills(run_id="canary-run") == []
    assert store.get_paper_account("canary-run").cash == D("10000.00000000")


class _CorruptReadStore:
    """Inject one hostile durable projection read without mutating the protected schema."""

    def __init__(self, store, target: str) -> None:
        self._store = store
        self._target = target

    def __getattr__(self, name):
        return getattr(self._store, name)

    def get_paper_account(self, run_id):
        row = self._store.get_paper_account(run_id)
        if self._target == "account-shape":
            return object()
        if self._target == "account":
            return replace(row, cash=row.cash + D("1"))
        return row

    def list_paper_orders(self, *, run_id, state=None, limit=1000):
        rows = self._store.list_paper_orders(run_id=run_id, state=state, limit=limit)
        if self._target == "order-shape":
            return [object()]
        if self._target == "order":
            return [replace(rows[0], request_checksum="sha256:corrupt")]
        return rows

    def list_paper_fills(self, *, run_id, limit=10000):
        rows = self._store.list_paper_fills(run_id=run_id, limit=limit)
        if self._target == "fill-shape":
            return [object()]
        if self._target == "fill":
            return [replace(rows[0], price=rows[0].price + D("1"))]
        return rows

    def list_paper_positions(self, *, run_id):
        rows = self._store.list_paper_positions(run_id=run_id)
        if self._target == "position-shape":
            return [object()]
        if self._target == "position":
            return [replace(rows[0], quantity=rows[0].quantity + D("1"))]
        return rows


@pytest.mark.parametrize(
    "target",
    [
        "order", "fill", "position", "account",
        "order-shape", "fill-shape", "position-shape", "account-shape",
    ],
)
def test_recovery_blocks_each_corrupt_durable_projection(store, target):
    first_process, _, token = _running(store)
    first_process.submit(**_request(token))

    corrupt = DurablePaperCanary(
        _CorruptReadStore(store, target),
        clock=lambda: NOW + timedelta(seconds=2),
    )
    recovery = corrupt.recover(run_id="canary-run")
    assert recovery.ok is False
    assert recovery.run.status == "RECOVERY_REQUIRED"
    assert recovery.reconciliation.status == "FAIL"
    assert recovery.breaks
    assert store.get_paper_run("canary-run").status == "RECOVERY_REQUIRED"


def test_long_only_breach_rejects_authorized_intent_without_fill(store):
    canary, _, token = _running(store)
    with pytest.raises(store_base.PaperCanarySafetyError, match="long-only"):
        canary.submit(**_request(token, side="SELL"))
    orders = store.list_paper_orders(run_id="canary-run")
    assert [row.state for row in orders] == ["REJECTED"]
    assert store.list_paper_fills(run_id="canary-run") == []


def test_global_recovery_requires_real_snapshot_or_uses_active_durable_canary(store):
    no_run_checks = build_recovery_checks(store)
    assert no_run_checks["load_orders"]() is True
    assert no_run_checks["query_broker"]() is False
    assert no_run_checks["reconcile"]() is False

    explicit_empty = build_recovery_checks(store, broker_positions={})
    assert explicit_empty["query_broker"]() is True
    assert explicit_empty["reconcile"]() is True

    mutable_snapshot = {}
    frozen = build_recovery_checks(store, broker_positions=mutable_snapshot)
    mutable_snapshot["AAPL"] = D("NaN")
    assert frozen["query_broker"]() is True
    assert frozen["reconcile"]() is True
    invalid = build_recovery_checks(store, broker_positions={"AAPL": D("NaN")})
    assert invalid["query_broker"]() is False
    assert invalid["reconcile"]() is False

    _, _, _ = _running(store)
    restarted = DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=2))
    durable = build_recovery_checks(store, paper_canary=restarted)
    assert durable["load_orders"]() is True
    assert durable["query_broker"]() is False
    assert durable["reconcile"]() is False
    assert store.get_paper_run("canary-run").status == "READY_FOR_ARM"

    reconciled = build_recovery_checks(
        store,
        broker_positions={},
        paper_canary=restarted,
    )
    assert reconciled["load_orders"]() is True
    assert reconciled["query_broker"]() is True
    assert reconciled["reconcile"]() is True


def test_global_recovery_retry_uses_persisted_pass_after_late_gate_failure(store):
    _, _, _ = _running(store)
    life = LifecycleManager(store)
    assert life.recover().value == "RECOVERY_REQUIRED"

    first = build_recovery_checks(
        store,
        broker_positions={},
        paper_canary=DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=2)),
    )
    ok, results = life.run_recovery(first)
    assert ok is False
    assert results[-1] == ("validate_market_data", False)
    assert store.get_paper_run("canary-run").status == "READY_FOR_ARM"
    assert store.list_paper_reconciliations(run_id="canary-run")[-1].status == "PASS"

    # A fresh checker instance proves READY from durable checksums; no in-memory result is reused.
    auto_retry = build_recovery_checks(
        store,
        broker_positions={},
        paper_canary=DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=3)),
    )
    assert auto_retry["load_orders"]() is True
    assert auto_retry["query_broker"]() is True
    assert auto_retry["reconcile"]() is True

    store.upsert_md_health(
        symbol="AAPL",
        source="test",
        status="READY",
        latency_ms=1.0,
        ts=datetime.now(UTC).isoformat(),
    )
    second = build_recovery_checks(
        store,
        broker_positions={},
        paper_run_id="canary-run",
        paper_canary=DurablePaperCanary(store, clock=lambda: NOW + timedelta(seconds=4)),
    )
    ok, _ = life.run_recovery(second)
    assert ok is True
    assert life.status.value == "READY_FOR_ARM"
    assert store.get_paper_run("canary-run").status == "READY_FOR_ARM"


def test_ready_recovery_proof_rechecks_current_ledger_not_just_pass_label(store):
    canary, _, _ = _running(store)
    assert canary.recover(run_id="canary-run").ok is True
    with store.tx() as cur:
        store._exec(
            cur,
            "UPDATE paper_accounts SET cash=? WHERE run_id=?",
            ("9999.00000000", "canary-run"),
        )

    proof = DurablePaperCanary(store).prove_reconciled_ready(run_id="canary-run")
    assert proof.ok is False
    assert proof.run.status == "READY_FOR_ARM"
    assert proof.breaks

    stopped = DurablePaperCanary(store).stop(run_id="canary-run")
    assert stopped.ok is False
    assert stopped.run.status == "READY_FOR_ARM"
    assert stopped.breaks


def test_new_ready_run_without_persisted_pass_cannot_use_external_snapshot_bypass(store):
    canary = DurablePaperCanary(store)
    canary.create_run(
        run_id="canary-run",
        config=_config(),
        commit_sha=COMMIT_SHA,
        risk_config_checksum=store.current_paper_risk_config_checksum(),
    )
    checks = build_recovery_checks(store, broker_positions={})
    assert checks["load_orders"]() is True
    assert checks["query_broker"]() is False
    assert checks["reconcile"]() is False
    assert store.get_paper_run("canary-run").status == "READY_FOR_ARM"


def test_create_and_activate_require_current_risk_binding(store):
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    token = store.current_paper_risk_config_checksum()
    with pytest.raises(PaperCanarySafetyError):
        canary.create_run(
            run_id="canary-run",
            config=_config(),
            commit_sha=COMMIT_SHA,
            risk_config_checksum="sha256:stale",
        )

    canary.create_run(
        run_id="canary-run", config=_config(), commit_sha=COMMIT_SHA,
        risk_config_checksum=token,
    )
    store.upsert_risk_config(
        capital=D("10000"), risk_per_trade_pct=D("2"), max_daily_loss_pct=D("5"),
    )
    with pytest.raises(PaperCanarySafetyError, match="risk configuration changed"):
        canary.activate(run_id="canary-run", confirm=True)


def test_create_rejects_starting_cash_above_canonical_risk_capital(store):
    store.upsert_risk_config(
        capital=D("9000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
    )
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    config = _config()
    token = store.current_paper_risk_config_checksum()

    with pytest.raises(PaperCanarySafetyError, match="starting_cash exceeds"):
        canary.create_run(
            run_id="canary-run",
            config=config,
            commit_sha=COMMIT_SHA,
            risk_config_checksum=token,
        )

    assert store.get_paper_run("canary-run") is None
    with pytest.raises(store_base.PaperCanarySafetyError, match="starting_cash exceeds"):
        store.create_paper_run(
            run_id="direct-store-run",
            config_json=config.canonical_json(),
            risk_config_checksum=token,
            commit_sha=COMMIT_SHA,
            starting_cash=config.starting_cash,
            status="READY_FOR_ARM",
        )
    assert store.get_paper_run("direct-store-run") is None
