"""Cross-Asset specialist (§8, using §6 intelligence).

Trades cross-asset *divergence*: when a follower has decoupled from what its leader implies
(via `CrossAssetEngine`), it bets on convergence — buy the follower that has lagged, sell the
one that has run ahead — provided the pair's realized correlation still confirms the
relationship (otherwise the divergence is just a broken link, not an opportunity).

Unlike single-instrument specialists, this one reads shared cross-asset state: it holds a
reference to the same `CrossAssetEngine` the desk updates each bar, and keys off the follower
instrument passed in `generate`.
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..cross_asset.engine import CrossAssetEngine
from ..features.engine import FeatureSet
from .base import Signal, Strategy


class CrossAssetStrategy(Strategy):
    #: Stand aside in panic — cross-asset relationships break down under liquidity stress (§7).
    active_regimes = frozenset()

    def __init__(self, engine: CrossAssetEngine, *, entry_z: float = 1.5, corr_min: float = 0.3) -> None:
        self._engine = engine
        self._entry_z = entry_z
        self._corr_min = corr_min
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "cross_asset"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or regime is Regime.PANIC:
            return None
        view = self._engine.assessment(fs.instrument)
        if view is None or not view.ready:
            return None
        # Only act when the relationship is intact (still correlated as expected).
        if view.correlation < self._corr_min:
            return None

        z = view.divergence_z
        # Follower lagged (z<0) => buy it to converge up; ran ahead (z>0) => sell.
        sign = 1 if z <= -self._entry_z else (-1 if z >= self._entry_z else 0)
        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign

        if sign == 0 or sign == prev:
            return None

        confidence = min(1.0, abs(z) / (2 * self._entry_z))
        expected_return = abs(z) * max(fs.realized_vol, 0.001)
        action = Action.BUY if sign > 0 else Action.SELL
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=confidence,
            expected_return=expected_return,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"{view.follower}~{view.leader} div_z={z:.2f} corr={view.correlation:.2f}",
        )
