"""Market Universe Scanner (§6).

We can't subscribe to real-time deep data for tens of thousands of instruments at once. The
scanner is a hierarchical funnel that narrows the global universe down to the handful of names
worth intensive real-time analysis:

    GLOBAL UNIVERSE → LIQUIDITY FILTER → VOLATILITY FILTER → MOMENTUM/ANOMALY FILTER
    → OPPORTUNITY RANK → (deep real-time data → AI traders)

Input is a list of lightweight per-instrument summaries (from a broad, cheap discovery scan —
snapshot data, daily bars). It never fabricates data: candidates come from the caller. Output
is the ranked shortlist plus the funnel counts, so the decision is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.enums import AssetClass


@dataclass(slots=True)
class ScanCandidate:
    key: str
    symbol: str
    asset_class: AssetClass
    adv: float                 # average daily volume / liquidity proxy
    volatility: float          # realized volatility (fraction)
    momentum: float            # signed trend/return over the scan window
    rel_volume: float = 1.0    # today's volume vs. its average (anomaly signal)
    spread_bps: float = 0.0    # top-of-book spread (liquidity quality)


@dataclass(slots=True)
class ScanConfig:
    min_adv: float = 0.0
    max_spread_bps: float = 100.0
    min_volatility: float = 0.0
    max_volatility: float = 1e9
    min_abs_momentum: float = 0.0
    min_rel_volume: float = 0.0
    top_n: int = 50


@dataclass(slots=True)
class ScanResult:
    universe: int
    after_liquidity: int
    after_volatility: int
    after_momentum: int
    selected: list[ScanCandidate] = field(default_factory=list)

    def funnel(self) -> dict[str, int]:
        return {
            "universe": self.universe,
            "after_liquidity": self.after_liquidity,
            "after_volatility": self.after_volatility,
            "after_momentum": self.after_momentum,
            "selected": len(self.selected),
        }


class MarketUniverseScanner:
    def __init__(self, config: ScanConfig | None = None) -> None:
        self._cfg = config or ScanConfig()

    def _score(self, c: ScanCandidate) -> float:
        """Opportunity score: momentum, boosted by volume anomaly, scaled by liquidity quality."""
        liquidity_quality = 1.0 / (1.0 + c.spread_bps / 10.0)
        anomaly = 1.0 + max(0.0, c.rel_volume - 1.0)
        return abs(c.momentum) * anomaly * liquidity_quality

    def scan(self, candidates: list[ScanCandidate]) -> ScanResult:
        cfg = self._cfg
        universe = len(candidates)

        liquid = [c for c in candidates if c.adv >= cfg.min_adv and c.spread_bps <= cfg.max_spread_bps]
        vol = [c for c in liquid if cfg.min_volatility <= c.volatility <= cfg.max_volatility]
        mom = [c for c in vol if abs(c.momentum) >= cfg.min_abs_momentum and c.rel_volume >= cfg.min_rel_volume]

        ranked = sorted(mom, key=self._score, reverse=True)[: cfg.top_n]
        return ScanResult(
            universe=universe, after_liquidity=len(liquid),
            after_volatility=len(vol), after_momentum=len(mom), selected=ranked,
        )
