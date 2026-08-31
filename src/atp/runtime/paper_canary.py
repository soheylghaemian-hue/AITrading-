"""Durable, deliberately narrow Paper Canary runtime.

The canary is not a general execution service. It supports one configured equity instrument,
full-fill MARKET orders, multiplier one and a long-only paper ledger. Store state is authoritative;
an in-memory broker is never used as a recovery oracle.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from threading import RLock
from typing import Any

from ..store import base as store_base
from ..store.money import QUANT
from .lifecycle import CONFIRM_PHRASE

CONFIG_TAG = "atp.paper-canary.config.v1"
IDENTITY_TAG = "atp.paper-canary.order-identity.v1"
RECONCILIATION_TAG = "atp.paper-canary.reconciliation.v1"
_CHECKSUM_PREFIX = "sha256:"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_NONTERMINAL_ORDER_STATES = frozenset({"INTENT", "AUTHORIZED"})
_TERMINAL_ORDER_STATES = frozenset({"REJECTED", "FILLED", "CANCELLED"})


class PaperCanaryError(RuntimeError):
    """Base error for the durable Paper Canary boundary."""


class PaperCanaryConfigurationError(ValueError, PaperCanaryError):
    """The canary mandate is malformed or outside its narrow scope."""


class PaperCanaryRequestError(ValueError, PaperCanaryError):
    """An order/quote request is malformed, stale or unsupported."""


class PaperCanaryStateError(PaperCanaryError):
    """The durable run is absent or not in a state that permits the operation."""


class PaperCanarySafetyError(PaperCanaryError):
    """A durable binding, reconstruction or safety invariant failed closed."""


def _fixed_money(value: Decimal) -> str:
    """Paper protocol representation: one lossless spelling, including exact zero."""
    if value == 0 and value.is_signed():
        raise ValueError("Paper money must not use signed zero")
    exact = value.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    return format(abs(exact) if exact == 0 else exact, "f")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _checksum(value: Any) -> str:
    raw = value if type(value) is str else _canonical_json(value)
    return _CHECKSUM_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _exact_decimal(value: object, field: str, *, positive: bool = False,
                   nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise PaperCanaryConfigurationError(f"{field} must be an exact finite Decimal")
    try:
        canonical = value.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise PaperCanaryConfigurationError(f"{field} is outside the supported precision") from exc
    if canonical != value:
        raise PaperCanaryConfigurationError(f"{field} must have at most 8 decimal places")
    if canonical == 0 and canonical.is_signed():
        raise PaperCanaryConfigurationError(f"{field} must not use signed zero")
    if positive and canonical <= 0:
        raise PaperCanaryConfigurationError(f"{field} must be positive")
    if nonnegative and canonical < 0:
        raise PaperCanaryConfigurationError(f"{field} must be non-negative")
    return canonical


def _request_decimal(value: object, field: str, *, positive: bool = False,
                     nonnegative: bool = False) -> Decimal:
    try:
        return _exact_decimal(value, field, positive=positive, nonnegative=nonnegative)
    except PaperCanaryConfigurationError as exc:
        raise PaperCanaryRequestError(str(exc)) from exc


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise PaperCanaryRequestError(f"{field} must be a canonical identifier")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise PaperCanaryConfigurationError(f"{field} must be an exact non-empty string")
    return value


def _checksum_token(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PaperCanaryRequestError(f"{field} must be an exact non-empty checksum token")
    return value


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        raise PaperCanaryRequestError(f"{field} must be an exact datetime")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(UTC)
    except Exception as exc:
        raise PaperCanaryRequestError(f"{field} must be an aware UTC datetime") from exc
    if offset != timedelta(0):
        raise PaperCanaryRequestError(f"{field} must be UTC")
    return normalized


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise PaperCanarySafetyError(f"{field} is not a persisted timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except Exception as exc:
        raise PaperCanarySafetyError(f"{field} is not a valid timestamp") from exc
    if offset != timedelta(0):
        raise PaperCanarySafetyError(f"{field} is not UTC")
    return parsed.astimezone(UTC)


def _row_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise PaperCanarySafetyError(f"{field} is not an exact finite Decimal")
    try:
        canonical = value.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise PaperCanarySafetyError(f"{field} has invalid precision") from exc
    if canonical != value:
        raise PaperCanarySafetyError(f"{field} exceeds durable precision")
    if canonical == 0 and canonical.is_signed():
        raise PaperCanarySafetyError(f"{field} uses noncanonical signed zero")
    return canonical


@dataclass(frozen=True, slots=True)
class PaperCanaryConfig:
    """Immutable, tagged and checksum-addressed one-instrument mandate."""

    mode: str
    allowed_instruments: tuple[str, ...]
    starting_cash: Decimal
    max_order_notional: Decimal
    max_gross_notional: Decimal
    max_daily_turnover: Decimal
    max_orders_per_day: int
    commission_per_unit: Decimal
    min_commission: Decimal
    slippage_bps: Decimal
    quote_max_age_s: Decimal

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode != "paper":
            raise PaperCanaryConfigurationError("mode must be exactly 'paper'")
        if type(self.allowed_instruments) is not tuple or len(self.allowed_instruments) != 1:
            raise PaperCanaryConfigurationError("exactly one allowed instrument is required")
        instrument = _nonempty_string(self.allowed_instruments[0], "allowed_instruments[0]")
        if instrument != instrument.upper() or len(instrument) > 128:
            raise PaperCanaryConfigurationError("allowed instrument must be one uppercase symbol")
        for name in (
            "starting_cash", "max_order_notional", "max_gross_notional",
            "max_daily_turnover", "quote_max_age_s",
        ):
            object.__setattr__(self, name, _exact_decimal(getattr(self, name), name, positive=True))
        for name in ("commission_per_unit", "min_commission", "slippage_bps"):
            object.__setattr__(
                self, name, _exact_decimal(getattr(self, name), name, nonnegative=True),
            )
        if type(self.max_orders_per_day) is not int or self.max_orders_per_day <= 0:
            raise PaperCanaryConfigurationError("max_orders_per_day must be an exact positive integer")
        if self.max_order_notional > self.max_gross_notional:
            raise PaperCanaryConfigurationError("max_order_notional cannot exceed max_gross_notional")
        if self.max_gross_notional > self.starting_cash:
            raise PaperCanaryConfigurationError("Paper Canary does not permit leverage")
        if self.slippage_bps >= Decimal(10000):
            raise PaperCanaryConfigurationError("slippage_bps must be below 10000")

    @property
    def allowed_instrument(self) -> str:
        return self.allowed_instruments[0]

    def as_canonical_dict(self) -> dict[str, object]:
        return {
            "asset_class": "EQUITY",
            "commission_per_unit": _fixed_money(self.commission_per_unit),
            "instrument": self.allowed_instrument,
            "max_daily_turnover": _fixed_money(self.max_daily_turnover),
            "max_gross_notional": _fixed_money(self.max_gross_notional),
            "max_order_notional": _fixed_money(self.max_order_notional),
            "max_orders": self.max_orders_per_day,
            "min_commission": _fixed_money(self.min_commission),
            "mode": self.mode,
            "quote_max_age_s": _fixed_money(self.quote_max_age_s),
            "slippage_bps": _fixed_money(self.slippage_bps),
            "starting_cash": _fixed_money(self.starting_cash),
            "tag": CONFIG_TAG,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.as_canonical_dict())

    @property
    def checksum(self) -> str:
        return _checksum(self.canonical_json())

    @classmethod
    def from_canonical_json(cls, raw: object) -> PaperCanaryConfig:
        if type(raw) is not str:
            raise PaperCanaryConfigurationError("config_json must be an exact string")

        def _object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise PaperCanaryConfigurationError(f"duplicate config field: {key}")
                result[key] = value
            return result

        try:
            payload = json.loads(raw, object_pairs_hook=_object)
        except PaperCanaryConfigurationError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PaperCanaryConfigurationError("config_json is invalid") from exc
        required = frozenset(
            {
                "tag", "mode", "instrument", "asset_class", "starting_cash",
                "max_order_notional", "max_gross_notional", "max_daily_turnover",
                "max_orders", "commission_per_unit", "min_commission", "slippage_bps",
                "quote_max_age_s",
            }
        )
        if type(payload) is not dict or frozenset(payload) != required:
            raise PaperCanaryConfigurationError("config_json has an invalid shape")
        if payload["tag"] != CONFIG_TAG or payload["asset_class"] != "EQUITY":
            raise PaperCanaryConfigurationError("config_json scope tag is invalid")
        decimal_names = (
            "starting_cash", "max_order_notional", "max_gross_notional",
            "max_daily_turnover", "commission_per_unit", "min_commission",
            "slippage_bps", "quote_max_age_s",
        )
        values: dict[str, object] = {}
        for name in decimal_names:
            if type(payload[name]) is not str:
                raise PaperCanaryConfigurationError(f"{name} must be a canonical decimal string")
            try:
                values[name] = Decimal(payload[name])
            except InvalidOperation as exc:
                raise PaperCanaryConfigurationError(f"{name} is invalid") from exc
        config = cls(
            mode=payload["mode"],
            allowed_instruments=(payload["instrument"],),
            max_orders_per_day=payload["max_orders"],
            **values,
        )
        if config.canonical_json() != raw:
            raise PaperCanaryConfigurationError("config_json is not canonical")
        return config


@dataclass(frozen=True, slots=True)
class PaperCanaryOrderIds:
    client_order_id: str
    idempotency_key: str
    broker_order_id: str
    fill_id: str
    broker_fill_id: str
    correlation_id: str


def paper_canary_order_ids(run_id: object, decision_id: object) -> PaperCanaryOrderIds:
    """Derive every stable external identity from exactly one run/decision pair."""
    run = _identifier(run_id, "run_id")
    decision = _identifier(decision_id, "decision_id")
    raw = _canonical_json({"decision_id": decision, "run_id": run, "tag": IDENTITY_TAG})
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return PaperCanaryOrderIds(
        client_order_id="pco_" + digest[:40],
        idempotency_key=_CHECKSUM_PREFIX + digest,
        broker_order_id="pcb_" + digest[:40],
        fill_id="pcf_" + digest[:40],
        broker_fill_id="pcbf_" + digest[:40],
        correlation_id="pcc_" + digest[:40],
    )


@dataclass(frozen=True, slots=True)
class PaperCanarySubmission:
    run: object
    order: object
    fill: object
    account: object
    position: object
    replayed: bool


@dataclass(frozen=True, slots=True)
class PaperCanaryRecovery:
    ok: bool
    run: object
    reconciliation: object | None
    cancelled_orders: tuple[object, ...]
    breaks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperCanarySnapshot:
    run: object
    account: object
    orders: tuple[object, ...]
    fills: tuple[object, ...]
    positions: tuple[object, ...]
    reconciliation: object | None


@dataclass(frozen=True, slots=True)
class _Replay:
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    quantity: Decimal
    avg_price: Decimal
    mark_price: Decimal


def _ledger_money(value: Decimal, field: str, breaks: list[str]) -> Decimal | None:
    try:
        if type(value) is not Decimal or not value.is_finite():
            raise ValueError
        canonical = value.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        breaks.append(f"{field} is not exact finite 8dp money")
        return None
    if canonical != value:
        breaks.append(f"{field} is not exactly representable at 8dp")
        return None
    if canonical == 0 and canonical.is_signed():
        breaks.append(f"{field} uses noncanonical signed zero")
        return None
    return canonical


class DurablePaperCanary:
    """One-process orchestrator over the Store's transactional Paper Canary contract.

    A newly constructed instance owns no pre-existing RUNNING run. This is intentional: after a
    process restart, callers must invoke :meth:`recover`, reconcile, and explicitly activate again.
    """

    def __init__(self, store, *, clock=None) -> None:
        required = (
            "current_paper_risk_config_checksum", "get_risk_config",
            "create_paper_run", "get_paper_run",
            "transition_paper_run", "get_paper_account", "get_or_create_paper_intent",
            "get_paper_order", "list_paper_orders", "transition_paper_order",
            "get_paper_fill", "list_paper_fills", "get_paper_position",
            "list_paper_positions", "commit_paper_fill_atomic",
            "cancel_paper_nonterminal_orders", "record_paper_reconciliation",
            "list_paper_reconciliations",
        )
        if any(not callable(getattr(store, name, None)) for name in required):
            raise PaperCanaryConfigurationError("store does not implement the Paper Canary contract")
        if clock is not None and not callable(clock):
            raise PaperCanaryConfigurationError("clock must be callable")
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._owned_versions: dict[str, int] = {}

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock result")

    def _get_run(self, run_id: object):
        canonical = _identifier(run_id, "run_id")
        run = self._store.get_paper_run(canonical)
        if run is None:
            raise PaperCanaryStateError("Paper Canary run does not exist")
        if type(run.run_id) is not str or run.run_id != canonical:
            raise PaperCanarySafetyError("Store returned the wrong Paper Canary run")
        if type(run.version) is not int or run.version < 0 or type(run.status) is not str:
            raise PaperCanarySafetyError("Paper Canary run state is malformed")
        return run

    @staticmethod
    def _config(run) -> PaperCanaryConfig:
        try:
            config = PaperCanaryConfig.from_canonical_json(run.config_json)
        except PaperCanaryConfigurationError as exc:
            raise PaperCanarySafetyError("durable Paper Canary config is invalid") from exc
        if type(run.config_checksum) is not str or config.checksum != run.config_checksum:
            raise PaperCanarySafetyError("durable Paper Canary config checksum does not match")
        return config

    def _current_risk_token(self) -> str:
        token = self._store.current_paper_risk_config_checksum()
        return _checksum_token(token, "current risk configuration checksum")

    def _current_risk_capital(self) -> Decimal:
        row = self._store.get_risk_config()
        if row is None:
            raise PaperCanarySafetyError("canonical risk capital is missing")
        capital = _row_decimal(row.capital, "risk_config.capital")
        if capital <= 0:
            raise PaperCanarySafetyError("canonical risk capital must be positive")
        return capital

    def create_run(
        self,
        *,
        run_id: str,
        config: PaperCanaryConfig,
        commit_sha: str,
        risk_config_checksum: str | None = None,
        reason: str | None = None,
    ):
        """Create an idempotent READY_FOR_ARM run bound to source, risk and exact config."""
        canonical_run = _identifier(run_id, "run_id")
        if type(config) is not PaperCanaryConfig:
            raise PaperCanaryConfigurationError("config must be exact PaperCanaryConfig")
        if type(commit_sha) is not str or _COMMIT_RE.fullmatch(commit_sha) is None:
            raise PaperCanaryConfigurationError("commit_sha must be a full lowercase 40-hex SHA")
        if reason is not None and (type(reason) is not str or not reason):
            raise PaperCanaryConfigurationError("reason must be None or a non-empty string")
        current = self._current_risk_token()
        if config.starting_cash > self._current_risk_capital():
            raise PaperCanarySafetyError(
                "Paper Canary starting_cash exceeds canonical risk capital"
            )
        bound = current if risk_config_checksum is None else _checksum_token(
            risk_config_checksum, "risk_config_checksum"
        )
        if bound != current:
            raise PaperCanarySafetyError("risk configuration changed before run creation")
        run = self._store.create_paper_run(
            run_id=canonical_run,
            config_json=config.canonical_json(),
            risk_config_checksum=bound,
            commit_sha=commit_sha,
            starting_cash=config.starting_cash,
            status="READY_FOR_ARM",
            reason=reason,
        )
        if run.status != "READY_FOR_ARM" or self._config(run) != config:
            raise PaperCanarySafetyError("Store did not persist the requested Paper Canary run")
        return run

    def status(self, run_id: str) -> PaperCanarySnapshot:
        with self._lock:
            run = self._get_run(run_id)
            self._config(run)
            account = self._store.get_paper_account(run.run_id)
            if account is None:
                raise PaperCanarySafetyError("durable Paper Canary account is missing")
            orders = tuple(self._store.list_paper_orders(run_id=run.run_id))
            fills = tuple(self._store.list_paper_fills(run_id=run.run_id))
            positions = tuple(self._store.list_paper_positions(run_id=run.run_id))
            reconciliations = tuple(self._store.list_paper_reconciliations(
                run_id=run.run_id,
                limit=10000,
            ))
            return PaperCanarySnapshot(
                run, account, orders, fills, positions,
                reconciliations[-1] if reconciliations else None,
            )

    def activate(self, *, run_id: str, confirm, reason: str = "explicit paper activation"):
        """Move READY_FOR_ARM to RUNNING with exact confirmation and current risk binding."""
        confirmed = confirm is True or (type(confirm) is str and confirm == CONFIRM_PHRASE)
        if not confirmed:
            raise PaperCanaryStateError("Paper Canary activation requires exact confirmation")
        if type(reason) is not str or not reason:
            raise PaperCanaryConfigurationError("activation reason must be a non-empty string")
        with self._lock:
            run = self._get_run(run_id)
            self._config(run)
            if run.status != "READY_FOR_ARM":
                raise PaperCanaryStateError("Paper Canary activation requires READY_FOR_ARM")
            runtime = self._store.get_runtime_state()
            risk_state = self._store.get_risk_state()
            kill = self._store.get_kill_switch()
            if (
                runtime is None
                or runtime.status != "RUNNING"
                or risk_state is None
                or risk_state.halted is not False
                or risk_state.killed is not False
                or kill.engaged is not False
            ):
                raise PaperCanarySafetyError(
                    "global runtime/risk boundary is not healthy RUNNING"
                )
            if self._current_risk_token() != run.risk_config_checksum:
                raise PaperCanarySafetyError("risk configuration changed before activation")
            updated = self._store.transition_paper_run(
                run_id=run.run_id,
                expected_status="READY_FOR_ARM",
                expected_version=run.version,
                new_status="RUNNING",
                reason=reason,
            )
            if updated.status != "RUNNING":
                raise PaperCanarySafetyError("Paper Canary activation did not reach RUNNING")
            self._owned_versions[updated.run_id] = updated.version
            return updated

    def _require_owned(self, run) -> None:
        owned = self._owned_versions.get(run.run_id)
        if run.status != "RUNNING" or owned is None or owned != run.version:
            raise PaperCanaryStateError(
                "run is not RUNNING in this process; recovery and explicit activation are required"
            )

    def _require_owned_running(self, run) -> None:
        self._require_owned(run)
        if self._current_risk_token() != run.risk_config_checksum:
            self._owned_versions.pop(run.run_id, None)
            raise PaperCanarySafetyError("risk configuration changed after activation")

    @staticmethod
    def _request(
        *,
        config: PaperCanaryConfig,
        instrument: object,
        asset_class: object,
        multiplier: object,
        side: object,
        quantity: object,
        order_type: object,
        quote_bid: object,
        quote_ask: object,
        quote_ts: object,
    ) -> tuple[str, str, Decimal, str, Decimal, Decimal, datetime]:
        if type(instrument) is not str or instrument != config.allowed_instrument:
            raise PaperCanaryRequestError("instrument is outside the one-instrument canary mandate")
        if type(asset_class) is not str or asset_class != "EQUITY":
            raise PaperCanaryRequestError("Paper Canary supports exact EQUITY only")
        mult = _request_decimal(multiplier, "multiplier", positive=True)
        if mult != Decimal(1):
            raise PaperCanaryRequestError("Paper Canary requires multiplier exactly 1")
        if type(side) is not str or side not in {"BUY", "SELL"}:
            raise PaperCanaryRequestError("Paper Canary supports BUY/SELL only")
        if type(order_type) is not str or order_type != "MARKET":
            raise PaperCanaryRequestError("Paper Canary supports exact MARKET full-fill orders only")
        qty = _request_decimal(quantity, "quantity", positive=True)
        bid = _request_decimal(quote_bid, "quote_bid", positive=True)
        ask = _request_decimal(quote_ask, "quote_ask", positive=True)
        if ask < bid:
            raise PaperCanaryRequestError("quote ask cannot be below bid")
        timestamp = _utc(quote_ts, "quote_ts")
        return instrument, side, qty, order_type, bid, ask, timestamp

    @staticmethod
    def _fill_terms(config: PaperCanaryConfig, *, side: str, quantity: Decimal,
                    quote_bid: Decimal, quote_ask: Decimal) -> tuple[Decimal, Decimal]:
        slip = config.slippage_bps / Decimal(10000)
        raw_price = quote_ask * (Decimal(1) + slip) if side == "BUY" else (
            quote_bid * (Decimal(1) - slip)
        )
        price = raw_price.quantize(QUANT, rounding=ROUND_HALF_EVEN)
        if price <= 0:
            raise PaperCanaryRequestError("slippage produced an invalid paper fill price")
        commission = max(config.min_commission, config.commission_per_unit * quantity)
        return price, commission.quantize(QUANT, rounding=ROUND_HALF_EVEN)

    @staticmethod
    def _fresh(config: PaperCanaryConfig, quote_ts: datetime, now: datetime) -> None:
        delta = now - quote_ts
        age_us = (
            (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        )
        age = Decimal(age_us) / Decimal(1000000)
        if age < 0:
            raise PaperCanaryRequestError("future quote rejected")
        if age > config.quote_max_age_s:
            raise PaperCanaryRequestError("stale quote rejected")

    @staticmethod
    def _assert_order_request(
        order,
        *,
        run_id: str,
        decision_id: str,
        ids: PaperCanaryOrderIds,
        instrument: str,
        side: str,
        quantity: Decimal,
        order_type: str,
        quote_bid: Decimal,
        quote_ask: Decimal,
        quote_ts: str,
        risk_config_checksum: str,
        request_checksum: str,
    ) -> None:
        expected = (
            order.client_order_id == ids.client_order_id
            and order.run_id == run_id
            and order.idempotency_key == ids.idempotency_key
            and order.decision_id == decision_id
            and order.instrument == instrument
            and order.side == side
            and order.quantity == quantity
            and order.order_type == order_type
            and order.quote_bid == quote_bid
            and order.quote_ask == quote_ask
            and order.quote_ts == quote_ts
            and order.risk_config_checksum == risk_config_checksum
            and order.request_checksum == request_checksum
            and order.correlation_id == ids.correlation_id
        )
        if not expected:
            raise store_base.PaperCanaryConflict(
                "run decision was reused with different immutable request content"
            )

    def _submission_from_rows(self, *, run, order, replayed: bool) -> PaperCanarySubmission:
        fill = self._store.get_paper_fill(order.client_order_id)
        account = self._store.get_paper_account(run.run_id)
        position = self._store.get_paper_position(run_id=run.run_id, instrument=order.instrument)
        if fill is None or account is None or position is None:
            raise PaperCanarySafetyError("FILLED order is missing durable ledger projections")
        return PaperCanarySubmission(run, order, fill, account, position, replayed)

    def submit(
        self,
        *,
        run_id: str,
        decision_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        quote_bid: Decimal,
        quote_ask: Decimal,
        quote_ts: datetime,
        risk_config_checksum: str,
        asset_class: str = "EQUITY",
        multiplier: Decimal = Decimal(1),
        order_type: str = "MARKET",
    ) -> PaperCanarySubmission:
        """Persist one intent, authorize once, then commit one deterministic full paper fill.

        Exact retries of an already FILLED decision return the durable result even after the quote has
        aged or the run has left RUNNING. A changed request for the same decision always conflicts.
        """
        canonical_run = _identifier(run_id, "run_id")
        canonical_decision = _identifier(decision_id, "decision_id")
        token = _checksum_token(risk_config_checksum, "risk_config_checksum")
        with self._lock:
            run = self._get_run(canonical_run)
            config = self._config(run)
            inst, direction, qty, kind, bid, ask, quote_time = self._request(
                config=config,
                instrument=instrument,
                asset_class=asset_class,
                multiplier=multiplier,
                side=side,
                quantity=quantity,
                order_type=order_type,
                quote_bid=quote_bid,
                quote_ask=quote_ask,
                quote_ts=quote_ts,
            )
            ids = paper_canary_order_ids(canonical_run, canonical_decision)
            quote_iso = _iso_utc(quote_time)
            request_checksum = store_base.paper_canary_request_checksum(
                run_id=canonical_run,
                decision_id=canonical_decision,
                client_order_id=ids.client_order_id,
                instrument=inst,
                side=direction,
                quantity=qty,
                order_type=kind,
                quote_bid=bid,
                quote_ask=ask,
                quote_ts=quote_iso,
                risk_config_checksum=token,
                config_checksum=run.config_checksum,
                asset_class="EQUITY",
                multiplier=Decimal(1),
            )

            existing = self._store.get_paper_order(ids.client_order_id)
            if existing is not None:
                self._assert_order_request(
                    existing,
                    run_id=canonical_run,
                    decision_id=canonical_decision,
                    ids=ids,
                    instrument=inst,
                    side=direction,
                    quantity=qty,
                    order_type=kind,
                    quote_bid=bid,
                    quote_ask=ask,
                    quote_ts=quote_iso,
                    risk_config_checksum=token,
                    request_checksum=request_checksum,
                )
                if existing.state == "FILLED":
                    return self._submission_from_rows(run=run, order=existing, replayed=True)
                if existing.state in _TERMINAL_ORDER_STATES:
                    raise PaperCanaryStateError(f"paper order is terminal {existing.state}")

            self._require_owned_running(run)
            self._fresh(config, quote_time, self._now())
            order = self._store.get_or_create_paper_intent(
                run_id=canonical_run,
                idempotency_key=ids.idempotency_key,
                decision_id=canonical_decision,
                instrument=inst,
                side=direction,
                quantity=qty,
                quote_bid=bid,
                quote_ask=ask,
                quote_ts=quote_iso,
                risk_config_checksum=token,
                correlation_id=ids.correlation_id,
                client_order_id=ids.client_order_id,
                order_type=kind,
            )
            self._assert_order_request(
                order,
                run_id=canonical_run,
                decision_id=canonical_decision,
                ids=ids,
                instrument=inst,
                side=direction,
                quantity=qty,
                order_type=kind,
                quote_bid=bid,
                quote_ask=ask,
                quote_ts=quote_iso,
                risk_config_checksum=token,
                request_checksum=request_checksum,
            )
            if order.state == "FILLED":
                return self._submission_from_rows(run=run, order=order, replayed=True)
            if order.state == "INTENT":
                order = self._store.transition_paper_order(
                    client_order_id=order.client_order_id,
                    expected_status="INTENT",
                    expected_version=order.version,
                    new_status="AUTHORIZED",
                    reason="durable Paper Canary mandate authorized",
                )
            if order.state != "AUTHORIZED":
                raise PaperCanaryStateError(f"paper order cannot submit from {order.state}")

            fill_price, commission = self._fill_terms(
                config, side=direction, quantity=qty, quote_bid=bid, quote_ask=ask,
            )
            try:
                trade_now = self._now()
                self._fresh(config, quote_time, trade_now)
                result = self._store.commit_paper_fill_atomic(
                    run_id=canonical_run,
                    client_order_id=order.client_order_id,
                    expected_order_version=order.version,
                    fill_id=ids.fill_id,
                    broker_order_id=ids.broker_order_id,
                    broker_fill_id=ids.broker_fill_id,
                    instrument=inst,
                    side=direction,
                    quantity=qty,
                    price=fill_price,
                    commission=commission,
                    multiplier=Decimal(1),
                    quote_ts=quote_iso,
                    ts=_iso_utc(trade_now),
                )
            except (
                PaperCanaryRequestError,
                store_base.PaperCanarySafetyError,
                store_base.PaperCanaryStateError,
            ):
                current = self._store.get_paper_order(order.client_order_id)
                if current is not None and current.state == "AUTHORIZED":
                    with suppress(store_base.PaperCanaryConflict, store_base.PaperCanaryStateError):
                        self._store.transition_paper_order(
                            client_order_id=current.client_order_id,
                            expected_status="AUTHORIZED",
                            expected_version=current.version,
                            new_status="REJECTED",
                            reason="atomic paper fill safety check rejected",
                        )
                raise
            return PaperCanarySubmission(
                self._get_run(canonical_run), result.order, result.fill,
                result.account, result.position, False,
            )

    @staticmethod
    def _actual_checksums(*, fills, positions, account) -> tuple[str, str, str]:
        fill_payload = [
            {
                "broker_fill_id": row.broker_fill_id,
                "client_order_id": row.client_order_id,
                "commission": _fixed_money(row.commission),
                "fill_id": row.fill_id,
                "instrument": row.instrument,
                "ledger_seq": row.ledger_seq,
                "multiplier": _fixed_money(row.multiplier),
                "price": _fixed_money(row.price),
                "quantity": _fixed_money(row.quantity),
                "quote_ts": row.quote_ts,
                "side": row.side,
                "ts": row.ts,
            }
            for row in fills
        ]
        position_payload = [
            {
                "avg_price": _fixed_money(row.avg_price),
                "instrument": row.instrument,
                "mark_price": _fixed_money(row.mark_price),
                "quantity": _fixed_money(row.quantity),
                "realized_pnl": _fixed_money(row.realized_pnl),
                "run_id": row.run_id,
                "version": row.version,
            }
            for row in positions
        ]
        account_payload = {
            "cash": _fixed_money(account.cash),
            "equity": _fixed_money(account.equity),
            "gross_exposure": _fixed_money(account.gross_exposure),
            "net_exposure": _fixed_money(account.net_exposure),
            "realized_pnl": _fixed_money(account.realized_pnl),
            "run_id": account.run_id,
            "starting_cash": _fixed_money(account.starting_cash),
            "version": account.version,
        }
        return (
            _checksum({"fills": fill_payload, "tag": RECONCILIATION_TAG}),
            _checksum({"positions": position_payload, "tag": RECONCILIATION_TAG}),
            _checksum({"account": account_payload, "tag": RECONCILIATION_TAG}),
        )

    @staticmethod
    def _replay(
        *,
        run,
        config: PaperCanaryConfig,
        orders,
        fills,
        positions,
        account,
    ) -> tuple[_Replay | None, list[str]]:
        breaks: list[str] = []
        if account is None:
            return None, ["paper account is missing"]
        try:
            if account.run_id != run.run_id:
                breaks.append("paper account belongs to another run")
            start = _row_decimal(account.starting_cash, "account.starting_cash")
        except Exception as exc:  # noqa: BLE001 - hostile durable account must record a break
            breaks.append(f"paper account is malformed: {type(exc).__name__}")
            return None, breaks
        if start != config.starting_cash:
            breaks.append("account starting_cash differs from the immutable config")

        order_by_id = {}
        for order in orders:
            try:
                if order.client_order_id in order_by_id:
                    breaks.append(f"duplicate order row {order.client_order_id}")
                    continue
                order_by_id[order.client_order_id] = order
                ids = paper_canary_order_ids(run.run_id, order.decision_id)
                canonical_quote_ts = _iso_utc(
                    _parse_utc(order.quote_ts, f"order {order.client_order_id}.quote_ts")
                )
                checksum = store_base.paper_canary_request_checksum(
                    run_id=run.run_id,
                    decision_id=order.decision_id,
                    client_order_id=ids.client_order_id,
                    instrument=order.instrument,
                    side=order.side,
                    quantity=order.quantity,
                    order_type=order.order_type,
                    quote_bid=order.quote_bid,
                    quote_ask=order.quote_ask,
                    quote_ts=order.quote_ts,
                    risk_config_checksum=order.risk_config_checksum,
                    config_checksum=run.config_checksum,
                    asset_class="EQUITY",
                    multiplier=Decimal(1),
                )
                if (
                    order.run_id != run.run_id
                    or order.instrument != config.allowed_instrument
                    or order.order_type != "MARKET"
                    or order.side not in {"BUY", "SELL"}
                    or order.state not in _TERMINAL_ORDER_STATES
                    or (order.state != "FILLED" and order.broker_order_id is not None)
                    or order.quote_ts != canonical_quote_ts
                    or order.client_order_id != ids.client_order_id
                    or order.idempotency_key != ids.idempotency_key
                    or order.correlation_id != ids.correlation_id
                    or order.risk_config_checksum != run.risk_config_checksum
                    or order.request_checksum != checksum
                ):
                    breaks.append(f"order {order.client_order_id} immutable binding is inconsistent")
            except Exception as exc:  # noqa: BLE001 - hostile durable row must record a break
                breaks.append(
                    f"order {getattr(order, 'client_order_id', '?')} is malformed: {type(exc).__name__}"
                )

        cash = start
        quantity = Decimal(0)
        average = Decimal(0)
        mark = Decimal(0)
        realized = Decimal(0)
        seen_fill_orders: set[str] = set()
        seen_sequences: set[int] = set()
        last_sequence = -1
        per_day_turnover: dict[str, Decimal] = {}
        per_day_orders: dict[str, int] = {}

        for fill in fills:
            try:
                order = order_by_id.get(fill.client_order_id)
                if order is None:
                    breaks.append(f"orphan fill {fill.fill_id}")
                    continue
                if fill.client_order_id in seen_fill_orders:
                    breaks.append(f"duplicate fill for order {fill.client_order_id}")
                    continue
                seen_fill_orders.add(fill.client_order_id)
                if type(fill.ledger_seq) is not int or fill.ledger_seq <= last_sequence:
                    breaks.append(f"fill {fill.fill_id} ledger sequence is not strictly increasing")
                if fill.ledger_seq in seen_sequences:
                    breaks.append(f"fill {fill.fill_id} duplicates ledger sequence")
                seen_sequences.add(fill.ledger_seq)
                last_sequence = max(last_sequence, fill.ledger_seq)

                qty = _row_decimal(fill.quantity, f"fill {fill.fill_id} quantity")
                price = _row_decimal(fill.price, f"fill {fill.fill_id} price")
                fee = _row_decimal(fill.commission, f"fill {fill.fill_id} commission")
                mult = _row_decimal(fill.multiplier, f"fill {fill.fill_id} multiplier")
                expected_price, expected_fee = DurablePaperCanary._fill_terms(
                    config,
                    side=fill.side,
                    quantity=qty,
                    quote_bid=order.quote_bid,
                    quote_ask=order.quote_ask,
                )
                ids = paper_canary_order_ids(run.run_id, order.decision_id)
                if (
                    order.state != "FILLED"
                    or order.broker_order_id != ids.broker_order_id
                    or fill.fill_id != ids.fill_id
                    or fill.broker_fill_id != ids.broker_fill_id
                    or fill.instrument != order.instrument
                    or fill.side != order.side
                    or qty != order.quantity
                    or fill.quote_ts != order.quote_ts
                    or mult != Decimal(1)
                    or price != expected_price
                    or fee != expected_fee
                ):
                    breaks.append(f"fill {fill.fill_id} does not match its authorized full order")
                quote_time = _parse_utc(fill.quote_ts, f"fill {fill.fill_id}.quote_ts")
                fill_time = _parse_utc(fill.ts, f"fill {fill.fill_id}.ts")
                if fill.quote_ts != _iso_utc(quote_time) or fill.ts != _iso_utc(fill_time):
                    breaks.append(f"fill {fill.fill_id} timestamps are not canonical UTC")
                if fill_time < quote_time:
                    breaks.append(f"fill {fill.fill_id} predates its quote")

                notional = _ledger_money(qty * price * mult, f"fill {fill.fill_id} notional", breaks)
                if notional is None:
                    continue
                day = fill_time.date().isoformat()
                per_day_turnover[day] = per_day_turnover.get(day, Decimal(0)) + notional
                per_day_orders[day] = per_day_orders.get(day, 0) + 1
                if notional > config.max_order_notional:
                    breaks.append(f"fill {fill.fill_id} exceeds max_order_notional")

                if fill.side == "BUY":
                    new_quantity = quantity + qty
                    weighted = quantity * average + qty * price
                    new_average = _ledger_money(
                        weighted / new_quantity, f"fill {fill.fill_id} average price", breaks,
                    )
                    new_cash = _ledger_money(cash - notional - fee, f"fill {fill.fill_id} cash", breaks)
                    if new_average is None or new_cash is None:
                        continue
                elif fill.side == "SELL":
                    if qty > quantity:
                        breaks.append(f"fill {fill.fill_id} creates a short position")
                        continue
                    new_quantity = quantity - qty
                    new_average = Decimal(0) if new_quantity == 0 else average
                    delta = (price - average) * qty * mult - fee
                    new_realized = _ledger_money(
                        realized + delta, f"fill {fill.fill_id} realized PnL", breaks,
                    )
                    new_cash = _ledger_money(cash + notional - fee, f"fill {fill.fill_id} cash", breaks)
                    if new_realized is None or new_cash is None:
                        continue
                    realized = new_realized
                else:
                    breaks.append(f"fill {fill.fill_id} has unsupported side")
                    continue
                quantity, average, cash, mark = new_quantity, new_average, new_cash, price
                gross = _ledger_money(quantity * mark, f"fill {fill.fill_id} gross exposure", breaks)
                if gross is not None and gross > config.max_gross_notional:
                    breaks.append(f"fill {fill.fill_id} exceeds max_gross_notional")
            except Exception as exc:  # noqa: BLE001 - hostile durable row must record a break
                breaks.append(
                    f"fill {getattr(fill, 'fill_id', '?')} is malformed: {type(exc).__name__}"
                )

        for order in orders:
            try:
                has_fill = order.client_order_id in seen_fill_orders
                if order.state == "FILLED" and not has_fill:
                    breaks.append(f"FILLED order {order.client_order_id} has no fill")
                if order.state != "FILLED" and has_fill:
                    breaks.append(f"terminal non-FILLED order {order.client_order_id} has a fill")
            except Exception as exc:  # noqa: BLE001 - hostile durable order must record a break
                breaks.append(f"paper order is malformed: {type(exc).__name__}")
        for day, turnover in per_day_turnover.items():
            if turnover > config.max_daily_turnover:
                breaks.append(f"daily turnover cap exceeded on {day}")
        for day, count in per_day_orders.items():
            if count > config.max_orders_per_day:
                breaks.append(f"daily order-count cap exceeded on {day}")

        net = _ledger_money(quantity * mark, "replayed net exposure", breaks)
        if net is None:
            return None, breaks
        equity = _ledger_money(cash + net, "replayed equity", breaks)
        if equity is None:
            return None, breaks
        replay = _Replay(cash, equity, realized, abs(net), net, quantity, average, mark)

        try:
            expected_positions = [] if not fills else [config.allowed_instrument]
            actual_instruments = [row.instrument for row in positions]
            if actual_instruments != expected_positions:
                breaks.append("paper position set differs from the deterministic fill replay")
            elif positions:
                row = positions[0]
                if (
                    row.run_id != run.run_id
                    or row.quantity != replay.quantity
                    or row.avg_price != replay.avg_price
                    or row.mark_price != replay.mark_price
                    or row.realized_pnl != replay.realized_pnl
                ):
                    breaks.append("paper position projection differs from the fill replay")
        except Exception as exc:  # noqa: BLE001 - hostile projection must record a break
            breaks.append(f"paper position projection is malformed: {type(exc).__name__}")
        try:
            if (
                account.cash != replay.cash
                or account.equity != replay.equity
                or account.realized_pnl != replay.realized_pnl
                or account.gross_exposure != replay.gross_exposure
                or account.net_exposure != replay.net_exposure
            ):
                breaks.append("paper account projection differs from the fill replay")
        except Exception as exc:  # noqa: BLE001 - hostile projection must record a break
            breaks.append(f"paper account projection is malformed: {type(exc).__name__}")
        return replay, breaks

    def prove_reconciled_ready(self, *, run_id: str) -> PaperCanaryRecovery:
        """Verify a persisted PASS for the current READY cycle without changing durable state."""
        with self._lock:
            return verify_paper_reconciled_ready(self._store, run_id=run_id)

    def recover(self, *, run_id: str, reason: str = "process restart recovery") -> PaperCanaryRecovery:
        """Cancel pending work, replay the ledger and move only a clean run to READY_FOR_ARM."""
        if type(reason) is not str or not reason:
            raise PaperCanaryConfigurationError("recovery reason must be a non-empty string")
        with self._lock:
            run = self._get_run(run_id)
            config = self._config(run)
            self._owned_versions.pop(run.run_id, None)
            if run.status == "RUNNING":
                run = self._store.transition_paper_run(
                    run_id=run.run_id,
                    expected_status="RUNNING",
                    expected_version=run.version,
                    new_status="RECOVERY_REQUIRED",
                    reason=reason,
                )
            elif run.status != "RECOVERY_REQUIRED":
                raise PaperCanaryStateError(
                    "recovery requires a prior RUNNING crash or RECOVERY_REQUIRED run"
                )

            cancelled = tuple(self._store.cancel_paper_nonterminal_orders(
                run_id=run.run_id,
                reason="recovery: never resubmit pending order",
            ))
            orders = tuple(self._store.list_paper_orders(run_id=run.run_id))
            fills = tuple(self._store.list_paper_fills(run_id=run.run_id))
            positions = tuple(self._store.list_paper_positions(run_id=run.run_id))
            account = self._store.get_paper_account(run.run_id)
            _, breaks = self._replay(
                run=run,
                config=config,
                orders=orders,
                fills=fills,
                positions=positions,
                account=account,
            )
            open_orders = 0
            for order in orders:
                try:
                    if order.state in _NONTERMINAL_ORDER_STATES:
                        open_orders += 1
                except Exception as exc:  # noqa: BLE001 - malformed order is conservatively open
                    open_orders += 1
                    breaks.append(f"paper order state is malformed: {type(exc).__name__}")
            if open_orders:
                breaks.append("nonterminal paper orders remain after recovery cancellation")
            if account is None:
                fills_checksum = _checksum({"fills": [], "tag": RECONCILIATION_TAG})
                positions_checksum = _checksum({"positions": [], "tag": RECONCILIATION_TAG})
                account_checksum = _checksum({"account": None, "tag": RECONCILIATION_TAG})
            else:
                try:
                    fills_checksum, positions_checksum, account_checksum = self._actual_checksums(
                        fills=fills, positions=positions, account=account,
                    )
                except Exception as exc:  # noqa: BLE001 - malformed projections must fail closed
                    breaks.append(f"durable reconciliation rows are malformed: {type(exc).__name__}")
                    fills_checksum = _checksum({"fills": "INVALID", "tag": RECONCILIATION_TAG})
                    positions_checksum = _checksum({"positions": "INVALID", "tag": RECONCILIATION_TAG})
                    account_checksum = _checksum({"account": "INVALID", "tag": RECONCILIATION_TAG})
            reconciliation = self._store.record_paper_reconciliation(
                run_id=run.run_id,
                status="PASS" if not breaks else "FAIL",
                fills_checksum=fills_checksum,
                positions_checksum=positions_checksum,
                account_checksum=account_checksum,
                open_order_count=open_orders,
                breaks_json=_canonical_json(breaks),
            )
            if not breaks:
                run = self._store.transition_paper_run(
                    run_id=run.run_id,
                    expected_status="RECOVERY_REQUIRED",
                    expected_version=run.version,
                    new_status="READY_FOR_ARM",
                    reason="durable paper reconciliation passed; explicit activation required",
                )
            else:
                run = self._get_run(run.run_id)
            return PaperCanaryRecovery(
                not breaks, run, reconciliation, cancelled, tuple(breaks),
            )

    def stop(self, *, run_id: str, reason: str = "explicit paper canary stop") -> PaperCanaryRecovery:
        """Stop a run through cancellation and reconciliation, never by bypass transition.

        RUNNING/RECOVERY_REQUIRED first pass through :meth:`recover`; READY_FOR_ARM must carry a
        currently valid persisted PASS.  Process ownership is deliberately not required because
        stopping is risk-reducing after a restart.  Any break remains nonterminal and fail-closed.
        """
        if type(reason) is not str or not reason:
            raise PaperCanaryConfigurationError("stop reason must be a non-empty string")
        with self._lock:
            run = self._get_run(run_id)
            self._config(run)
            self._owned_versions.pop(run.run_id, None)
            if run.status in {"RUNNING", "RECOVERY_REQUIRED"}:
                recovery = self.recover(
                    run_id=run.run_id,
                    reason=f"stop recovery: {reason}",
                )
            elif run.status == "READY_FOR_ARM":
                recovery = self.prove_reconciled_ready(run_id=run.run_id)
            else:
                raise PaperCanaryStateError(
                    "stop requires RUNNING, RECOVERY_REQUIRED, or reconciled READY_FOR_ARM"
                )
            if not recovery.ok:
                return recovery
            stopped = self._store.transition_paper_run(
                run_id=recovery.run.run_id,
                expected_status="READY_FOR_ARM",
                expected_version=recovery.run.version,
                new_status="STOPPED",
                reason=reason,
            )
            return PaperCanaryRecovery(
                True,
                stopped,
                recovery.reconciliation,
                recovery.cancelled_orders,
                recovery.breaks,
            )


def verify_paper_reconciled_ready(store, *, run_id: str) -> PaperCanaryRecovery:
    """Pure read-only verification of a persisted PASS against the current Paper ledger."""
    canonical = _identifier(run_id, "run_id")
    run = store.get_paper_run(canonical)
    if run is None:
        raise PaperCanaryStateError("Paper Canary run does not exist")
    if type(run.run_id) is not str or run.run_id != canonical:
        raise PaperCanarySafetyError("Store returned the wrong Paper Canary run")
    if type(run.version) is not int or run.version < 0 or type(run.status) is not str:
        raise PaperCanarySafetyError("Paper Canary run state is malformed")
    config = DurablePaperCanary._config(run)
    if run.status != "READY_FOR_ARM" or run.active_slot != 1:
        raise PaperCanaryStateError("persisted recovery proof requires active READY_FOR_ARM")

    orders = tuple(store.list_paper_orders(run_id=run.run_id))
    fills = tuple(store.list_paper_fills(run_id=run.run_id))
    positions = tuple(store.list_paper_positions(run_id=run.run_id))
    account = store.get_paper_account(run.run_id)
    _, breaks = DurablePaperCanary._replay(
        run=run,
        config=config,
        orders=orders,
        fills=fills,
        positions=positions,
        account=account,
    )
    open_orders = 0
    for order in orders:
        try:
            if order.state in _NONTERMINAL_ORDER_STATES:
                open_orders += 1
        except Exception as exc:  # noqa: BLE001 - malformed order is conservatively open
            open_orders += 1
            breaks.append(f"paper order state is malformed: {type(exc).__name__}")
    if open_orders:
        breaks.append("nonterminal paper orders invalidate persisted recovery proof")

    latest = None
    reconciliations = tuple(store.list_paper_reconciliations(
        run_id=run.run_id,
        limit=10000,
    ))
    if reconciliations:
        latest = reconciliations[-1]
    if account is None:
        breaks.append("paper account is missing from persisted recovery proof")
        current_checksums = None
    else:
        try:
            current_checksums = DurablePaperCanary._actual_checksums(
                fills=fills,
                positions=positions,
                account=account,
            )
        except Exception as exc:  # noqa: BLE001 - malformed proof rows fail closed
            breaks.append(f"persisted recovery rows are malformed: {type(exc).__name__}")
            current_checksums = None

    if latest is None:
        breaks.append("persisted PASS reconciliation is missing")
    else:
        try:
            checked = _parse_utc(latest.checked_at, "reconciliation.checked_at")
            heartbeat = _parse_utc(run.heartbeat_at, "run.heartbeat_at")
            proof_fields_match = (
                latest.run_id == run.run_id
                and latest.status == "PASS"
                and type(latest.open_order_count) is int
                and latest.open_order_count == 0
                and latest.breaks_json == "[]"
                and current_checksums is not None
                and (
                    latest.fills_checksum,
                    latest.positions_checksum,
                    latest.account_checksum,
                ) == current_checksums
                and latest.checked_at == _iso_utc(checked)
                and run.heartbeat_at == _iso_utc(heartbeat)
                and checked >= heartbeat
                and run.reason
                == "durable paper reconciliation passed; explicit activation required"
            )
            if not proof_fields_match:
                breaks.append("persisted PASS does not bind the current READY ledger cycle")
        except Exception as exc:  # noqa: BLE001 - malformed proof metadata fails closed
            breaks.append(f"persisted PASS metadata is malformed: {type(exc).__name__}")
    return PaperCanaryRecovery(not breaks, run, latest, (), tuple(breaks))
