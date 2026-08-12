"""Market calendar — trading days, holidays and session hours (§3/§5).

The backtester and live desk need to know when a venue is actually open. This is a small,
explicit calendar: weekly session hours plus a holiday set. Real exchange calendars (half-days,
per-venue holidays) are loaded from data later; the interface here is what the rest of the
system depends on, so swapping in a full calendar is a data change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time


@dataclass(slots=True)
class MarketCalendar:
    name: str = "24x5"
    trading_days: tuple[int, ...] = (0, 1, 2, 3, 4)   # Mon–Fri (weekday numbers)
    session_open: time = time(0, 0)
    session_close: time = time(23, 59)
    holidays: frozenset[str] = field(default_factory=frozenset)  # {"YYYY-MM-DD"}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() in self.trading_days and d.isoformat() not in self.holidays

    def is_open(self, dt: datetime) -> bool:
        if not self.is_trading_day(dt.date()):
            return False
        t = dt.timetz().replace(tzinfo=None)
        return self.session_open <= t <= self.session_close


# A couple of ready-made calendars (extended from real exchange data later).
US_EQUITY = MarketCalendar(
    name="us_equity", trading_days=(0, 1, 2, 3, 4),
    session_open=time(14, 30), session_close=time(21, 0),   # 09:30–16:00 ET in UTC (approx)
)
CONTINUOUS = MarketCalendar(name="24x7", trading_days=(0, 1, 2, 3, 4, 5, 6))
