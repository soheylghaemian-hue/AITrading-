"""Time-scheduled execution — TWAP / VWAP (§16).

`SlicingAlgo` splits an order *within one step*; this scheduler works a parent order *across
time*, releasing one child slice per tick (bar). TWAP releases equal slices; VWAP weights them
by a volume profile so more is executed when the market is liquid. Each released child still
passes the Risk Engine, and the scheduler exposes the in-flight quantity so the desk counts a
working order against its target and doesn't double-submit.

If a released slice is vetoed by risk (or doesn't fill), the working order is aborted — the
Risk Engine said no, so we stop working it rather than retry blindly.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..brokers.base import Account, Order
from ..core.enums import OrderType, Side
from ..core.events import Instrument
from ..logging_config import get_logger
from .engine import ExecutionEngine, ExecutionResult

log = get_logger("execution.scheduler")


def split_quantity(quantity: float, weights: list[float], *, whole_units: bool = True) -> list[float]:
    """Split `quantity` across `weights` (TWAP => equal weights). Children sum to `quantity`."""
    total = sum(weights)
    if total <= 0:
        return [quantity]
    raw = [quantity * w / total for w in weights]
    if whole_units:
        children = [float(math.floor(x)) for x in raw]
        children[-1] += quantity - sum(children)   # remainder into the last slice
    else:
        children = raw
    return [c for c in children if c > 1e-9]


@dataclass(slots=True)
class WorkingOrder:
    instrument: Instrument
    side: Side
    slices: deque[float]
    price_ref: float
    stop_distance: float = 0.0
    adv: float | None = None
    context: Any = None                # opaque (e.g. TradeContext) for journaling on fill
    released: int = 0
    filled_qty: float = 0.0

    @property
    def working_qty(self) -> float:
        """Signed quantity still to be worked (long > 0, short < 0)."""
        return self.side.sign * sum(self.slices)


class ExecutionScheduler:
    def __init__(self, execution: ExecutionEngine, *, slices: int = 4,
                 volume_profile: list[float] | None = None) -> None:
        self._execution = execution
        self._slices = slices
        self._profile = volume_profile          # VWAP weights; None => TWAP (uniform)
        self._working: dict[str, WorkingOrder] = {}

    def working_qty(self, key: str) -> float:
        w = self._working.get(key)
        return w.working_qty if w else 0.0

    def has_work(self) -> bool:
        return bool(self._working)

    def submit_parent(
        self,
        order: Order,
        *,
        price: float,
        stop_distance: float = 0.0,
        adv: float | None = None,
        context: Any = None,
    ) -> bool:
        """Register a parent order to be worked over time. Replaces any existing work on the
        instrument. Returns True if at least one slice was scheduled."""
        weights = self._profile if self._profile else [1.0] * self._slices
        children = split_quantity(order.quantity, weights)
        if not children:
            return False
        self._working[order.instrument.key] = WorkingOrder(
            instrument=order.instrument, side=order.side, slices=deque(children),
            price_ref=price, stop_distance=stop_distance, adv=adv, context=context,
        )
        return True

    def cancel(self, key: str) -> None:
        self._working.pop(key, None)

    async def tick(
        self,
        account: Account,
        *,
        price_fn: Callable[[str], float | None],
        now: datetime | None = None,
    ) -> list[tuple[ExecutionResult, Any]]:
        """Release the next due slice of each working order. Returns (result, context) pairs.

        The caller refreshes `account` between ticks; here we read the current position per
        instrument from the passed account for the risk check."""
        out: list[tuple[ExecutionResult, Any]] = []
        for key in list(self._working):
            w = self._working[key]
            if not w.slices:
                del self._working[key]
                continue
            child_qty = w.slices[0]
            price = price_fn(key) or w.price_ref
            current = account.positions[key].quantity if key in account.positions else 0.0
            child = Order(w.instrument, w.side, child_qty, OrderType.MARKET)
            result = await self._execution.submit(
                child, account, price=price, current_qty=current,
                stop_distance=w.stop_distance, adv=w.adv, urgency="normal",
            )
            out.append((result, w.context))
            if result.filled:
                w.slices.popleft()
                w.released += 1
                w.filled_qty += child_qty
                if not w.slices:
                    del self._working[key]
            else:
                log.info("aborting working order %s — slice not filled (%s)", key, result.reason)
                del self._working[key]
        return out
