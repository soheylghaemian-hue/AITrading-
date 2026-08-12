"""Corporate actions, futures rollover and options expiry (§3).

Data models + processors. All action/roll data is caller-supplied — nothing is fabricated."""

from .actions import (
    CorporateActionsBook,
    CorporateActionsProcessor,
    Dividend,
    Split,
    apply_split_to_position,
    dividend_cash,
)
from .expiry import options_expiring_on, settle_expiration
from .rollover import FuturesRoll, FuturesRollProcessor, RollCalendar

__all__ = [
    "Split",
    "Dividend",
    "CorporateActionsBook",
    "CorporateActionsProcessor",
    "apply_split_to_position",
    "dividend_cash",
    "FuturesRoll",
    "RollCalendar",
    "FuturesRollProcessor",
    "options_expiring_on",
    "settle_expiration",
]
