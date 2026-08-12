"""Macro specialist (§8 Macro).

A rate-cycle bias on risk assets: an easing central bank (falling policy rate) is supportive
for its currency's equities/indices → long bias; a tightening cycle (rising rate) is a
headwind → step out or lean short. This is a deliberately simple, transparent macro heuristic
over the shared `RatesTable`'s rate *trend* — not a claim that rate moves mechanically set
prices, so it only acts once a clear cycle direction has formed.
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from ..macro.rates import RatesTable
from .base import Signal, Strategy


class MacroStrategy(Strategy):
    active_regimes = frozenset()

    def __init__(self, rates: RatesTable, *, trend_threshold: float = 0.005, allow_short: bool = False) -> None:
        self._rates = rates
        self._trend_threshold = trend_threshold
        self._allow_short = allow_short
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "macro"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or regime is Regime.PANIC:
            return None
        rate_trend = self._rates.trend(fs.instrument.currency)

        sign = 0
        if rate_trend <= -self._trend_threshold:
            sign = 1        # easing cycle => risk-on, long bias
        elif rate_trend >= self._trend_threshold:
            sign = -1       # tightening cycle => risk-off

        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign
        if sign == 0 or sign == prev:
            return None

        if sign > 0:
            action = Action.BUY
        elif self._allow_short:
            action = Action.SELL
        else:
            action = Action.CLOSE
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=min(1.0, abs(rate_trend) / (2 * self._trend_threshold)),
            expected_return=abs(rate_trend) if action is not Action.CLOSE else 0.0,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"rate_trend({fs.instrument.currency})={rate_trend:+.2%}",
        )
