"""Execution algorithms (§16).

An `ExecutionAlgo` turns one parent order into a *plan* of child orders, given liquidity and
urgency context. The engine risk-checks the parent once, then works the children.

* `ImmediateAlgo` — the baseline: one order, as before (default, so nothing changes unless a
  smarter algo is chosen).
* `SlicingAlgo` — participation-aware: if the order is large relative to average volume, split
  it into equal child slices so each pays less market impact (impact is convex in size, see
  `impact.py`). Urgent orders (risk-reducing / close) skip slicing and execute immediately —
  cutting risk fast beats saving a few bps.

Smart *timing* (spreading children across time, VWAP/TWAP schedules) is a further extension;
here the children are worked within the step, which is what lets the convex-impact benefit be
modeled deterministically offline.
"""

from __future__ import annotations

import abc
import math
from dataclasses import replace

from ..brokers.base import Order


class ExecutionAlgo(abc.ABC):
    @abc.abstractmethod
    def plan(self, order: Order, *, adv: float | None = None, urgency: str = "normal") -> list[Order]:
        ...


class ImmediateAlgo(ExecutionAlgo):
    def plan(self, order: Order, *, adv: float | None = None, urgency: str = "normal") -> list[Order]:
        return [order]


class SlicingAlgo(ExecutionAlgo):
    def __init__(self, *, participation_cap: float = 0.1, max_slices: int = 10) -> None:
        # Keep each child at or below this fraction of average volume.
        self._cap = participation_cap
        self._max_slices = max_slices

    def _n_slices(self, quantity: float, adv: float | None) -> int:
        if adv is None or adv <= 0 or self._cap <= 0:
            return 1
        participation = quantity / adv
        if participation <= self._cap:
            return 1
        return min(self._max_slices, math.ceil(participation / self._cap))

    def plan(self, order: Order, *, adv: float | None = None, urgency: str = "normal") -> list[Order]:
        # Urgent (close / risk-reducing): execute now, don't work the order.
        if urgency == "high" or order.reduce_only:
            return [order]

        n = self._n_slices(order.quantity, adv)
        if n <= 1:
            return [order]

        base = order.quantity / n
        children: list[Order] = []
        remaining = order.quantity
        for i in range(n):
            qty = remaining if i == n - 1 else base
            remaining -= qty
            children.append(replace(order, quantity=qty))
        return children
