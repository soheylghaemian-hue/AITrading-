"""FX Carry specialist (§8 FX).

The classic carry trade: hold the higher-yielding currency, funded in the lower-yielding one,
and earn the rate differential — long the pair when carry = rate(base) − rate(quote) is
meaningfully positive, short when meaningfully negative. Carry trades are exposed to sharp
reversals, so a price-trend gate keeps the strategy from holding a positive-carry long straight
into a strong downtrend (the way carry unwinds hurt). Reads the shared `RatesTable`.
"""

from __future__ import annotations

from ..core.enums import Action, AssetClass, Regime
from ..features.engine import FeatureSet
from ..macro.rates import RatesTable
from .base import Signal, Strategy


class FXCarryStrategy(Strategy):
    #: Carry unwinds violently in panics/liquidity stress — stand aside there (§7).
    active_regimes = frozenset()

    def __init__(self, rates: RatesTable, *, min_carry: float = 0.005, trend_block: float = 0.5) -> None:
        self._rates = rates
        self._min_carry = min_carry
        self._trend_block = trend_block
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "fx_carry"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        inst = fs.instrument
        if inst.asset_class is not AssetClass.FX or not fs.ready or regime is Regime.PANIC:
            return None
        carry = self._rates.carry(inst.symbol, inst.currency)   # base vs quote
        if carry is None:
            return None

        sign = 0
        # Positive carry => long the pair, unless price is trending hard against it.
        if carry >= self._min_carry and fs.trend > -self._trend_block:
            sign = 1
        elif carry <= -self._min_carry and fs.trend < self._trend_block:
            sign = -1

        key = inst.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign
        if sign == 0 or sign == prev:
            return None

        confidence = min(1.0, abs(carry) / (2 * self._min_carry))
        return Signal(
            instrument=inst,
            action=Action.BUY if sign > 0 else Action.SELL,
            confidence=confidence,
            expected_return=abs(carry),          # the carry earned, annualized
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"carry {inst.symbol}-{inst.currency}={carry:+.2%} trend={fs.trend:+.2f}",
        )
