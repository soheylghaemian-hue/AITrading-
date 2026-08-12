"""Momentum specialist (§8).

Trades trend continuation: it goes long on a bullish fast/slow crossover and short (or flat)
on a bearish one, but only while the regime engine reports a trend (§7) — in a range or a
panic it stands aside. The crossover *event* (a sign change of `sma_fast - sma_slow`) is the
trigger, so it fires once per turn rather than every bar it stays above/below.
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from .base import Signal, Strategy


class MomentumStrategy(Strategy):
    active_regimes = frozenset({Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.BREAKOUT})

    def __init__(self, *, allow_short: bool = True) -> None:
        self._allow_short = allow_short
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "momentum"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or not self.is_active(regime):
            return None

        diff = fs.sma_fast - fs.sma_slow
        sign = 1 if diff > 0 else (-1 if diff < 0 else 0)
        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign

        if sign == 0 or sign == prev:
            return None  # no fresh crossover event

        confidence = min(1.0, abs(fs.trend))
        expected_return = abs(fs.trend) * max(fs.realized_vol, 0.001)

        if sign > 0:
            return Signal(
                instrument=fs.instrument,
                action=Action.BUY,
                confidence=confidence,
                expected_return=expected_return,
                stop_distance=fs.stop_distance,
                strategy=self.name,
                regime=regime,
                ts=fs.ts,
                rationale=f"bullish crossover trend={fs.trend:.2f}",
            )

        if not self._allow_short:
            # Long-only: a bearish crossover is an exit, not a new short.
            return Signal(
                instrument=fs.instrument,
                action=Action.CLOSE,
                confidence=confidence,
                expected_return=0.0,
                stop_distance=fs.stop_distance,
                strategy=self.name,
                regime=regime,
                ts=fs.ts,
                rationale=f"bearish crossover (exit) trend={fs.trend:.2f}",
            )

        return Signal(
            instrument=fs.instrument,
            action=Action.SELL,
            confidence=confidence,
            expected_return=expected_return,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"bearish crossover trend={fs.trend:.2f}",
        )
