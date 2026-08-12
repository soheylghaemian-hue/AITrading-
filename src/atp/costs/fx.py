"""FX conversion — cost model + rate seam (§20).

Multi-currency P&L must be converted to the base currency. The *conversion cost* (a bps fee) is
data-independent and modeled here. The *rate* is market data and is injected via `FXRateSource`;
if a rate is unavailable, `convert` returns `None` (DATA_NOT_AVAILABLE) — **never an invented
rate**. `TableFXRates` is a caller-populated table (identity for same-currency, inverse when the
reverse pair is known).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


class FXRateSource(abc.ABC):
    @abc.abstractmethod
    def rate(self, base: str, quote: str) -> float | None:
        """Units of `quote` per 1 unit of `base`, or None if unknown."""


@dataclass(slots=True)
class TableFXRates(FXRateSource):
    rates: dict[tuple[str, str], float] = field(default_factory=dict)

    def rate(self, base: str, quote: str) -> float | None:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return 1.0
        if (base, quote) in self.rates:
            return self.rates[(base, quote)]
        inv = self.rates.get((quote, base))
        return (1.0 / inv) if inv else None


class FXConverter:
    def __init__(self, source: FXRateSource, *, conversion_cost_bps: float = 2.0) -> None:
        self._source = source
        self._cost_bps = conversion_cost_bps

    def convert(self, amount: float, base: str, quote: str) -> float | None:
        """Convert `amount` from `base` to `quote`. None if the rate is unavailable."""
        r = self._source.rate(base, quote)
        return amount * r if r is not None else None

    def conversion_cost(self, amount: float, base: str, quote: str) -> float:
        """The broker's conversion fee (currency of `amount`). Zero for same-currency."""
        if base.upper() == quote.upper():
            return 0.0
        return abs(amount) * self._cost_bps / 1e4
