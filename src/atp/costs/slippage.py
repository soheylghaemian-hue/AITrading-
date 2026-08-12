"""Slippage models (§16/§20).

Pluggable slippage so the fill cost isn't a single constant. `FixedBpsSlippage` reproduces the
prior behavior. All return slippage in **basis points** (adverse), applied by the broker.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class SlippageModel(abc.ABC):
    @abc.abstractmethod
    def slippage_bps(self, *, quantity: float, price: float, adv: float | None = None,
                     spread_bps: float = 0.0, volatility: float | None = None) -> float:
        ...


@dataclass(slots=True)
class FixedBpsSlippage(SlippageModel):
    bps: float = 1.0

    def slippage_bps(self, *, quantity, price, adv=None, spread_bps=0.0, volatility=None) -> float:
        return self.bps


@dataclass(slots=True)
class SpreadSlippage(SlippageModel):
    """A fraction of the quoted spread — you cross part of the book."""

    fraction: float = 0.5

    def slippage_bps(self, *, quantity, price, adv=None, spread_bps=0.0, volatility=None) -> float:
        return spread_bps * self.fraction


@dataclass(slots=True)
class VolumeSlippage(SlippageModel):
    """Grows with participation (√ of order size vs. average volume), like impact."""

    eta_bps: float = 10.0
    exponent: float = 0.5

    def slippage_bps(self, *, quantity, price, adv=None, spread_bps=0.0, volatility=None) -> float:
        if not adv or adv <= 0 or quantity <= 0:
            return 0.0
        return self.eta_bps * ((quantity / adv) ** self.exponent)
