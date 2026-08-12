"""Corporate actions — splits & dividends (§3).

Data models and a processor that applies them to broker positions on the ex-date. All action
data is **caller-supplied** (loaded from a data vendor later) — nothing is invented here; an
empty book applies nothing. The maths (split-adjust shares/basis, dividend cash) are pure and
tested; applying them uses the broker's `adjust_position` / `credit_cash` primitives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..brokers.base import Broker
from ..logging_config import get_logger

log = get_logger("corpactions")


@dataclass(slots=True, frozen=True)
class Split:
    instrument_key: str
    ex_date: str                 # "YYYY-MM-DD"
    numerator: int               # 2:1 forward split => numerator=2, denominator=1
    denominator: int = 1

    @property
    def ratio(self) -> float:
        return self.numerator / self.denominator


@dataclass(slots=True, frozen=True)
class Dividend:
    instrument_key: str
    ex_date: str
    amount_per_share: float
    currency: str = "USD"


@dataclass(slots=True)
class CorporateActionsBook:
    splits: dict[str, list[Split]] = field(default_factory=dict)
    dividends: dict[str, list[Dividend]] = field(default_factory=dict)

    def add_split(self, s: Split) -> None:
        self.splits.setdefault(s.instrument_key, []).append(s)

    def add_dividend(self, d: Dividend) -> None:
        self.dividends.setdefault(d.instrument_key, []).append(d)

    def splits_on(self, key: str, day: str) -> list[Split]:
        return [s for s in self.splits.get(key, []) if s.ex_date == day]

    def dividends_on(self, key: str, day: str) -> list[Dividend]:
        return [d for d in self.dividends.get(key, []) if d.ex_date == day]


def apply_split_to_position(quantity: float, avg_price: float, split: Split) -> tuple[float, float]:
    """A forward split multiplies shares by the ratio and divides the basis (value-neutral)."""
    return quantity * split.ratio, avg_price / split.ratio


def dividend_cash(quantity: float, dividend: Dividend) -> float:
    """Cash from a dividend: longs receive, shorts pay (signed by quantity)."""
    return quantity * dividend.amount_per_share


class CorporateActionsProcessor:
    def __init__(self, book: CorporateActionsBook) -> None:
        self._book = book

    async def process(self, broker: Broker, on_date: date) -> list[dict]:
        """Apply all splits/dividends with an ex-date of `on_date` to the broker's positions."""
        if not (hasattr(broker, "adjust_position") and hasattr(broker, "credit_cash")):
            raise TypeError("corporate actions require a broker with adjust_position/credit_cash")
        day = on_date.isoformat()
        applied: list[dict] = []
        positions = await broker.get_positions()
        for key, pos in positions.items():
            for s in self._book.splits_on(key, day):
                nq, na = apply_split_to_position(pos.quantity, pos.avg_price, s)
                broker.adjust_position(pos.instrument, nq, na)  # type: ignore[attr-defined]
                applied.append({"type": "split", "key": key, "ratio": s.ratio, "new_qty": nq})
                log.info("split %s %d:%d -> qty %.4g", key, s.numerator, s.denominator, nq)
            for d in self._book.dividends_on(key, day):
                cash = dividend_cash(pos.quantity, d)
                broker.credit_cash(cash)  # type: ignore[attr-defined]
                applied.append({"type": "dividend", "key": key, "cash": cash})
                log.info("dividend %s %.4f/sh -> cash %.2f", key, d.amount_per_share, cash)
        return applied
