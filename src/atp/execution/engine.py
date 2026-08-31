"""Execution Engine (§16, §24 Phase 12).

Sits between the desk's sized order and the broker. Its job here is twofold:
1. Route every order through the Risk Engine's veto first — execution can never bypass risk
   (§14). If risk says no, the order is dropped with a reason, never sent.
2. Choose the order type from urgency/liquidity context and hand it to the broker adapter.

The smart execution algorithms (VWAP/TWAP/impact-aware slicing) named in §16 are future
extension points; today it submits a single order and reports the fill honestly.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass

from ..brokers.base import Account, Broker, Fill, Order, OrderResult, Position
from ..core.enums import OrderStatus
from ..core.events import Instrument
from ..logging_config import get_logger
from ..risk.engine import RiskDecision, RiskEngine
from .algo import ExecutionAlgo, ImmediateAlgo

log = get_logger("execution")


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    """Canonical collaborators authorized by an autonomous engine boundary."""

    broker: Broker
    risk: RiskEngine
    algo: ExecutionAlgo
    autonomous: bool


@dataclass(slots=True)
class _SendLease:
    authority: ExecutionAuthority
    task: asyncio.Task | None
    order: Order
    consumed: bool = False


@dataclass(slots=True)
class _SubmitLease:
    task: asyncio.Task | None
    order: Order
    consumed: bool = False


OrderGuard = Callable[[], AbstractContextManager[RiskDecision | ExecutionAuthority]]


@dataclass(slots=True)
class ExecutionResult:
    order: Order
    decision: RiskDecision
    result: OrderResult | None = None

    @property
    def approved(self) -> bool:
        return self.decision.approved

    @property
    def filled(self) -> bool:
        return self.result is not None and self.result.filled

    @property
    def reason(self) -> str:
        if not self.decision.approved:
            return self.decision.reason
        return self.result.reason if self.result else "not submitted"


class ExecutionEngine:
    def __init__(
        self,
        broker: Broker,
        risk: RiskEngine,
        *,
        autonomous: bool = True,
        algo: ExecutionAlgo | None = None,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._autonomous = autonomous
        # Default is the one-shot algo, so behavior is unchanged unless a smarter one is set.
        self._algo = algo or ImmediateAlgo()
        self._order_guard: OrderGuard | None = None
        self._authority: ExecutionAuthority | None = None
        self._submit_lease: ContextVar[_SubmitLease | None] = ContextVar(
            f"execution_submit_lease_{id(self)}", default=None
        )
        self._send_lease: ContextVar[_SendLease | None] = ContextVar(
            f"execution_send_lease_{id(self)}", default=None
        )
        self._broker_guard = self._broker_send_permit

    def bind_order_guard(
        self,
        guard: OrderGuard,
        *,
        authority: ExecutionAuthority | None = None,
    ) -> ExecutionAuthority:
        """Bind a single authoritative final-send guard.

        Paper autonomy installs this once.  Both direct submissions and scheduler slices pass
        through the same guard immediately before the risk check and broker calls.
        """
        if self._order_guard is not None and self._order_guard is not guard:
            raise ValueError("execution order guard is already bound")
        canonical = authority or ExecutionAuthority(
            self._broker, self._risk, self._algo, self._autonomous
        )
        if self._authority is not None and self._authority is not canonical:
            raise ValueError("execution authority is already bound")
        self._order_guard = guard
        self._authority = canonical
        return canonical

    @contextmanager
    def _authorize_submit(self, order: Order):
        """Issue a one-shot lease for one exact Desk/Scheduler order in the current task."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        lease = _SubmitLease(task, order)
        token = self._submit_lease.set(lease)
        try:
            yield
        finally:
            self._submit_lease.reset(token)

    @contextmanager
    def _broker_send_permit(self, order: Order):
        """Allow only a risk-approved send through the bound broker's final-send guard."""
        lease = self._send_lease.get()
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if (
            type(lease) is not _SendLease
            or lease.consumed
            or lease.authority is not self._authority
            or lease.task is None
            or lease.task is not task
            or lease.order is not order
            or self._order_guard is None
        ):
            yield RiskDecision(False, "paper broker send is not risk-authorized")
            return
        lease.consumed = True
        with self._order_guard() as boundary:
            if boundary is not lease.authority:
                if type(boundary) is RiskDecision:
                    yield boundary
                else:
                    yield RiskDecision(False, "paper execution authority changed")
                return
            yield None

    @staticmethod
    def _children_conserve_parent(parent: Order, children: object) -> bool:
        """Require a child plan to preserve every parent field except positive quantity."""
        if type(children) is not list or not children:
            return False
        total = 0.0
        for child in children:
            if type(child) is not Order:
                return False
            if (
                child.instrument != parent.instrument
                or child.side is not parent.side
                or child.order_type is not parent.order_type
                or child.limit_price != parent.limit_price
                or child.stop_price != parent.stop_price
                or child.reduce_only is not parent.reduce_only
                or child.client_id != parent.client_id
                or isinstance(child.quantity, bool)
                or not isinstance(child.quantity, (int, float))
                or not math.isfinite(child.quantity)
                or child.quantity <= 0
            ):
                return False
            total += float(child.quantity)
        return math.isclose(total, float(parent.quantity), rel_tol=1e-12, abs_tol=1e-9)

    @staticmethod
    def _canonical_paper_account(order: Order, account: object) -> tuple[Account, float] | None:
        """Validate an exact broker snapshot and derive position from it, never the caller."""
        if type(account) is not Account or type(account.positions) is not dict:
            return None
        values = (
            account.cash,
            account.equity,
            account.realized_pnl,
            account.unrealized_pnl,
            account.gross_exposure,
            account.net_exposure,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ) or account.gross_exposure < 0:
            return None

        gross = 0.0
        net = 0.0
        unrealized = 0.0
        current_qty = 0.0
        try:
            for key, position in account.positions.items():
                if type(key) is not str or type(position) is not Position:
                    return None
                instrument = position.instrument
                position_values = (
                    position.quantity,
                    position.avg_price,
                    position.market_price,
                    getattr(instrument, "multiplier", None),
                )
                if (
                    type(instrument) is not Instrument
                    or key != instrument.key
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in position_values
                    )
                    or position.avg_price < 0
                    or position.market_price < 0
                    or instrument.multiplier <= 0
                ):
                    return None
                notional = position.quantity * position.market_price * instrument.multiplier
                gross += abs(notional)
                net += notional
                unrealized += (
                    (position.market_price - position.avg_price)
                    * position.quantity
                    * instrument.multiplier
                )
                if key == order.instrument.key:
                    if instrument != order.instrument:
                        return None
                    current_qty = float(position.quantity)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

        consistent = (
            math.isclose(account.gross_exposure, gross, rel_tol=1e-12, abs_tol=1e-6)
            and math.isclose(account.net_exposure, net, rel_tol=1e-12, abs_tol=1e-6)
            and math.isclose(account.unrealized_pnl, unrealized, rel_tol=1e-12, abs_tol=1e-6)
            and math.isclose(account.equity, account.cash + net, rel_tol=1e-12, abs_tol=1e-6)
        )
        return (account, current_qty) if consistent else None

    async def submit(
        self,
        order: Order,
        account: Account,
        *,
        price: float,
        current_qty: float,
        stop_distance: float = 0.0,
        adv: float | None = None,
        urgency: str = "normal",
    ) -> ExecutionResult:
        guarded = self._order_guard is not None
        if guarded:
            lease = self._submit_lease.get()
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            if (
                type(lease) is not _SubmitLease
                or lease.consumed
                or lease.task is None
                or lease.task is not task
                or lease.order is not order
            ):
                return ExecutionResult(
                    order, RiskDecision(False, "paper execution submit is not desk-authorized")
                )
            lease.consumed = True
        guard = self._order_guard() if guarded else nullcontext(None)
        with guard as boundary:
            if type(boundary) is RiskDecision:
                return ExecutionResult(order, boundary)
            if guarded:
                if type(boundary) is not ExecutionAuthority or boundary is not self._authority:
                    return ExecutionResult(
                        order, RiskDecision(False, "paper execution authority is invalid")
                    )
                authority = boundary
            else:
                authority = ExecutionAuthority(
                    self._broker, self._risk, self._algo, self._autonomous
                )

            if guarded:
                try:
                    broker_account = await authority.broker.get_account()
                except Exception:  # broker boundary failures must reject, never trust caller state
                    return ExecutionResult(
                        order, RiskDecision(False, "canonical paper account unavailable")
                    )
                canonical = self._canonical_paper_account(order, broker_account)
                if canonical is None:
                    return ExecutionResult(
                        order, RiskDecision(False, "invalid canonical paper account")
                    )
                account, current_qty = canonical

            # Risk vetoes the conserved PARENT order before any child is worked (§14).
            decision = authority.risk.check_order(
                order, account, price=price, current_qty=current_qty, stop_distance=stop_distance
            )
            if not decision.approved:
                log.info(
                    "BLOCKED %s %s x%.4g — %s",
                    order.side.value,
                    order.instrument,
                    order.quantity,
                    decision.reason,
                )
                return ExecutionResult(order, decision)

            if not authority.autonomous:
                # Manual/confirmation mode: risk-approved but not auto-sent (§15 opt-in autonomy).
                return ExecutionResult(order, decision)

            # A buggy/custom algorithm may not change side, instrument, semantics, or total size.
            children = authority.algo.plan(order, adv=adv, urgency=urgency)
            if not self._children_conserve_parent(order, children):
                return ExecutionResult(order, RiskDecision(False, "invalid execution child plan"))
            results = []
            for child in children:
                lease = _SendLease(authority, asyncio.current_task(), child)
                token = self._send_lease.set(lease)
                try:
                    results.append(await authority.broker.place_order(child))
                finally:
                    self._send_lease.reset(token)
            return ExecutionResult(order, decision, _aggregate(order, results))


def _aggregate(parent: Order, results: list[OrderResult]) -> OrderResult:
    """Combine child fills into one parent-level result (size-weighted avg price)."""
    fills = [r.fill for r in results if r.fill is not None]
    total_qty = sum(f.quantity for f in fills)
    if total_qty <= 0:
        reason = results[-1].reason if results else "no fill"
        return OrderResult(parent, OrderStatus.REJECTED, reason=reason)

    avg_price = sum(f.price * f.quantity for f in fills) / total_qty
    commission = sum(f.commission for f in fills)
    ts = fills[-1].ts
    fill = Fill(parent.instrument, parent.side, total_qty, avg_price, commission, ts)
    status = OrderStatus.FILLED if abs(total_qty - parent.quantity) < 1e-9 else OrderStatus.PARTIALLY_FILLED
    return OrderResult(parent, status, fill=fill)
