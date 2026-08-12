"""Cross-Asset Intelligence (§4/§6 — the Global Market Brain).

The concept's premise is that the system reads *relationships and divergences between markets*,
not just single instruments (§6): Gold ↔ USD ↔ real yields, Oil ↔ energy equities, Nasdaq ↔
bond yields, EUR/USD ↔ rate differentials, Copper ↔ growth. This engine tracks such
relationships and quantifies, over a rolling window, whether two instruments are *confirming*
(co-moving as history says they should) or *diverging* (one has decoupled from the other).

A `Relationship(leader, follower, expected_sign)` says "the follower usually moves `sign`×
with the leader". The engine measures how far the follower's cumulative move deviates from what
the leader — given their realized correlation — implies, normalized to a comparable score. A
large divergence is a cross-asset trading hypothesis (spread convergence); confirmation is a
trend filter. Pure and dependency-free (stdlib), so it runs in the offline suite (§25).
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

from ..core.events import Bar, Instrument


@dataclass(slots=True, frozen=True)
class Relationship:
    leader: str          # instrument key
    follower: str        # instrument key
    expected_sign: int   # +1 (moves together) or -1 (moves opposite)

    def __post_init__(self) -> None:
        if self.expected_sign not in (1, -1):
            raise ValueError("expected_sign must be +1 or -1")


@dataclass(slots=True)
class CrossAssetView:
    follower: str
    leader: str
    n: int
    correlation: float       # realized Pearson corr of returns (signed to expectation)
    leader_cum: float        # cumulative leader return over the window
    follower_cum: float      # cumulative follower return over the window
    implied: float           # follower move implied by the leader + their co-movement
    divergence: float        # follower_cum - implied (raw)
    divergence_z: float      # divergence normalized by follower vol (comparable across pairs)
    confirming: bool         # correlation strong & follower tracking the leader

    @property
    def ready(self) -> bool:
        return self.n >= 2


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (sx ** 0.5 * sy ** 0.5)


class CrossAssetEngine:
    def __init__(
        self,
        relationships: list[Relationship],
        *,
        window: int = 30,
        min_window: int = 15,
        corr_confirm: float = 0.5,
    ) -> None:
        self._rels = list(relationships)
        self._window = window
        self._min_window = min_window
        self._corr_confirm = corr_confirm
        self._returns: dict[str, deque[float]] = {}
        self._prev_close: dict[str, float] = {}
        # followers -> relationships, for quick assessment lookup.
        self._by_follower: dict[str, list[Relationship]] = {}
        for r in self._rels:
            self._by_follower.setdefault(r.follower, []).append(r)

    def update_bar(self, bar: Bar) -> None:
        key = bar.instrument.key
        prev = self._prev_close.get(key)
        self._prev_close[key] = bar.close
        if prev is not None and prev > 0:
            dq = self._returns.setdefault(key, deque(maxlen=self._window))
            dq.append((bar.close - prev) / prev)

    def _paired(self, a: str, b: str) -> tuple[list[float], list[float]]:
        """Return the last-k aligned returns of a and b (k = shorter of the two)."""
        ra, rb = self._returns.get(a), self._returns.get(b)
        if not ra or not rb:
            return [], []
        k = min(len(ra), len(rb))
        return list(ra)[-k:], list(rb)[-k:]

    def view(self, relationship: Relationship) -> CrossAssetView | None:
        lead, foll = self._paired(relationship.leader, relationship.follower)
        n = len(lead)
        if n < self._min_window:
            return None

        sign = relationship.expected_sign
        corr = _correlation(lead, foll) * sign          # signed to expectation
        leader_cum = sum(lead)
        follower_cum = sum(foll)

        std_lead = statistics.pstdev(lead) if n >= 2 else 0.0
        std_foll = statistics.pstdev(foll) if n >= 2 else 0.0
        # Regression-style beta of follower on (sign-adjusted) leader, scaled by co-movement.
        beta = corr * (std_foll / std_lead) if std_lead > 0 else 0.0
        implied = sign * beta * leader_cum
        divergence = follower_cum - implied
        denom = std_foll * (n ** 0.5)
        divergence_z = divergence / denom if denom > 0 else 0.0
        confirming = corr >= self._corr_confirm and abs(divergence_z) < 1.0

        return CrossAssetView(
            follower=relationship.follower, leader=relationship.leader, n=n,
            correlation=corr, leader_cum=leader_cum, follower_cum=follower_cum,
            implied=implied, divergence=divergence, divergence_z=divergence_z,
            confirming=confirming,
        )

    def assessment(self, instrument: Instrument | str) -> CrossAssetView | None:
        """Most significant divergence view for `instrument` as a follower, or None."""
        key = instrument if isinstance(instrument, str) else instrument.key
        best: CrossAssetView | None = None
        for rel in self._by_follower.get(key, []):
            v = self.view(rel)
            if v is None:
                continue
            if best is None or abs(v.divergence_z) > abs(best.divergence_z):
                best = v
        return best
