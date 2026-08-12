"""Breakout specialist (§8, §7 Breakout regime).

Trades range breakouts *confirmed by volume*: it goes long when price has stretched well above
its slow average (a high z-score) on above-average volume, short on the symmetric downside.
The volume filter is what separates a real breakout from noise — a move on thin volume is
faded by others, not chased here. Active only where a breakout makes sense (trend/breakout/
high-vol regimes, §7).
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from .base import Signal, Strategy


class BreakoutStrategy(Strategy):
    active_regimes = frozenset(
        {Regime.BREAKOUT, Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY}
    )

    def __init__(self, *, entry_z: float = 1.5, volume_mult: float = 1.3, allow_short: bool = True) -> None:
        self._entry_z = entry_z
        self._volume_mult = volume_mult
        self._allow_short = allow_short
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "breakout"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or not self.is_active(regime) or fs.close_std <= 0:
            return None

        z = (fs.price - fs.sma_slow) / fs.close_std
        breakout = abs(z) >= self._entry_z and fs.rel_volume >= self._volume_mult
        sign = (1 if z > 0 else -1) if breakout else 0

        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign
        if sign == 0 or sign == prev:
            return None

        confidence = min(1.0, abs(z) / (2 * self._entry_z))
        expected_return = abs(z) * max(fs.realized_vol, 0.001)
        if sign > 0:
            action = Action.BUY
        elif self._allow_short:
            action = Action.SELL
        else:
            action = Action.CLOSE
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=confidence,
            expected_return=expected_return if action is not Action.CLOSE else 0.0,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"breakout z={z:.2f} relvol={fs.rel_volume:.2f}",
        )
