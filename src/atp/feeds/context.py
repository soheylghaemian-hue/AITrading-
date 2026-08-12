"""Concrete context feeds (§5): rates, events and options.

Offline/deterministic implementations that replay scheduled or synthesized data into the shared
engines. Each is the reference for a production adapter: swap the "where the data comes from"
while keeping the same `ContextFeed.refresh` contract.

* `ScheduledRatesFeed`   — applies dated policy-rate changes to a `RatesTable` as time passes.
* `ScheduledEventsFeed`  — schedules calendar events, then reveals each event's `actual`
                           (the surprise) once its timestamp passes.
* `OptionsChainFeed`     — rebuilds an option chain from the latest underlying spot and pushes
                           it into an `OptionsEngine` (spot supplied by a callback).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..macro.calendar import EconomicCalendar, Event
from ..macro.rates import RatesTable
from ..options.chain import build_chain
from ..options.engine import OptionsEngine
from .base import ContextFeed


class ScheduledRatesFeed(ContextFeed):
    def __init__(self, rates: RatesTable, schedule: list[tuple[datetime, str, float]]) -> None:
        # schedule: (effective_ts, currency, rate), applied when now >= effective_ts.
        self._rates = rates
        self._schedule = sorted(schedule, key=lambda x: x[0])
        self._applied = 0

    @property
    def name(self) -> str:
        return "rates"

    async def refresh(self, now: datetime) -> int:
        applied = 0
        while self._applied < len(self._schedule) and self._schedule[self._applied][0] <= now:
            _, ccy, rate = self._schedule[self._applied]
            self._rates.set_rate(ccy, rate, now)
            self._applied += 1
            applied += 1
        return applied


class ScheduledEventsFeed(ContextFeed):
    def __init__(self, calendar: EconomicCalendar, events: list[Event], *, horizon_hours: float = 72) -> None:
        # Events carry their (possibly not-yet-known) `actual`; the feed schedules them ahead of
        # time (actual hidden) and reveals the actual once `now` reaches the event timestamp.
        self._cal = calendar
        self._events = list(events)
        self._horizon = horizon_hours * 3600
        self._scheduled: set[int] = set()
        self._revealed: set[int] = set()

    @property
    def name(self) -> str:
        return "events"

    async def refresh(self, now: datetime) -> int:
        updates = 0
        for i, ev in enumerate(self._events):
            secs_ahead = (ev.ts - now).total_seconds()
            if i not in self._scheduled and 0 <= secs_ahead <= self._horizon:
                # Schedule it with the actual hidden (for blackout awareness).
                self._cal.add(Event(ev.ts, ev.instrument_key, ev.kind, ev.importance,
                                    expected=ev.expected, actual=None))
                self._scheduled.add(i)
                updates += 1
            if i not in self._revealed and ev.ts <= now and ev.actual is not None:
                self._cal.add(ev)   # upsert with the released actual (surprise now known)
                self._revealed.add(i)
                updates += 1
        return updates


class OptionsChainFeed(ContextFeed):
    def __init__(
        self,
        engine: OptionsEngine,
        underlying_key: str,
        spot_fn: Callable[[], float | None],
        *,
        T: float = 0.08,
        base_iv: float = 0.20,
        skew: float = 0.4,
        iv_fn: Callable[[], float] | None = None,
    ) -> None:
        self._engine = engine
        self._key = underlying_key
        self._spot_fn = spot_fn
        self._T = T
        self._base_iv = base_iv
        self._skew = skew
        self._iv_fn = iv_fn   # optional: derive base IV dynamically (e.g. from realized vol)

    @property
    def name(self) -> str:
        return "options"

    async def refresh(self, now: datetime) -> int:
        spot = self._spot_fn()
        if not spot or spot <= 0:
            return 0
        base_iv = self._iv_fn() if self._iv_fn is not None else self._base_iv
        self._engine.update(build_chain(self._key, spot, self._T, base_iv=base_iv, skew=self._skew))
        return 1
