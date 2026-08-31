"""Single-owner service boundary for the durable Paper Canary.

Only ``TradingCoreService`` constructs this owner.  It opens a dedicated database connection,
constructs exactly one long-lived ``DurablePaperCanary`` and serializes every mutation through one
bounded asyncio queue/worker on the Trading Core event loop.  No broker, scheduler or Redis command
path exists here.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ..runtime.paper_canary import (
    DurablePaperCanary,
    PaperCanaryConfig,
    PaperCanaryConfigurationError,
    PaperCanaryError,
    PaperCanaryRequestError,
    PaperCanarySafetyError,
    PaperCanaryStateError,
    paper_canary_order_ids,
)
from ..research.intel.commit import CommitVerificationError, resolve_commit_sha
from ..store import PaperCanaryError as StorePaperCanaryError
from ..store import open_store
from ..store.money import QUANT
from .base import LoopbackCommandError, build_dsn

PAPER_CANARY_OWNER_PATHS = frozenset(
    {
        "/internal/paper-canary/create",
        "/internal/paper-canary/activate",
        "/internal/paper-canary/submit",
        "/internal/paper-canary/recover",
        "/internal/paper-canary/stop",
    }
)
_PATH_TO_COMMAND = {path: path.rsplit("/", 1)[-1] for path in PAPER_CANARY_OWNER_PATHS}
_OFFENSIVE_COMMANDS = frozenset({"create", "activate", "submit"})
_STOP = object()
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def paper_canary_offensive_enabled() -> bool:
    """Literal double opt-in; aliases/case folding are intentionally not accepted."""
    return (
        os.environ.get("ATP_DURABLE_PAPER_CANARY_ENABLED") == "true"
        and os.environ.get("BROKER_EXECUTION_ENABLED") == "false"
    )


def _exact_shape(payload: object, *, required: frozenset[str],
                 optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if type(payload) is not dict or not all(type(key) is str for key in payload):
        raise PaperCanaryRequestError("command body must be an exact JSON object")
    keys = frozenset(payload)
    if not required <= keys or not keys <= required | optional:
        raise PaperCanaryRequestError("command body has an invalid field set")
    return payload


def _decimal_text(value: object, field: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise PaperCanaryRequestError(f"{field} must be an exact decimal string")
    try:
        exact = Decimal(value)
        canonical = exact.quantize(QUANT)
    except (InvalidOperation, ValueError) as exc:
        raise PaperCanaryRequestError(f"{field} must be an exact decimal string") from exc
    if not exact.is_finite() or canonical != exact or (canonical == 0 and canonical.is_signed()):
        raise PaperCanaryRequestError(f"{field} must be finite, unsigned-zero and at most 8dp")
    if positive and canonical <= 0:
        raise PaperCanaryRequestError(f"{field} must be positive")
    return canonical


def _utc(value: object, field: str) -> datetime:
    try:
        parsed = value if type(value) is datetime else datetime.fromisoformat(value) if type(value) is str else None
        if parsed is None or parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed.astimezone(UTC)
    except Exception as exc:
        raise PaperCanarySafetyError(f"{field} must be an aware UTC timestamp") from exc


def _quote_decimal(value: object, field: str) -> Decimal:
    if type(value) not in {int, float}:
        raise PaperCanarySafetyError(f"owner quote {field} is not numeric")
    try:
        exact = Decimal(str(value))
        canonical = exact.quantize(QUANT)
    except (InvalidOperation, ValueError) as exc:
        raise PaperCanarySafetyError(f"owner quote {field} is invalid") from exc
    if not exact.is_finite() or exact <= 0 or canonical != exact:
        raise PaperCanarySafetyError(f"owner quote {field} is not positive finite 8dp")
    return canonical


def _jsonable(value: object) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) is datetime:
        return value.astimezone(UTC).isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if type(value) in {tuple, list}:
        return [_jsonable(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _jsonable(item) for key, item in value.items()}
    raise PaperCanarySafetyError("owner result is not JSON serializable")


@dataclasses.dataclass(slots=True)
class _Command:
    name: str
    payload: dict[str, Any]
    result: asyncio.Future


class PaperCanaryOwner:
    """One DurablePaperCanary, one dedicated Store connection and one mutation worker."""

    def __init__(
        self,
        *,
        quote_getter: Callable[[str], dict[str, Any] | None],
        trade_gate: Callable[[], tuple[bool, str]],
        risk_reduction_gate: Callable[[], tuple[bool, str]] | None = None,
        store_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        queue_limit: int = 32,
    ) -> None:
        if not callable(quote_getter):
            raise TypeError("quote_getter must be callable")
        if not callable(trade_gate):
            raise TypeError("trade_gate must be callable")
        if risk_reduction_gate is not None and not callable(risk_reduction_gate):
            raise TypeError("risk_reduction_gate must be callable or None")
        if type(queue_limit) is not int or queue_limit <= 0:
            raise ValueError("queue_limit must be a positive integer")
        self._quote_getter = quote_getter
        self._trade_gate = trade_gate
        self._risk_reduction_gate = risk_reduction_gate or trade_gate
        self._store_factory = store_factory or (lambda: open_store(build_dsn(), migrate=False))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_limit)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._store = None
        self._canary: DurablePaperCanary | None = None
        self._worker: asyncio.Task | None = None
        self._startup_recovery = None
        self._closing = False

    @property
    def started(self) -> bool:
        return self._worker is not None

    @property
    def startup_recovery(self):
        return self._startup_recovery

    @property
    def canary_instance(self) -> DurablePaperCanary | None:
        """Inspection hook used by health/tests; callers must never mutate through it."""
        return self._canary

    async def start(self) -> None:
        if self._worker is not None or self._store is not None:
            raise RuntimeError("Paper Canary owner already started")
        self._closing = False
        self._loop = asyncio.get_running_loop()
        store = self._store_factory()
        try:
            canary = DurablePaperCanary(store, clock=self._clock)
            self._store = store
            self._canary = canary
            await self._recover_active_once()
            self._worker = asyncio.create_task(self._worker_loop(), name="paper-canary-owner")
        except Exception:
            self._canary = None
            self._store = None
            store.close()
            raise

    async def _recover_active_once(self) -> None:
        store, canary = self._require_started_components()
        active = [
            *store.list_paper_runs(status="RUNNING", limit=2),
            *store.list_paper_runs(status="RECOVERY_REQUIRED", limit=2),
        ]
        if len(active) > 1:
            raise PaperCanarySafetyError("multiple active Paper Canary runs violate single ownership")
        if active:
            self._startup_recovery = canary.recover(
                run_id=active[0].run_id,
                reason="Trading Core owner startup recovery; explicit activation required",
            )

    def _require_started_components(self):
        if self._store is None or self._canary is None:
            raise RuntimeError("Paper Canary owner is not started")
        return self._store, self._canary

    async def close(self) -> None:
        store = self._store
        canary = self._canary
        self._closing = True
        try:
            worker = self._worker
            if worker is not None:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        if isinstance(item, _Command) and not item.result.done():
                            item.result.set_exception(
                                LoopbackCommandError(503, "Paper Canary owner is shutting down"),
                            )
                    finally:
                        self._queue.task_done()
                await self._queue.put(_STOP)
                await worker
                self._worker = None
            if store is not None and canary is not None:
                active = [
                    *store.list_paper_runs(status="RUNNING", limit=2),
                    *store.list_paper_runs(status="RECOVERY_REQUIRED", limit=2),
                ]
                if len(active) > 1:
                    raise PaperCanarySafetyError(
                        "multiple active Paper Canary runs violate single-owner shutdown",
                    )
                if active:
                    canary.recover(
                        run_id=active[0].run_id,
                        reason="Trading Core owner shutdown recovery; explicit activation required",
                    )
        finally:
            self._worker = None
            self._canary = None
            self._store = None
            self._loop = None
            if store is not None:
                store.close()

    async def command(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path not in PAPER_CANARY_OWNER_PATHS:
            raise LoopbackCommandError(404, "not found")
        name = _PATH_TO_COMMAND[path]
        if name in _OFFENSIVE_COMMANDS and not paper_canary_offensive_enabled():
            raise LoopbackCommandError(404, "durable Paper Canary is disabled")
        loop = asyncio.get_running_loop()
        if self._closing or self._loop is None or loop is not self._loop or self._worker is None:
            raise LoopbackCommandError(503, "Paper Canary owner is unavailable")
        result = loop.create_future()
        try:
            self._queue.put_nowait(_Command(name, payload, result))
        except asyncio.QueueFull as exc:
            raise LoopbackCommandError(503, "Paper Canary owner queue is full") from exc
        return await result

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, _Command)
                try:
                    value = self._dispatch(item.name, item.payload)
                except LoopbackCommandError as exc:
                    if not item.result.done():
                        item.result.set_exception(exc)
                except (PaperCanaryError, StorePaperCanaryError, ValueError, TypeError) as exc:
                    if not item.result.done():
                        item.result.set_exception(LoopbackCommandError(409, str(exc)))
                except Exception:  # noqa: BLE001 - owner worker must survive hostile Store/runtime failures
                    if not item.result.done():
                        item.result.set_exception(
                            LoopbackCommandError(500, "Paper Canary command failed closed"),
                        )
                else:
                    if not item.result.done():
                        item.result.set_result(_jsonable(value))
            finally:
                self._queue.task_done()

    def _dispatch(self, name: str, payload: dict[str, Any]):
        store, canary = self._require_started_components()
        if name == "create":
            body = _exact_shape(
                payload,
                required=frozenset({"run_id"}),
                optional=frozenset({"reason"}),
            )
            config = self._server_config()
            commit_sha = self._deployed_commit()
            return canary.create_run(
                run_id=body["run_id"],
                config=config,
                commit_sha=commit_sha,
                reason=body.get("reason"),
                require_prepared=True,
            )
        if name == "activate":
            body = _exact_shape(
                payload,
                required=frozenset({"run_id", "confirm"}),
                optional=frozenset({"reason"}),
            )
            if type(body["confirm"]) is not str:
                raise PaperCanaryStateError("activation requires the exact confirmation phrase")
            run = store.get_paper_run(body["run_id"])
            if run is None:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            self._require_server_binding(run)
            kwargs = {"run_id": body["run_id"], "confirm": body["confirm"]}
            if "reason" in body:
                kwargs["reason"] = body["reason"]
            return canary.activate(**kwargs)
        if name == "submit":
            body = _exact_shape(
                payload,
                required=frozenset({"run_id", "decision_id", "side", "quantity"}),
            )
            quantity = _decimal_text(body["quantity"], "quantity", positive=True)
            ids = paper_canary_order_ids(body["run_id"], body["decision_id"])
            existing = store.get_paper_order(ids.client_order_id)
            run = store.get_paper_run(body["run_id"])
            if run is None:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            config = PaperCanaryConfig.from_canonical_json(run.config_json)
            if existing is not None and existing.state == "FILLED":
                return canary.submit(
                    run_id=body["run_id"],
                    decision_id=body["decision_id"],
                    instrument=existing.instrument,
                    side=body["side"],
                    quantity=quantity,
                    quote_bid=existing.quote_bid,
                    quote_ask=existing.quote_ask,
                    quote_ts=_utc(existing.quote_ts, "persisted quote timestamp"),
                    risk_config_checksum=existing.risk_config_checksum,
                    asset_class="EQUITY",
                    multiplier=Decimal(1),
                    order_type="MARKET",
                )
            self._require_server_binding(run)
            gate = (
                self._risk_reduction_gate() if body["side"] == "SELL" else self._trade_gate()
            )
            if (
                type(gate) is not tuple
                or len(gate) != 2
                or type(gate[0]) is not bool
                or type(gate[1]) is not str
            ):
                raise PaperCanarySafetyError("Trading Core gate returned an invalid result")
            if gate[0] is not True:
                raise PaperCanarySafetyError(f"Trading Core gate refused Paper Canary submit: {gate[1]}")
            if existing is not None:
                now = self._clock()
                bid, ask, quote_ts = self._bound_quote(
                    store=store,
                    config=config,
                    raw=self._quote_getter(config.allowed_instrument),
                    now=now,
                )
                if (
                    existing.instrument != config.allowed_instrument
                    or existing.quote_bid != bid
                    or existing.quote_ask != ask
                    or _utc(existing.quote_ts, "persisted quote timestamp") != quote_ts
                ):
                    raise PaperCanarySafetyError(
                        "open Paper Canary order no longer matches the latest validated quote",
                    )
                return canary.submit(
                    run_id=body["run_id"],
                    decision_id=body["decision_id"],
                    instrument=existing.instrument,
                    side=body["side"],
                    quantity=quantity,
                    quote_bid=bid,
                    quote_ask=ask,
                    quote_ts=quote_ts,
                    risk_config_checksum=existing.risk_config_checksum,
                    asset_class="EQUITY",
                    multiplier=Decimal(1),
                    order_type="MARKET",
                )
            now = self._clock()
            bid, ask, quote_ts = self._bound_quote(
                store=store,
                config=config,
                raw=self._quote_getter(config.allowed_instrument),
                now=now,
            )
            return canary.submit(
                run_id=body["run_id"],
                decision_id=body["decision_id"],
                instrument=config.allowed_instrument,
                side=body["side"],
                quantity=quantity,
                quote_bid=bid,
                quote_ask=ask,
                quote_ts=quote_ts,
                risk_config_checksum=store.current_paper_risk_config_checksum(),
                asset_class="EQUITY",
                multiplier=Decimal(1),
                order_type="MARKET",
            )
        if name == "recover":
            body = _exact_shape(
                payload, required=frozenset({"run_id"}), optional=frozenset({"reason"}),
            )
            run = store.get_paper_run(body["run_id"])
            if run is None:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            if run.status == "READY_FOR_ARM":
                return canary.prove_reconciled_ready(run_id=run.run_id)
            kwargs = {"run_id": body["run_id"]}
            if "reason" in body:
                kwargs["reason"] = body["reason"]
            return canary.recover(**kwargs)
        if name == "stop":
            body = _exact_shape(
                payload, required=frozenset({"run_id"}), optional=frozenset({"reason"}),
            )
            kwargs = {"run_id": body["run_id"]}
            if "reason" in body:
                kwargs["reason"] = body["reason"]
            return canary.stop(**kwargs)
        raise LoopbackCommandError(404, "not found")

    @staticmethod
    def _server_config() -> PaperCanaryConfig:
        raw = os.environ.get("ATP_PAPER_CANARY_CONFIG_JSON")
        if type(raw) is not str or not raw:
            raise PaperCanaryConfigurationError(
                "ATP_PAPER_CANARY_CONFIG_JSON is required for run creation",
            )
        return PaperCanaryConfig.from_canonical_json(raw)

    @staticmethod
    def _deployed_commit() -> str:
        try:
            return resolve_commit_sha()
        except CommitVerificationError as exc:
            raise PaperCanaryConfigurationError(
                f"deployed commit verification failed: {exc.code}",
            ) from exc

    @classmethod
    def _require_server_binding(cls, run) -> None:
        commit_sha = cls._deployed_commit()
        if _COMMIT_SHA.fullmatch(commit_sha) is None:  # defensive after fail-closed resolver
            raise PaperCanaryConfigurationError("deployed commit must be an exact 40-lowerhex SHA")
        if run.commit_sha != commit_sha:
            raise PaperCanarySafetyError("Paper Canary run is bound to a different deployed commit")
        config = cls._server_config()
        if run.config_json != config.canonical_json() or run.config_checksum != config.checksum:
            raise PaperCanarySafetyError("Paper Canary run is bound to a different server config")

    @staticmethod
    def _bound_quote(*, store, config: PaperCanaryConfig, raw: object,
                     now: datetime) -> tuple[Decimal, Decimal, datetime]:
        if type(raw) is not dict:
            raise PaperCanarySafetyError("no validated Trading Core quote is available")
        required = frozenset(
            {"symbol", "asset_class", "source", "status", "market_data_type", "bid", "ask", "timestamp"}
        )
        if not required <= frozenset(raw):
            raise PaperCanarySafetyError("Trading Core quote is incomplete")
        if (
            raw["symbol"] != config.allowed_instrument
            or raw["asset_class"] != "STK"
            or raw["source"] != "MASSIVE"
            or raw["status"] != "READY"
            or raw["market_data_type"] != "REALTIME"
        ):
            raise PaperCanarySafetyError("Trading Core quote is outside the exact canary mandate")
        bid = _quote_decimal(raw["bid"], "bid")
        ask = _quote_decimal(raw["ask"], "ask")
        if ask < bid:
            raise PaperCanarySafetyError("Trading Core quote is crossed")
        quote_ts = _utc(raw["timestamp"], "Trading Core quote timestamp")
        current = _utc(now, "owner clock")
        age = Decimal(str((current - quote_ts).total_seconds()))
        if age < 0 or age > config.quote_max_age_s:
            raise PaperCanarySafetyError("Trading Core quote is stale or future-dated")
        health = [row for row in store.list_md_health() if row[0] == config.allowed_instrument]
        if len(health) != 1 or health[0][1] != "MASSIVE" or health[0][2] != "READY":
            raise PaperCanarySafetyError("matching database market-data health is not READY/MASSIVE")
        health_ts = _utc(health[0][4], "database market-data health timestamp")
        health_age = Decimal(str((current - health_ts).total_seconds()))
        if health_ts < quote_ts or health_age < 0 or health_age > config.quote_max_age_s:
            raise PaperCanarySafetyError("matching database market-data health is stale or inconsistent")
        return bid, ask, quote_ts
