"""Statistical Arbitrage specialist (§8).

A genuine two-leg pairs trade expressed through the per-instrument signal interface: it holds
the shared `StatArbEngine` (fed every bar by the desk) and, when asked about an instrument,
returns that instrument's *leg* of the trade. When the spread is stretched, the desk calls
`generate` for both legs in the same step — SELL the rich one, BUY the cheap one — building a
market-neutral position. It only acts while the two legs are still correlated (relationship
intact); it stands aside in panic, when such relationships break down (§7).
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from ..stat_arb.engine import StatArbEngine
from .base import Signal, Strategy


class StatArbStrategy(Strategy):
    active_regimes = frozenset()

    def __init__(self, engine: StatArbEngine, *, entry_z: float = 2.0, corr_min: float = 0.5) -> None:
        self._engine = engine
        self._entry_z = entry_z
        self._corr_min = corr_min
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "stat_arb"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or regime is Regime.PANIC:
            return None
        view = self._engine.assessment(fs.instrument.key)
        if view is None or view.correlation < self._corr_min:
            return None

        sign = view.leg_sign(self._entry_z)
        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign
        if sign == 0 or sign == prev:
            return None

        confidence = min(1.0, abs(view.z) / (2 * self._entry_z))
        # β-weighted hedge: leg A gets base units, leg B gets β·base units, both sized from the
        # same reference price (leg A) => qty_b = β·qty_a => market-neutral to the shared move.
        hedge_factor = 1.0 if view.role == "a" else abs(view.beta)
        return Signal(
            instrument=fs.instrument,
            action=Action.BUY if sign > 0 else Action.SELL,
            confidence=confidence,
            expected_return=abs(view.z) * max(fs.realized_vol, 0.001),
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"pairs {view.a}~{view.b} z={view.z:.2f} beta={view.beta:.2f} leg={view.role}",
            sizing="hedged",
            hedge_factor=hedge_factor,
            ref_price=view.price_a,
        )
