"""Context data feeds (§5/§17).

The shared context engines — `OptionsEngine` (IV/skew), `RatesTable` (carry), `EconomicCalendar`
(events) — need to be kept current from external data. A `ContextFeed` is the seam: each feed
knows how to pull its slice of data and push it into its engine on `refresh(now)`. Offline,
concrete feeds replay scheduled/synthetic data (used by tests and the paper loop); in production
the same interfaces wrap real providers (an options-data vendor, a rates API, an events
calendar) — the desk and strategies never change.

Kept intentionally minimal: `refresh` is idempotent-friendly (safe to call repeatedly) and
returns how many updates it applied, for telemetry.
"""

from __future__ import annotations

import abc
from datetime import datetime


class ContextFeed(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    async def refresh(self, now: datetime) -> int:
        """Pull any data due as of `now` and update the target engine. Returns update count."""
