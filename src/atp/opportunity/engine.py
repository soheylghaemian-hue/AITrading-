"""Opportunity Engine (§10, §24 Phase 8).

Every actionable signal is turned into a running *opportunity score* on a common scale so
the desk can compare a commodity breakout against an FX mean-reversion and pick the best
risk-adjusted chance across all asset classes (§10) — the core question of the concept:
"where, worldwide, is the most attractive risk-adjusted opportunity right now?"

Score = confidence × (expected_return / fractional_risk). It is a reward-for-risk figure
scaled by how much the specialist believes its own view. Risk-reducing exits (CLOSE) always
rank first — flattening risk is never gated by an opportunity threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import Action
from ..strategy.base import Signal


@dataclass(slots=True)
class Opportunity:
    signal: Signal
    price: float
    score: float
    reward_risk: float
    fractional_risk: float

    @property
    def instrument(self):
        return self.signal.instrument


class OpportunityEngine:
    def __init__(self, *, min_score: float = 0.0) -> None:
        self._min_score = min_score

    def score(self, signal: Signal, price: float) -> Opportunity:
        frac_risk = (signal.stop_distance / price) if price > 0 else 0.0
        if signal.action is Action.CLOSE:
            # Exits are always actionable; give them a dominating score.
            return Opportunity(signal, price, float("inf"), float("inf"), frac_risk)
        reward_risk = (signal.expected_return / frac_risk) if frac_risk > 0 else 0.0
        return Opportunity(signal, price, signal.confidence * reward_risk, reward_risk, frac_risk)

    def rank(self, scored: list[tuple[Signal, float]]) -> list[Opportunity]:
        """Score `(signal, price)` pairs, filter by `min_score`, best first."""
        opps = [self.score(sig, price) for sig, price in scored]
        opps = [o for o in opps if o.score >= self._min_score]
        opps.sort(key=lambda o: o.score, reverse=True)
        return opps
