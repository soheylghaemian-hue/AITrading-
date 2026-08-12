"""Data-driven rule strategies for Strategy Discovery (§12).

Discovery needs a *family* of candidate strategies it can enumerate and validate, not
hand-written ones. A `RuleStrategy` is exactly that: a threshold rule on one feature
(go long when the feature is strongly positive, short when strongly negative), optionally
gated by filter predicates on other features. Every candidate is fully described by its
params, so a survivor can be versioned and governed (§19) like any other model.

Feature access goes through a small accessor table (not raw getattr) so the search space is
an explicit, auditable set of signals — price/vol/trend today; options-flow, cross-asset and
news (§12) plug in here later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from ..strategy.base import Signal, Strategy

# --- the auditable feature vocabulary discovery may search over (§12) --------
FEATURE_ACCESSORS: dict[str, Callable[[FeatureSet], float]] = {
    "trend": lambda fs: fs.trend,
    "ret": lambda fs: fs.ret,
    "realized_vol": lambda fs: fs.realized_vol,
    "vol_percentile": lambda fs: fs.vol_percentile,
    "rel_volume": lambda fs: fs.rel_volume,
    "momentum": lambda fs: (fs.sma_fast / fs.sma_slow - 1.0) if fs.sma_slow else 0.0,
    "zscore": lambda fs: (fs.price - fs.sma_slow) / fs.close_std if fs.close_std > 0 else 0.0,
}

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


@dataclass(slots=True, frozen=True)
class FeaturePredicate:
    feature: str
    op: str
    threshold: float

    def __post_init__(self) -> None:
        if self.feature not in FEATURE_ACCESSORS:
            raise ValueError(f"unknown feature '{self.feature}'")
        if self.op not in _OPS:
            raise ValueError(f"unknown op '{self.op}'")

    def value(self, fs: FeatureSet) -> float:
        return FEATURE_ACCESSORS[self.feature](fs)

    def holds(self, fs: FeatureSet) -> bool:
        return _OPS[self.op](self.value(fs), self.threshold)

    def __str__(self) -> str:
        return f"{self.feature}{self.op}{self.threshold:g}"


class RuleStrategy(Strategy):
    """Long when `signal_feature` > +threshold, short/exit when < -threshold; filters must all
    hold for any action. Fires on the *event* of crossing the band, not every bar inside it."""

    def __init__(
        self,
        *,
        signal_feature: str = "trend",
        entry_threshold: float = 0.3,
        filters: tuple[FeaturePredicate, ...] = (),
        allow_short: bool = True,
        active_regimes: frozenset[Regime] = frozenset(),
        name: str | None = None,
    ) -> None:
        if signal_feature not in FEATURE_ACCESSORS:
            raise ValueError(f"unknown signal feature '{signal_feature}'")
        if entry_threshold <= 0:
            raise ValueError("entry_threshold must be positive")
        self._feature = signal_feature
        self._threshold = entry_threshold
        self._filters = tuple(filters)
        self._allow_short = allow_short
        self.active_regimes = active_regimes
        self._name = name or self._default_name()
        self._prev_sign: dict[str, int] = {}

    def _default_name(self) -> str:
        base = f"rule({self._feature}>{self._threshold:g})"
        if self._filters:
            base += "[" + ",".join(str(f) for f in self._filters) + "]"
        return base

    @property
    def name(self) -> str:
        return self._name

    @property
    def params(self) -> dict:
        return {
            "signal_feature": self._feature,
            "entry_threshold": self._threshold,
            "filters": [str(f) for f in self._filters],
            "allow_short": self._allow_short,
            "active_regimes": sorted(r.value for r in self.active_regimes),
        }

    def reset(self) -> None:
        self._prev_sign.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready or not self.is_active(regime):
            return None
        if not all(f.holds(fs) for f in self._filters):
            return None

        val = FEATURE_ACCESSORS[self._feature](fs)
        sign = 1 if val > self._threshold else (-1 if val < -self._threshold else 0)
        key = fs.instrument.key
        prev = self._prev_sign.get(key, 0)
        self._prev_sign[key] = sign

        if sign == 0 or sign == prev:
            return None  # no fresh band crossing

        confidence = min(1.0, abs(val) / (2 * self._threshold))
        expected_return = abs(val) * max(fs.realized_vol, 0.001)

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
            strategy=self._name,
            regime=regime,
            ts=fs.ts,
            rationale=f"{self._feature}={val:.3f} vs ±{self._threshold:g}",
        )
