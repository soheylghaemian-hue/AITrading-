"""Specialized traders (§8)."""

from .base import Signal, Strategy
from .breakout import BreakoutStrategy
from .cross_asset import CrossAssetStrategy
from .event import EventStrategy
from .fx_carry import FXCarryStrategy
from .macro import MacroStrategy
from .mean_reversion import MeanReversionStrategy
from .momentum import MomentumStrategy
from .stat_arb import StatArbStrategy
from .volatility import VolatilityStrategy

__all__ = [
    "Signal",
    "Strategy",
    "MomentumStrategy",
    "MeanReversionStrategy",
    "CrossAssetStrategy",
    "BreakoutStrategy",
    "StatArbStrategy",
    "VolatilityStrategy",
    "FXCarryStrategy",
    "MacroStrategy",
    "EventStrategy",
]
