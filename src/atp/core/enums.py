"""Domain enumerations (§2/§7/§14/§15).

Small, closed vocabularies shared across the whole pipeline. Keeping them here (rather than
as bare strings) makes the market-brain's state machine explicit and greppable.
"""

from __future__ import annotations

from enum import Enum


class AssetClass(str, Enum):
    """The tradable universe (§2). `str` mixin so values serialize cleanly to JSON/logs."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    COMMODITY = "commodity"
    FX = "fx"
    BOND = "bond"
    FUTURE = "future"
    OPTION = "option"
    CRYPTO = "crypto"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL — signed-quantity math in the broker/desk."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    """Supported order types (§16)."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class Action(str, Enum):
    """Decisions the autonomous policy may take (§15)."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"
    REDUCE = "reduce"
    HEDGE = "hedge"


class Regime(str, Enum):
    """Market regimes (§7). Strategies are activated/reduced/disabled per regime (§7)."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE = "range"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    PANIC = "panic"
    LIQUIDITY_STRESS = "liquidity_stress"
    EVENT_DRIVEN = "event_driven"
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    UNKNOWN = "unknown"


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
