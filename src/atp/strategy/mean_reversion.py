"""Mean-Reversion specialist (§8).

The counterpart to momentum: in a *range* regime (§7) it fades stretched moves — buying when
price sits well below its slow average and selling when well above — measured in standard-
deviation units so the threshold is scale-free across instruments.
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from .base import Signal, Strategy


class MeanReversionStrategy(Strategy):
    active_regimes = frozenset({Regime.RANGE, Regime.LOW_VOLATILITY, Regime.MEAN_REVERSION})

    def __init__(self, *, entry_z: float = 1.0) -> None:
        self._entry_z = entry_z

    @property
    def name(self) -> str:
        return "mean_reversion"

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or not self.is_active(regime) or fs.close_std <= 0:
            return None

        z = (fs.price - fs.sma_slow) / fs.close_std
        if abs(z) < self._entry_z:
            return None

        action = Action.SELL if z > 0 else Action.BUY  # fade the stretch
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=min(1.0, abs(z) / (2 * self._entry_z)),
            expected_return=abs(z) * max(fs.realized_vol, 0.001),
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"mean-reversion z={z:.2f}",
        )
