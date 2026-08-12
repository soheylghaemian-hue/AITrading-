"""Commission models (§20).

Pluggable commission schedules so the paper broker (and later the live cost accounting) can
model real broker pricing per asset class. `PerShareCommission` reproduces the previous fixed
behavior, so wiring one in changes nothing unless you pick a different schedule.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class CommissionModel(abc.ABC):
    @abc.abstractmethod
    def commission(self, *, quantity: float, price: float, multiplier: float = 1.0) -> float:
        """Commission (currency) for filling `quantity` units at `price`."""


@dataclass(slots=True)
class PerShareCommission(CommissionModel):
    """Per-share with a minimum — the default US-equity style (matches the prior broker)."""

    per_unit: float = 0.005
    minimum: float = 1.0

    def commission(self, *, quantity: float, price: float, multiplier: float = 1.0) -> float:
        return max(self.minimum, self.per_unit * quantity)


@dataclass(slots=True)
class PerContractCommission(CommissionModel):
    """Per-contract — futures/options style."""

    per_contract: float = 0.85
    minimum: float = 0.0

    def commission(self, *, quantity: float, price: float, multiplier: float = 1.0) -> float:
        return max(self.minimum, self.per_contract * quantity)


@dataclass(slots=True)
class PercentCommission(CommissionModel):
    """A fraction of traded notional — some venues / structured products."""

    rate: float = 0.0005          # 5 bps
    minimum: float = 0.0

    def commission(self, *, quantity: float, price: float, multiplier: float = 1.0) -> float:
        return max(self.minimum, self.rate * quantity * price * multiplier)
