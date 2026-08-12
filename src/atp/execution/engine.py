"""Execution Engine (§16, §24 Phase 12).

Sits between the desk's sized order and the broker. Its job here is twofold:
1. Route every order through the Risk Engine's veto first — execution can never bypass risk
   (§14). If risk says no, the order is dropped with a reason, never sent.
2. Choose the order type from urgency/liquidity context and hand it to the broker adapter.

The smart execution algorithms (VWAP/TWAP/impact-aware slicing) named in §16 are future
extension points; today it submits a single order and reports the fill honestly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..brokers.base import Account, Broker, Fill, Order, OrderResult
from ..core.enums import OrderStatus
from ..logging_config import get_logger
from ..risk.engine import RiskDecision, RiskEngine
from .algo import ExecutionAlgo, ImmediateAlgo

log = get_logger("execution")


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
        # Risk vetoes the PARENT order once, before any child is worked (§14).
        decision = self._risk.check_order(
            order, account, price=price, current_qty=current_qty, stop_distance=stop_distance
        )
        if not decision.approved:
            log.info("BLOCKED %s %s x%.4g — %s", order.side.value, order.instrument, order.quantity, decision.reason)
            return ExecutionResult(order, decision)

        if not self._autonomous:
            # Manual/confirmation mode: risk-approved but not auto-sent (§15 opt-in autonomy).
            return ExecutionResult(order, decision)

        # Plan child orders (§16) and work them, aggregating into one result on the parent.
        children = self._algo.plan(order, adv=adv, urgency=urgency)
        results = [await self._broker.place_order(child) for child in children]
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
