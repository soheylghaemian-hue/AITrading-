"""Specialized traders — base contract (§8, §24 Phase 9).

Each specialist (Momentum, Mean-Reversion, Macro, Options, Commodities, FX, StatArb, Event,
Cross-Asset) reads the current features + regime and, when it has a view, emits a `Signal`
carrying not just a direction but the numbers the Portfolio/Opportunity/Risk layers need:
expected return, confidence, and a stop distance for risk sizing (§8/§10).

A specialist expresses a *directional view*; it does not know the current position. Turning
a view into an order (open / add / reduce / close / flip) is the desk's job (§9), which keeps
sizing and risk in one place.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime

from ..core.enums import Action, Regime
from ..core.events import Instrument
from ..features.engine import FeatureSet


@dataclass(slots=True)
class Signal:
    instrument: Instrument
    action: Action                 # BUY / SELL / HOLD / CLOSE / REDUCE / HEDGE
    confidence: float              # [0, 1]
    expected_return: float         # expected move as a fraction of price
    stop_distance: float           # price-unit risk per unit, for sizing (§10)
    strategy: str
    regime: Regime
    ts: datetime
    rationale: str = ""
    #: Sizing mode: "risk" (fraction-of-equity risk per trade) or "hedged" (β-weighted share
    #: sizing for market-neutral pairs — both legs derive units from a common reference price
    #: and hedge factor, so qty scales with β, §8).
    sizing: str = "risk"
    hedge_factor: float = 1.0        # units = base_units × hedge_factor (β for the second leg)
    ref_price: float | None = None   # common reference price both legs size from

    @property
    def is_directional(self) -> bool:
        return self.action in (Action.BUY, Action.SELL)

    @property
    def direction(self) -> int:
        if self.action is Action.BUY:
            return 1
        if self.action is Action.SELL:
            return -1
        return 0


class Strategy(abc.ABC):
    """Base class for a specialized trader (§8)."""

    #: Regimes in which this strategy is allowed to act (§7). Empty => all regimes.
    active_regimes: frozenset[Regime] = frozenset()

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    def is_active(self, regime: Regime) -> bool:
        return not self.active_regimes or regime in self.active_regimes

    def reset(self) -> None:
        """Clear any per-instrument state (e.g. crossover memory).

        Called at the start of every backtest run so a strategy instance can be safely reused
        across independent runs — in-sample, out-of-sample and each walk-forward window —
        without state bleeding between them (§13 no look-ahead / no leakage). Stateless
        strategies need not override this."""

    @abc.abstractmethod
    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        """Return a Signal, or None if the strategy has no view right now."""
