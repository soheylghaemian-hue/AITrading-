"""Market Regime Engine (§7, §24 Phase 7).

Classifies each instrument's current regime so strategies can be activated, reduced or
disabled accordingly (§7). This is a transparent, rule-based classifier over the feature
set — deliberately auditable rather than a black box. A learned regime model can later slot
in behind the same `classify()` signature (§19 model governance).
"""

from __future__ import annotations

from ..core.enums import Regime
from ..features.engine import FeatureSet


class RegimeClassifier:
    def __init__(
        self,
        *,
        trend_threshold: float = 0.3,
        low_vol_percentile: float = 0.3,
        high_vol_percentile: float = 0.85,
        panic_return: float = -0.03,
    ) -> None:
        self._trend_threshold = trend_threshold
        self._low_vol_percentile = low_vol_percentile
        self._high_vol_percentile = high_vol_percentile
        self._panic_return = panic_return

    def classify(self, fs: FeatureSet) -> Regime:
        if not fs.ready:
            return Regime.UNKNOWN

        # A sharp drop in an already-stressed tape => panic (risk-off, defensive).
        if fs.vol_percentile >= self._high_vol_percentile and fs.ret <= self._panic_return:
            return Regime.PANIC

        if fs.trend >= self._trend_threshold:
            return Regime.TRENDING_UP
        if fs.trend <= -self._trend_threshold:
            return Regime.TRENDING_DOWN

        if fs.vol_percentile >= self._high_vol_percentile:
            return Regime.HIGH_VOLATILITY
        if fs.vol_percentile <= self._low_vol_percentile:
            return Regime.LOW_VOLATILITY

        # No dominant trend, moderate vol: a range to be mean-reverted.
        return Regime.RANGE
