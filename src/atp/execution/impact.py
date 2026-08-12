"""Market-impact model (§16/§20).

Estimates the adverse price move an order causes, as a function of its size relative to
available liquidity (average volume). Uses the standard **square-root law**: impact grows with
the square root of participation, so cost is *convex* in size — which is exactly why breaking a
large order into smaller slices lowers total impact (see `execution.algo.SlicingAlgo`).

    impact_bps = eta_bps · (quantity / adv)^exponent · vol_factor

Pure and parameterized so it's auditable and testable; it feeds both the PaperBroker (to make
fills realistic) and the slicing algo (to decide how much to split).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MarketImpactModel:
    eta_bps: float = 50.0        # impact in bps at 100% participation (quantity == adv)
    exponent: float = 0.5        # 0.5 == square-root law
    vol_ref: float = 0.01        # realized-vol reference for the (optional) vol scaling
    vol_floor: float = 0.25      # never scale impact below this fraction

    def impact_bps(self, quantity: float, adv: float, volatility: float | None = None) -> float:
        """Adverse impact in basis points for `quantity` against average volume `adv`."""
        if adv <= 0 or quantity <= 0:
            return 0.0
        participation = quantity / adv
        impact = self.eta_bps * (participation ** self.exponent)
        if volatility is not None and self.vol_ref > 0:
            impact *= max(self.vol_floor, volatility / self.vol_ref)
        return impact

    def cost_bps_for_slices(self, quantity: float, adv: float, n_slices: int,
                            volatility: float | None = None) -> float:
        """Average impact (bps) of executing `quantity` as `n_slices` equal children.

        With the square-root law this scales like 1/sqrt(n_slices): more slices, less cost.
        Used to show slicing helps and to pick a slice count."""
        n = max(1, n_slices)
        child = quantity / n
        # Notional-weighted average impact across children == impact_bps(child).
        return self.impact_bps(child, adv, volatility)
