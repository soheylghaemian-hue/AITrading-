"""Futures rollover — data model + processor (§3).

A `FuturesRoll` says: on `roll_date`, close the expiring contract and open the equivalent
position in the next one. Roll dates and the next contract are **caller-supplied** (from the
exchange calendar / data vendor) — no roll schedule is invented. The processor closes the old
and opens the new at market (needs quotes for both contracts, like any fill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..brokers.base import Broker, Order
from ..core.enums import OrderType, Side
from ..core.events import Instrument
from ..logging_config import get_logger

log = get_logger("corpactions.roll")


@dataclass(slots=True, frozen=True)
class FuturesRoll:
    underlying: str
    from_key: str                # instrument key of the expiring contract
    to_instrument: Instrument    # the next contract to roll into
    roll_date: str               # "YYYY-MM-DD"


@dataclass(slots=True)
class RollCalendar:
    rolls: list[FuturesRoll] = field(default_factory=list)

    def add(self, roll: FuturesRoll) -> None:
        self.rolls.append(roll)

    def due(self, day: str) -> list[FuturesRoll]:
        return [r for r in self.rolls if r.roll_date == day]


class FuturesRollProcessor:
    def __init__(self, calendar: RollCalendar) -> None:
        self._cal = calendar

    async def process(self, broker: Broker, on_date: date) -> list[dict]:
        """Roll any positions whose contract is due on `on_date`: flatten old, open new."""
        day = on_date.isoformat()
        positions = await broker.get_positions()
        applied: list[dict] = []
        for roll in self._cal.due(day):
            pos = positions.get(roll.from_key)
            if pos is None or pos.quantity == 0:
                continue
            qty = pos.quantity
            close_side = Side.SELL if qty > 0 else Side.BUY
            open_side = Side.BUY if qty > 0 else Side.SELL
            await broker.place_order(Order(pos.instrument, close_side, abs(qty), OrderType.MARKET, reduce_only=True))
            await broker.place_order(Order(roll.to_instrument, open_side, abs(qty), OrderType.MARKET))
            applied.append({"from": roll.from_key, "to": roll.to_instrument.key, "quantity": qty})
            log.info("rolled %s -> %s qty=%.4g", roll.from_key, roll.to_instrument.key, qty)
        return applied
