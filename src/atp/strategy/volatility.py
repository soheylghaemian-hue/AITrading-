"""Volatility / Options specialist (§8, using §5 derivative signals).

A contrarian volatility-sentiment strategy on the *underlying*: when options are pricing in
extreme fear — ATM IV rich versus its own history AND puts heavily bid over calls — it fades
the panic (buys); when they price in complacency (cheap IV, low put/call) it steps out or
leans short. This reads the options engine's chain features; it does not (yet) trade options
directly (that needs multi-leg execution & assignment modeling — a named gap).

It is explicitly a *sentiment heuristic*, not a claim that IV predicts direction; the signal
is gated on both IV rank and put/call so it only fires at genuine extremes.
"""

from __future__ import annotations

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from ..options.engine import OptionsEngine
from .base import Signal, Strategy


class VolatilityStrategy(Strategy):
    active_regimes = frozenset()

    def __init__(
        self,
        engine: OptionsEngine,
        *,
        iv_rank_high: float = 0.80,
        iv_rank_low: float = 0.20,
        pc_high: float = 1.3,
        pc_low: float = 0.7,
        allow_short: bool = False,
    ) -> None:
        self._engine = engine
        self._iv_rank_high = iv_rank_high
        self._iv_rank_low = iv_rank_low
        self._pc_high = pc_high
        self._pc_low = pc_low
        self._allow_short = allow_short
        self._prev_sign: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "volatility"

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready:
            return None
        key = fs.instrument.key
        feats = self._engine.features(key)
        rank = self._engine.iv_rank(key)
        if feats is None or rank is None:
            return None

        sign = 0
        if rank >= self._iv_rank_high and feats.put_call_oi_ratio >= self._pc_high:
            sign = 1                      # extreme fear => contrarian long
        elif rank <= self._iv_rank_low and feats.put_call_oi_ratio <= self._pc_low:
            sign = -1                     # complacency => step out / lean short

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
        confidence = min(1.0, abs(rank - 0.5) * 2)
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=confidence,
            expected_return=feats.atm_iv * 0.1 if action is not Action.CLOSE else 0.0,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=f"iv_rank={rank:.2f} pc_oi={feats.put_call_oi_ratio:.2f} skew={feats.iv_skew:.3f}",
        )
