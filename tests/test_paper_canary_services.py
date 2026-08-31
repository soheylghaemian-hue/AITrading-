"""Single-owner, loopback and Control boundaries for the durable Paper Canary."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from atp.runtime.lifecycle import CONFIRM_PHRASE, LifecycleManager, RuntimeStatus
from atp.runtime.paper_canary import (
    DurablePaperCanary,
    PaperCanaryConfig,
    paper_canary_order_ids,
)
from atp.services import control, trading
from atp.services.base import (
    PAPER_CANARY_INTERNAL_TOKEN_HEADER,
    LoopbackCommandError,
    LoopbackCommandServer,
)
from atp.services.paper_canary_owner import (
    PAPER_CANARY_OWNER_PATHS,
    PaperCanaryOwner,
    paper_canary_offensive_enabled,
)
from atp.store import base as store_base
from atp.store import open_store

D = Decimal
NOW = datetime(2026, 8, 31, 14, 0, 1, tzinfo=UTC)
QUOTE = NOW - timedelta(seconds=1)
COMMIT = "a" * 40
TOKEN = "separate-internal-paper-owner-token"


def _config() -> PaperCanaryConfig:
    return PaperCanaryConfig(
        mode="paper",
        allowed_instruments=("AAPL",),
        starting_cash=D("10000"),
        max_order_notional=D("1000"),
        max_gross_notional=D("5000"),
        max_daily_turnover=D("9000"),
        max_orders_per_day=5,
        commission_per_unit=D("0.01"),
        min_commission=D("1"),
        slippage_bps=D("5"),
        quote_max_age_s=D("60"),
    )


def _seed(path: Path):
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
            (
                "policy", 1, "USD", "80.00000000", "50.00000000", "20.00000000", 1,
                NOW.isoformat(), "test",
            ),
        )
    store.transition(new_status="RUNNING", actor="test", reason="owner integration")
    risk_token = store.current_paper_risk_config_checksum()
    with store.tx() as cur:
        store._exec(
            cur,
            "UPDATE runtime_state SET paper_commit_sha=?,paper_config_checksum=?,"
            "paper_risk_config_checksum=?,paper_prepared_at=? WHERE id=1",
            (COMMIT, _config().checksum, risk_token, NOW.isoformat()),
        )
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=NOW.isoformat(), quote_ts=NOW.isoformat(),
    )
    return store


def _quote() -> dict:
    return {
        "symbol": "AAPL",
        "asset_class": "STK",
        "source": "MASSIVE",
        "status": "READY",
        "market_data_type": "REALTIME",
        "bid": 99.99,
        "ask": 100.0,
        "timestamp": QUOTE.isoformat(),
    }


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    monkeypatch.setattr(store_base, "utcnow_iso", lambda: NOW.isoformat())
    monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "true")
    monkeypatch.setenv("BROKER_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("ATP_PAPER_CANARY_CONFIG_JSON", _config().canonical_json())
    monkeypatch.setenv("ATP_COMMIT_REF", COMMIT)
    monkeypatch.setattr(PaperCanaryOwner, "_deployed_commit", staticmethod(lambda: COMMIT))


def _request(port: int, path: str, body: bytes, *, token: str | None = TOKEN,
             content_type: str = "application/json") -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    if token is not None:
        headers[PAPER_CANARY_INTERNAL_TOKEN_HEADER] = token
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("paper", "broker", "expected"),
    [
        ("true", "false", True),
        (None, "false", False),
        ("true", None, False),
        ("TRUE", "false", False),
        ("1", "false", False),
        ("true", "FALSE", False),
        ("true", "0", False),
    ],
)
def test_offensive_double_opt_in_is_literal_and_both_flags_are_required(
    monkeypatch, paper, broker, expected,
):
    for name, value in (
        ("ATP_DURABLE_PAPER_CANARY_ENABLED", paper),
        ("BROKER_EXECUTION_ENABLED", broker),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert paper_canary_offensive_enabled() is expected
    assert control._paper_offensive_enabled() is expected


@pytest.mark.asyncio
async def test_owner_positive_path_is_serial_and_submit_is_server_bound(tmp_path):
    primary = _seed(tmp_path / "owner.db")
    latest = {"quote": _quote()}
    gate = {"value": (True, "ok")}
    owner = PaperCanaryOwner(
        quote_getter=lambda symbol: latest["quote"] if symbol == "AAPL" else None,
        trade_gate=lambda: gate["value"],
        store_factory=lambda: open_store(str(tmp_path / "owner.db"), migrate=False),
        clock=lambda: NOW,
        queue_limit=2,
    )
    await owner.start()
    server = LoopbackCommandServer(
        owner_loop=asyncio.get_running_loop(),
        handler=owner.command,
        token=TOKEN,
        paths=PAPER_CANARY_OWNER_PATHS,
        port=0,
    )
    server.start()
    try:
        create = await asyncio.to_thread(
            _request,
            server.port,
            "/internal/paper-canary/create",
            json.dumps({"run_id": "run-1"}).encode(),
        )
        assert create[0] == 200
        assert create[1]["result"]["status"] == "READY_FOR_ARM"
        activate = await asyncio.to_thread(
            _request,
            server.port,
            "/internal/paper-canary/activate",
            json.dumps({"run_id": "run-1", "confirm": CONFIRM_PHRASE}).encode(),
        )
        assert activate[0] == 200
        submit_body = {"run_id": "run-1", "decision_id": "decision-1", "side": "BUY", "quantity": "2"}
        submit = await asyncio.to_thread(
            _request,
            server.port,
            "/internal/paper-canary/submit",
            json.dumps(submit_body).encode(),
        )
        assert submit[0] == 200
        result = submit[1]["result"]
        assert result["order"]["instrument"] == "AAPL"
        assert result["order"]["quote_bid"] == "99.99000000"
        assert result["order"]["risk_config_checksum"] == primary.current_paper_risk_config_checksum()
        assert result["replayed"] is False
        stopped = await asyncio.to_thread(
            _request,
            server.port,
            "/internal/paper-canary/stop",
            json.dumps({"run_id": "run-1"}).encode(),
        )
        assert stopped[0] == 200 and stopped[1]["result"]["run"]["status"] == "STOPPED"
        latest["quote"] = None
        gate["value"] = (False, "must not be consulted for FILLED replay")
        primary.upsert_risk_config(
            capital=D("9000"), risk_per_trade_pct=D("1"), max_daily_loss_pct=D("5"),
        )
        primary.upsert_md_health(
            symbol="AAPL", source="MASSIVE", status="STALE", latency_ms=1, ts=NOW.isoformat(),
        )
        retry = await asyncio.to_thread(
            _request, server.port, "/internal/paper-canary/submit", json.dumps(submit_body).encode(),
        )
        assert retry[0] == 200 and retry[1]["result"]["replayed"] is True
        assert len(primary.list_paper_fills(run_id="run-1")) == 1
    finally:
        server.close()
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_owner_uses_reduction_gate_for_sell_but_normal_gate_for_buy(tmp_path):
    primary = _seed(tmp_path / "owner-reduction-gate.db")
    normal = {"allowed": True}
    calls = []

    def normal_gate():
        calls.append("normal")
        return (normal["allowed"], "normal gate blocked")

    def reduction_gate():
        calls.append("reduction")
        return (True, "daily latch may be bypassed only for Store-proven reduction")

    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: _quote(),
        trade_gate=normal_gate,
        risk_reduction_gate=reduction_gate,
        store_factory=lambda: open_store(
            str(tmp_path / "owner-reduction-gate.db"), migrate=False,
        ),
        clock=lambda: NOW,
    )
    await owner.start()
    try:
        await owner.command("/internal/paper-canary/create", {"run_id": "run-1"})
        await owner.command(
            "/internal/paper-canary/activate",
            {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
        )
        await owner.command(
            "/internal/paper-canary/submit",
            {"run_id": "run-1", "decision_id": "buy-1", "side": "BUY", "quantity": "1"},
        )

        primary.set_daily_loss_lock(
            trade_date=NOW.date().isoformat(),
            engaged=True,
            reason="exercise owner risk-reduction path",
            actor="test",
        )
        normal["allowed"] = False
        with pytest.raises(LoopbackCommandError, match="normal gate blocked"):
            await owner.command(
                "/internal/paper-canary/submit",
                {"run_id": "run-1", "decision_id": "buy-2", "side": "BUY", "quantity": "1"},
            )
        sold = await owner.command(
            "/internal/paper-canary/submit",
            {"run_id": "run-1", "decision_id": "sell-1", "side": "SELL", "quantity": "1"},
        )
        assert sold["order"]["side"] == "SELL"
        assert primary.get_paper_position(run_id="run-1", instrument="AAPL").quantity == D("0")
        assert calls.count("reduction") == 1
        assert calls.count("normal") == 2
    finally:
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_open_order_retry_requires_current_bound_quote_attestation(tmp_path):
    primary = _seed(tmp_path / "open-retry.db")
    latest = {"quote": _quote()}
    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: latest["quote"],
        trade_gate=lambda: (True, "ok"),
        store_factory=lambda: open_store(str(tmp_path / "open-retry.db"), migrate=False),
        clock=lambda: NOW,
    )
    await owner.start()
    try:
        await owner.command("/internal/paper-canary/create", {"run_id": "run-1"})
        await owner.command(
            "/internal/paper-canary/activate",
            {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
        )
        ids = paper_canary_order_ids("run-1", "decision-open")
        token = primary.current_paper_risk_config_checksum()
        intent = primary.get_or_create_paper_intent(
            run_id="run-1",
            idempotency_key=ids.idempotency_key,
            decision_id="decision-open",
            client_order_id=ids.client_order_id,
            instrument="AAPL",
            side="BUY",
            quantity=D("1"),
            quote_bid=D("99.99"),
            quote_ask=D("100"),
            quote_ts=QUOTE.isoformat(),
            risk_config_checksum=token,
            correlation_id=ids.correlation_id,
        )
        authorized = primary.transition_paper_order(
            client_order_id=intent.client_order_id,
            expected_status="INTENT",
            expected_version=intent.version,
            new_status="AUTHORIZED",
        )

        latest["quote"] = {**_quote(), "market_data_type": "DELAYED"}
        with pytest.raises(LoopbackCommandError, match="exact canary mandate"):
            await owner.command(
                "/internal/paper-canary/submit",
                {
                    "run_id": "run-1",
                    "decision_id": "decision-open",
                    "side": "BUY",
                    "quantity": "1",
                },
            )
        assert primary.get_paper_order(authorized.client_order_id).state == "AUTHORIZED"
        assert primary.list_paper_fills(run_id="run-1") == []

        latest["quote"] = {**_quote(), "ask": 100.01}
        with pytest.raises(LoopbackCommandError, match="no longer matches"):
            await owner.command(
                "/internal/paper-canary/submit",
                {
                    "run_id": "run-1",
                    "decision_id": "decision-open",
                    "side": "BUY",
                    "quantity": "1",
                },
            )
        latest["quote"] = _quote()
        committed = await owner.command(
            "/internal/paper-canary/submit",
            {
                "run_id": "run-1",
                "decision_id": "decision-open",
                "side": "BUY",
                "quantity": "1",
            },
        )
        assert committed["replayed"] is False
        assert len(primary.list_paper_fills(run_id="run-1")) == 1
    finally:
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_atomic_fill_rechecks_health_after_owner_quote_attestation(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "health-toctou.db")
    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: _quote(),
        trade_gate=lambda: (True, "ok"),
        store_factory=lambda: open_store(str(tmp_path / "health-toctou.db"), migrate=False),
        clock=lambda: NOW,
    )
    await owner.start()
    try:
        await owner.command("/internal/paper-canary/create", {"run_id": "run-1"})
        await owner.command(
            "/internal/paper-canary/activate",
            {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
        )
        assert owner._store is not None
        commit = owner._store.commit_paper_fill_atomic

        def health_flips_before_commit(**kwargs):
            owner._store.upsert_md_health(
                symbol="AAPL",
                source="MASSIVE",
                status="STALE",
                latency_ms=1,
                ts=NOW.isoformat(),
            )
            return commit(**kwargs)

        monkeypatch.setattr(owner._store, "commit_paper_fill_atomic", health_flips_before_commit)
        with pytest.raises(LoopbackCommandError, match="market-data health"):
            await owner.command(
                "/internal/paper-canary/submit",
                {
                    "run_id": "run-1",
                    "decision_id": "decision-1",
                    "side": "BUY",
                    "quantity": "1",
                },
            )
        order = primary.list_paper_orders(run_id="run-1")[0]
        assert order.state == "REJECTED"
        assert primary.list_paper_fills(run_id="run-1") == []
        assert primary.get_paper_position(run_id="run-1", instrument="AAPL") is None
    finally:
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_loopback_rejects_disabled_unauthorized_unknown_malformed_and_large(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "negative.db")
    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: _quote(),
        trade_gate=lambda: (True, "ok"),
        store_factory=lambda: open_store(str(tmp_path / "negative.db"), migrate=False),
        clock=lambda: NOW,
    )
    await owner.start()
    server = LoopbackCommandServer(
        owner_loop=asyncio.get_running_loop(), handler=owner.command, token=TOKEN,
        paths=PAPER_CANARY_OWNER_PATHS, port=0, body_limit=64,
    )
    server.start()
    try:
        path = "/internal/paper-canary/create"
        assert (await asyncio.to_thread(_request, server.port, path, b"{}", token="wrong"))[0] == 401
        assert (await asyncio.to_thread(_request, server.port, "/not-allowed", b"{}"))[0] == 404
        assert (await asyncio.to_thread(_request, server.port, path, b"{"))[0] == 400
        assert (await asyncio.to_thread(_request, server.port, path, b"{}", content_type="text/plain"))[0] == 415
        assert (await asyncio.to_thread(_request, server.port, path, b"{" + b"x" * 128 + b"}"))[0] == 413
        monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "1")
        assert (
            await asyncio.to_thread(
                _request, server.port, path, json.dumps({"run_id": "run-disabled"}).encode(),
            )
        )[0] == 404
    finally:
        server.close()
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_loopback_timeout_does_not_kill_next_command():
    calls = 0

    async def handler(_path, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return {"calls": calls}

    server = LoopbackCommandServer(
        owner_loop=asyncio.get_running_loop(), handler=handler, token=TOKEN,
        paths=frozenset({"/command"}), port=0, command_timeout=0.05,
    )
    server.start()
    try:
        assert (await asyncio.to_thread(_request, server.port, "/command", b"{}"))[0] == 504
        second = await asyncio.to_thread(_request, server.port, "/command", b"{}")
        assert second == (200, {"ok": True, "result": {"calls": 2}})
    finally:
        server.close()


@pytest.mark.asyncio
async def test_loopback_stalled_body_is_bounded():
    async def handler(_path, _payload):
        return {}

    server = LoopbackCommandServer(
        owner_loop=asyncio.get_running_loop(), handler=handler, token=TOKEN,
        paths=frozenset({"/command"}), port=0, request_timeout=0.05,
    )
    server.start()

    def _stall() -> bytes:
        with socket.create_connection(("127.0.0.1", server.port), timeout=1) as client:
            client.sendall(
                b"POST /command HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                + PAPER_CANARY_INTERNAL_TOKEN_HEADER.encode()
                + b": " + TOKEN.encode() + b"\r\nContent-Length: 10\r\n\r\n{}"
            )
            return client.recv(4096)

    try:
        response = await asyncio.to_thread(_stall)
        assert b" 408 " in response
    finally:
        server.close()


@pytest.mark.asyncio
async def test_startup_recovers_once_and_never_auto_activates(tmp_path):
    primary = _seed(tmp_path / "recovery.db")
    old = DurablePaperCanary(primary, clock=lambda: NOW)
    old.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    old.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: None,
        trade_gate=lambda: (False, "blocked"),
        store_factory=lambda: open_store(str(tmp_path / "recovery.db"), migrate=False),
        clock=lambda: NOW,
    )
    await owner.start()
    try:
        assert owner.startup_recovery is not None and owner.startup_recovery.ok is True
        assert primary.get_paper_run("run-1").status == "READY_FOR_ARM"
        ready = primary.get_paper_run("run-1")
        reconciliation_count = len(primary.list_paper_reconciliations(run_id="run-1"))
        proof = await owner.command(
            "/internal/paper-canary/recover",
            {"run_id": "run-1", "reason": "read-only retry"},
        )
        assert proof["ok"] is True
        assert proof["run"]["status"] == "READY_FOR_ARM"
        assert primary.get_paper_run("run-1") == ready
        assert len(primary.list_paper_reconciliations(run_id="run-1")) == reconciliation_count
        with pytest.raises(RuntimeError, match="already started"):
            await owner.start()
    finally:
        await owner.close()
        primary.close()


@pytest.mark.asyncio
async def test_deploy_and_server_config_drift_block_activation_and_new_submit(tmp_path, monkeypatch):
    primary = _seed(tmp_path / "binding.db")
    owner = PaperCanaryOwner(
        quote_getter=lambda _symbol: _quote(),
        trade_gate=lambda: (True, "ok"),
        store_factory=lambda: open_store(str(tmp_path / "binding.db"), migrate=False),
        clock=lambda: NOW,
    )
    await owner.start()
    try:
        await owner.command("/internal/paper-canary/create", {"run_id": "run-1"})
        monkeypatch.setattr(PaperCanaryOwner, "_deployed_commit", staticmethod(lambda: "b" * 40))
        with pytest.raises(Exception, match="different deployed commit"):
            await owner.command(
                "/internal/paper-canary/activate",
                {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
            )
        monkeypatch.setattr(PaperCanaryOwner, "_deployed_commit", staticmethod(lambda: COMMIT))
        other = replace(_config(), allowed_instruments=("MSFT",))
        monkeypatch.setenv("ATP_PAPER_CANARY_CONFIG_JSON", other.canonical_json())
        with pytest.raises(Exception, match="different server config"):
            await owner.command(
                "/internal/paper-canary/activate",
                {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
            )
        monkeypatch.setenv("ATP_PAPER_CANARY_CONFIG_JSON", _config().canonical_json())
        await owner.command(
            "/internal/paper-canary/activate",
            {"run_id": "run-1", "confirm": CONFIRM_PHRASE},
        )
        monkeypatch.setattr(PaperCanaryOwner, "_deployed_commit", staticmethod(lambda: "b" * 40))
        with pytest.raises(Exception, match="different deployed commit"):
            await owner.command(
                "/internal/paper-canary/submit",
                {"run_id": "run-1", "decision_id": "new", "side": "BUY", "quantity": "1"},
            )
        assert primary.list_paper_orders(run_id="run-1") == []
    finally:
        await owner.close()
        assert primary.get_paper_run("run-1").status == "READY_FOR_ARM"
        primary.close()


def test_loopback_close_before_start_is_bounded():
    async def handler(_path, _payload):
        return {}

    loop = asyncio.new_event_loop()
    try:
        server = LoopbackCommandServer(
            owner_loop=loop, handler=handler, token=TOKEN,
            paths=frozenset({"/command"}), port=0,
        )
        server.close()
        server.close()
        with pytest.raises(RuntimeError, match="closed"):
            server.start()
    finally:
        loop.close()


def test_control_auth_proxy_shape_default_off_and_no_runtime_import(monkeypatch):
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external-control-token")
    with pytest.raises(HTTPException) as raw:
        control._auth("external-control-token")
    assert raw.value.status_code == 401
    control._auth("Bearer external-control-token")

    captured = []
    monkeypatch.setattr(control, "_paper_owner_request", lambda command, body: captured.append((command, body)) or {})
    body = control.PaperCanarySubmitBody(
        run_id="run-1", decision_id="decision-1", side="BUY", quantity="1",
    )
    control.paper_canary_submit(body, authorization="Bearer external-control-token")
    assert captured == [
        ("submit", {"run_id": "run-1", "decision_id": "decision-1", "side": "BUY", "quantity": "1"})
    ]
    source = Path(control.__file__).read_text()
    assert "DurablePaperCanary" not in source
    assert "runtime.paper_canary" not in source

    monkeypatch.undo()


def test_control_prepare_is_strict_authenticated_server_bound_and_nonactivating(tmp_path, monkeypatch):
    store = open_store(str(tmp_path / "control-prepare.db"))
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
             datetime.now(UTC).isoformat(), "test"),
        )
    store.transition(new_status="DISABLED", actor="test", reason="prepare")
    md_now = NOW.isoformat()
    store.upsert_md_health(
        symbol="AAPL", source="MASSIVE", status="READY", latency_ms=1,
        ts=md_now, quote_ts=md_now,
    )
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control, "resolve_commit_sha", lambda: COMMIT)
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("prepare must not call the runtime owner"),
    )
    body = control.PaperCanaryPrepareBody(
        expected_commit_sha=COMMIT,
        expected_config_checksum=_config().checksum,
        expected_risk_version_token=store.current_paper_risk_config_checksum(),
        reason="bounded service test",
    )
    try:
        with pytest.raises(HTTPException) as unauthorized:
            control.paper_canary_prepare(body, authorization=None)
        assert unauthorized.value.status_code == 401
        result = control.paper_canary_prepare(body, authorization="Bearer external")
        assert result["ok"] is True and result["status"] == "READY_FOR_ARM"
        assert result["commit_sha"] == COMMIT and result["instrument"] == "AAPL"
        assert store.get_runtime_state().status == "READY_FOR_ARM"
        assert store.get_risk_state() is not None
        assert store.list_orders() == [] and store.list_fills() == []
        with pytest.raises(ValidationError):
            control.PaperCanaryPrepareBody(
                expected_commit_sha=COMMIT,
                expected_config_checksum=_config().checksum,
                expected_risk_version_token="a" * 20,
                capital="10000",
            )
    finally:
        store.close()


def test_control_prepare_requires_literal_double_opt_in_before_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "TRUE")
    body = control.PaperCanaryPrepareBody(
        expected_commit_sha=COMMIT,
        expected_config_checksum=_config().checksum,
        expected_risk_version_token="a" * 20,
    )
    with pytest.raises(HTTPException) as disabled:
        control.paper_canary_prepare(body, authorization="Bearer external")
    assert disabled.value.status_code == 404


def test_control_proxy_is_loopback_fixed_and_fails_closed_when_disabled_or_down(monkeypatch):
    monkeypatch.setenv("ATP_PAPER_CANARY_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("ATP_PAPER_CANARY_OWNER_PORT", "1")
    monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "1")
    with pytest.raises(HTTPException) as disabled:
        control._paper_owner_request("create", {"run_id": "run-1"})
    assert disabled.value.status_code == 404
    with pytest.raises(HTTPException) as reducing:
        control._paper_owner_request("recover", {"run_id": "run-1"})
    assert reducing.value.status_code == 503  # available while OFF; only the absent owner blocks it
    monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "true")
    with pytest.raises(HTTPException) as down:
        control._paper_owner_request("create", {"run_id": "run-1"})
    assert down.value.status_code == 503


def test_global_recovery_is_owner_first_read_only_in_control_and_legacy_and_paper(
    tmp_path, monkeypatch,
):
    store = _seed(tmp_path / "control-recovery.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    canary.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    store.upsert_md_health(
        symbol="AAPL",
        source="MASSIVE",
        status="READY",
        latency_ms=1,
        ts=datetime.now(UTC).isoformat(),
    )
    lock = threading.Lock()
    life = LifecycleManager(store)
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", life)
    monkeypatch.setattr(control.ctx, "lock", lock)
    proofs = []

    def owner_request(command, payload):
        assert command == "recover"
        assert lock.acquire(blocking=False), "owner loopback must run outside the Control DB lock"
        lock.release()
        run = store.get_paper_run(payload["run_id"])
        if run.status == "READY_FOR_ARM":
            result = canary.prove_reconciled_ready(run_id=run.run_id)
        else:
            result = canary.recover(run_id=run.run_id, reason=payload["reason"])
        serialized = control._paper_jsonable(result)
        proofs.append(serialized)
        return serialized

    monkeypatch.setattr(control, "_paper_owner_request", owner_request)

    def forbidden_constructor(*_args, **_kwargs):
        pytest.fail("Control recovery must never construct a DurablePaperCanary")

    monkeypatch.setattr(DurablePaperCanary, "__init__", forbidden_constructor)
    try:
        response = control.ctl_recover(authorization="Bearer external")
        assert response["ok"] is True
        assert response["status"] == RuntimeStatus.READY_FOR_ARM.value
        results = dict(response["results"])
        assert results["load_orders"] is True
        assert results["query_broker"] is True
        assert results["reconcile"] is True
        assert store.get_paper_run("run-1").status == "READY_FOR_ARM"

        with store.tx() as cur:
            store._exec(
                cur,
                "UPDATE paper_accounts SET cash=? WHERE run_id=?",
                ("9999.00000000", "run-1"),
            )
        stale = control.build_recovery_checks(
            store,
            broker_positions={},
            paper_recovery_proof=proofs[-1],
        )
        assert stale["load_orders"]() is True
        assert stale["query_broker"]() is False
        assert stale["reconcile"]() is False
    finally:
        store.close()


def test_submit_model_forbids_client_quote_config_commit_and_token():
    with pytest.raises(ValidationError):
        control.PaperCanarySubmitBody(
            run_id="run-1", decision_id="decision-1", side="BUY", quantity="1",
            instrument="AAPL", quote_bid="100", config={}, commit_sha=COMMIT,
            risk_config_checksum="token",
        )


def test_control_status_reads_db_and_kill_notifies_owner_only_after_durable_kill(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-status.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(
        control,
        "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("status must never call the owner"),
    )
    status = control.paper_canary_status("run-1", authorization="Bearer external")
    assert status["run"]["status"] == "READY_FOR_ARM"
    assert status["account"]["starting_cash"] == "10000.00000000"

    events = []
    cleanup_done = threading.Event()

    class ActiveStore:
        @staticmethod
        def list_paper_runs(*, status, limit):
            assert limit == 2
            events.append(f"read-{status}")
            return [SimpleNamespace(run_id="run-1", status="RUNNING")] if status == "RUNNING" else []

    monkeypatch.setattr(control.ctx, "store", ActiveStore())
    monkeypatch.setattr(control, "_emergency_kill", lambda: events.append("durable-kill") or "KILLED")
    monkeypatch.setattr(
        control,
        "_paper_owner_request",
        lambda command, payload: events.append((command, payload)) or cleanup_done.set() or {},
    )
    assert control.ctl_kill(authorization="Bearer external") == {"status": "KILLED"}
    assert cleanup_done.wait(timeout=1)
    assert events[0] == "durable-kill"
    assert events[-1][0] == "recover"
    store.close()


def test_control_disable_requires_clean_paper_proof_then_is_idempotently_disabled(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-disable.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    canary.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    stopped = canary.stop(run_id="run-1", reason="test complete")
    assert stopped.ok is True and stopped.run.status == "STOPPED"
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("already STOPPED retry must not call owner"),
    )
    body = control.PaperCanaryDisableBody(run_id="run-1", reason="bounded test complete")
    try:
        result = control.paper_canary_disable(body, authorization="Bearer external")
        assert result["ok"] is True and result["status"] == "DISABLED"
        assert result["run"]["status"] == "STOPPED"
        assert result["reconciliation"]["status"] == "PASS"
        assert LifecycleManager(store).status is RuntimeStatus.DISABLED
        actions = [event.action for event in store.recent_audit(10)]
        assert "PAPER_CANARY_DISABLE" in actions
        retry = control.paper_canary_disable(body, authorization="Bearer external")
        assert retry["status"] == "DISABLED"
        assert LifecycleManager(store).recover() is RuntimeStatus.DISABLED
    finally:
        store.close()


def test_control_disable_can_close_a_clean_never_activated_run(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-disable-never-started.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    run = canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    assert run.status == "READY_FOR_ARM" and run.started_at is None
    stopped = canary.stop(run_id="run-1", reason="abort before first activation")
    assert stopped.ok is True
    assert stopped.run.status == "STOPPED"
    assert stopped.reconciliation.status == "PASS"

    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control,
        "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("already STOPPED retry must not call owner"),
    )
    try:
        result = control.paper_canary_disable(
            control.PaperCanaryDisableBody(run_id="run-1"),
            authorization="Bearer external",
        )
        assert result["ok"] is True and result["status"] == "DISABLED"
        assert result["run"]["status"] == "STOPPED"
    finally:
        store.close()


def test_control_disable_refuses_corrupt_reconciliation_without_global_transition(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-disable-corrupt.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    canary.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    assert canary.stop(run_id="run-1", reason="test complete").ok is True
    with store.tx() as cur:
        store._exec(
            cur,
            "UPDATE paper_reconciliations SET breaks_json=? WHERE run_id=?",
            ('["LEDGER_BREAK"]', "run-1"),
        )
    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    body = control.PaperCanaryDisableBody(run_id="run-1")
    try:
        with pytest.raises(HTTPException) as blocked:
            control.paper_canary_disable(body, authorization="Bearer external")
        assert blocked.value.status_code == 409
        assert LifecycleManager(store).status is RuntimeStatus.RUNNING
    finally:
        store.close()


def test_control_disable_refuses_stopped_pass_with_open_paper_position(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-disable-open-position.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    canary.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    canary.submit(
        run_id="run-1",
        decision_id="buy-only",
        instrument="AAPL",
        side="BUY",
        quantity=D("1"),
        quote_bid=D("99.99"),
        quote_ask=D("100"),
        quote_ts=QUOTE,
        risk_config_checksum=store.current_paper_risk_config_checksum(),
    )
    stopped = canary.stop(run_id="run-1", reason="test must remain visibly non-flat")
    assert stopped.ok is True and stopped.reconciliation.status == "PASS"
    assert store.list_paper_positions(run_id="run-1")[0].quantity == D("1")

    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("already STOPPED run must not call owner"),
    )
    try:
        with pytest.raises(HTTPException) as blocked:
            control.paper_canary_disable(
                control.PaperCanaryDisableBody(run_id="run-1"),
                authorization="Bearer external",
            )
        assert blocked.value.status_code == 409
        with pytest.raises(HTTPException) as runless_bypass:
            control.paper_canary_disable(
                control.PaperCanaryDisableBody(),
                authorization="Bearer external",
            )
        assert runless_bypass.value.status_code == 409
        assert LifecycleManager(store).status is RuntimeStatus.RUNNING
    finally:
        store.close()


def test_control_disable_remains_risk_reducing_when_broker_flag_drifted(tmp_path, monkeypatch):
    store = _seed(tmp_path / "control-disable-broker-drift.db")
    canary = DurablePaperCanary(store, clock=lambda: NOW)
    canary.create_run(
        run_id="run-1", config=_config(), commit_sha=COMMIT, require_prepared=True,
    )
    canary.activate(run_id="run-1", confirm=CONFIRM_PHRASE)
    assert canary.stop(run_id="run-1", reason="flat bounded run complete").ok is True

    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setenv("BROKER_EXECUTION_ENABLED", "true")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("already STOPPED run must not call owner or broker"),
    )
    try:
        result = control.paper_canary_disable(
            control.PaperCanaryDisableBody(run_id="run-1"),
            authorization="Bearer external",
        )
        assert result["ok"] is True and result["status"] == "DISABLED"
        assert LifecycleManager(store).status is RuntimeStatus.DISABLED
    finally:
        store.close()


def test_control_kill_latches_durably_before_waiting_for_paper_lock(monkeypatch):
    durable_kill = threading.Event()
    finished = threading.Event()
    paper_lock = threading.Lock()
    control_lock = threading.Lock()
    outcome = {}

    class Store:
        @staticmethod
        def list_paper_runs(*, status, limit):
            assert status in set(control._PAPER_RECOVERY_STATUSES) and limit == 2
            return []

    def invoke_kill():
        try:
            outcome["result"] = control.ctl_kill(authorization="Bearer external")
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            outcome["error"] = exc
        finally:
            finished.set()

    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", Store())
    monkeypatch.setattr(control.ctx, "lock", control_lock)
    monkeypatch.setattr(control.ctx, "paper_lock", paper_lock)
    monkeypatch.setattr(
        control,
        "_emergency_kill",
        lambda: durable_kill.set() or "KILLED",
    )
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("no active run means no owner recovery"),
    )

    paper_lock.acquire()
    control_lock.acquire()
    worker = threading.Thread(target=invoke_kill, daemon=True)
    try:
        worker.start()
        latched_before_unlock = durable_kill.wait(timeout=1.0)
        returned_before_unlock = finished.wait(timeout=1.0)
    finally:
        paper_lock.release()
        control_lock.release()
        worker.join(timeout=2.0)

    assert latched_before_unlock is True
    assert returned_before_unlock is True
    assert finished.is_set() and "error" not in outcome
    assert outcome["result"] == {"status": "KILLED"}


@pytest.mark.parametrize("active_run", [False, True])
def test_control_disable_without_run_id_aborts_only_when_no_active_paper_run(
    tmp_path, monkeypatch, active_run,
):
    store = _seed(tmp_path / f"control-abort-no-run-{active_run}.db")
    if active_run:
        DurablePaperCanary(store, clock=lambda: NOW).create_run(
            run_id="active-run", config=_config(), commit_sha=COMMIT,
            require_prepared=True,
        )

    monkeypatch.setenv("ATP_CONTROL_TOKEN", "external")
    monkeypatch.setattr(control.ctx, "store", store)
    monkeypatch.setattr(control.ctx, "life", LifecycleManager(store))
    monkeypatch.setattr(control.ctx, "lock", threading.Lock())
    monkeypatch.setattr(control.ctx, "paper_lock", threading.RLock())
    monkeypatch.setattr(
        control, "_paper_owner_request",
        lambda *_args, **_kwargs: pytest.fail("run-less abort must never call the owner"),
    )
    body = control.PaperCanaryDisableBody(reason="abort before Paper run creation")
    try:
        if active_run:
            with pytest.raises(HTTPException) as blocked:
                control.paper_canary_disable(body, authorization="Bearer external")
            assert blocked.value.status_code == 409
            assert LifecycleManager(store).status is RuntimeStatus.RUNNING
        else:
            result = control.paper_canary_disable(body, authorization="Bearer external")
            assert result["ok"] is True and result["status"] == "DISABLED"
            assert LifecycleManager(store).status is RuntimeStatus.DISABLED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_trading_core_starts_exactly_one_owner(monkeypatch):
    constructed = []

    class FakeOwner:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.closed = False

        async def start(self):
            return None

        async def close(self):
            self.closed = True

        async def command(self, _path, _payload):
            return {}

    monkeypatch.setattr(trading, "PaperCanaryOwner", FakeOwner)
    monkeypatch.delenv("ATP_PAPER_CANARY_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("ATP_DURABLE_PAPER_CANARY_ENABLED", "false")
    service = object.__new__(trading.TradingCoreService)
    service.life = SimpleNamespace(status=SimpleNamespace(value="DISABLED"))
    service._paper_owner = None
    service._paper_commands = None
    service._sub_task = None
    service._quotes = {}
    service._stop = asyncio.Event()

    async def consume():
        await service._stop.wait()

    service._consume_quotes = consume
    await service.on_start()
    assert len(constructed) == 1
    with pytest.raises(RuntimeError, match="already started"):
        await service.on_start()
    await service.on_stop()
    assert len(constructed) == 1
