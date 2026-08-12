"""Options expiration helper (§3/§5).

The settlement/assignment logic already lives in `atp.options.execution.settle_expiration`
(cash and physical). This is the thin discovery helper: which option positions expire on a
given date. No data is invented — expiry comes from each instrument's own `expiry` field, and
settlement needs caller-supplied spots.
"""

from __future__ import annotations

from datetime import date

from ..brokers.base import Position
from ..options.execution import settle_expiration  # re-exported: the settlement processor

__all__ = ["options_expiring_on", "settle_expiration"]


def options_expiring_on(positions: dict[str, Position], on_date: date) -> list[Position]:
    """Option positions at or past their expiry as of `on_date` (YYYYMMDD comparison)."""
    day = on_date.strftime("%Y%m%d")
    return [
        p for p in positions.values()
        if p.instrument.is_option and p.instrument.expiry and p.instrument.expiry <= day
    ]
