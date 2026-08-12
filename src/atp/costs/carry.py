"""Financing and borrow-cost models (§20).

Overnight carrying costs on leveraged/short positions. Rates are either a flat configured value
or looked up from the injected `RatesTable` — never invented. Cost = rate × notional × days/365.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..macro.rates import RatesTable


class FinancingModel(abc.ABC):
    @abc.abstractmethod
    def financing_cost(self, *, notional: float, days: float, currency: str = "USD") -> float:
        """Cost (currency) of financing `notional` for `days` (positive = you pay)."""


@dataclass(slots=True)
class FlatFinancing(FinancingModel):
    annual_rate: float = 0.05

    def financing_cost(self, *, notional: float, days: float, currency: str = "USD") -> float:
        return notional * self.annual_rate * days / 365.0


@dataclass(slots=True)
class RateTableFinancing(FinancingModel):
    """Financing at the currency's policy rate (from `RatesTable`) plus a broker spread."""

    rates: RatesTable
    spread: float = 0.015

    def financing_cost(self, *, notional: float, days: float, currency: str = "USD") -> float:
        base = self.rates.rate(currency)
        rate = (base if base is not None else 0.0) + self.spread
        return notional * rate * days / 365.0


class BorrowModel(abc.ABC):
    @abc.abstractmethod
    def borrow_cost(self, *, short_notional: float, days: float, instrument_key: str | None = None) -> float:
        """Cost of borrowing to hold a short of `short_notional` for `days`."""


@dataclass(slots=True)
class FlatBorrow(BorrowModel):
    annual_rate: float = 0.005     # 50 bps general collateral

    def borrow_cost(self, *, short_notional: float, days: float, instrument_key: str | None = None) -> float:
        return abs(short_notional) * self.annual_rate * days / 365.0


@dataclass(slots=True)
class PerInstrumentBorrow(BorrowModel):
    """Hard-to-borrow names carry higher rates — looked up per instrument, else a default."""

    rates: dict[str, float] = field(default_factory=dict)
    default_rate: float = 0.005

    def borrow_cost(self, *, short_notional: float, days: float, instrument_key: str | None = None) -> float:
        rate = self.rates.get(instrument_key or "", self.default_rate)
        return abs(short_notional) * rate * days / 365.0
