"""Economic / events calendar (§5 Events, §8 Event).

Holds scheduled events per instrument — earnings, CPI, central-bank decisions — with an
importance and, once released, an expected/actual pair (the *surprise*). The event specialist
uses it to (a) flatten into high-impact events and (b) trade the surprise afterwards. Fed by an
events-data feed in production; populated directly in tests/demos. Pure stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_IMPORTANCE = {"low": 1, "medium": 2, "high": 3}


@dataclass(slots=True)
class Event:
    ts: datetime
    instrument_key: str      # which instrument this event drives
    kind: str                # "earnings", "cpi", "fomc", ...
    importance: str = "high"
    expected: float | None = None
    actual: float | None = None

    @property
    def surprise(self) -> float | None:
        """Signed, normalized surprise once released: (actual − expected)/|expected|."""
        if self.expected is None or self.actual is None:
            return None
        if self.expected == 0:
            return self.actual - self.expected
        return (self.actual - self.expected) / abs(self.expected)

    def at_least(self, importance: str) -> bool:
        return _IMPORTANCE.get(self.importance, 0) >= _IMPORTANCE.get(importance, 0)


class EconomicCalendar:
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}

    def add(self, event: Event) -> None:
        """Add or update an event. Upserts by (ts, kind) so a feed can first schedule an event
        (actual unknown) and later reveal its released value without duplicating it."""
        lst = self._events.setdefault(event.instrument_key, [])
        for i, e in enumerate(lst):
            if e.ts == event.ts and e.kind == event.kind:
                lst[i] = event
                return
        lst.append(event)
        lst.sort(key=lambda e: e.ts)

    def for_instrument(self, key: str) -> list[Event]:
        return list(self._events.get(key, []))

    def in_blackout(self, key: str, now: datetime, ahead: timedelta,
                    importance: str = "high") -> Event | None:
        """A high-impact event scheduled within `ahead` of `now` (get flat before it)."""
        for e in self._events.get(key, []):
            if e.at_least(importance) and now < e.ts <= now + ahead:
                return e
        return None

    def recent_surprise(self, key: str, now: datetime, within: timedelta,
                        importance: str = "high", min_surprise: float = 0.0) -> Event | None:
        """The most recent released, high-impact event within `within` whose surprise clears
        `min_surprise` — the signal to trade after the event."""
        best: Event | None = None
        for e in self._events.get(key, []):
            s = e.surprise
            if (e.at_least(importance) and s is not None and abs(s) >= min_surprise
                    and now - within <= e.ts <= now):
                if best is None or e.ts > best.ts:
                    best = e
        return best
