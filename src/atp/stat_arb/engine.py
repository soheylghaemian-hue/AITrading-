"""Statistical Arbitrage — pairs spread engine (§8).

Tracks a hedge-ratio spread between two instruments and its z-score. When the spread stretches
(one leg rich, the other cheap) the pair is a mean-reversion opportunity: sell the rich leg,
buy the cheap one, and profit as the spread converges — market-neutral by construction.

The spread is `price_a − β·price_b`, with β the OLS hedge ratio over a rolling window; the
z-score standardizes the current spread against its own recent distribution. A returns
correlation gate ensures the relationship is still intact before acting. Pure stdlib.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

from ..core.events import Bar


@dataclass(slots=True, frozen=True)
class Pair:
    a: str   # instrument key
    b: str   # instrument key


@dataclass(slots=True)
class StatArbView:
    a: str
    b: str
    symbol: str          # the instrument this view is for
    role: str            # "a" or "b"
    n: int
    z: float             # spread z-score (positive => a rich / b cheap)
    beta: float          # hedge ratio
    correlation: float   # returns correlation of the two legs
    price_a: float       # latest price of leg a (the reference leg for hedged sizing)
    price_b: float       # latest price of leg b

    def leg_sign(self, entry_z: float) -> int:
        """Trade direction for this leg when the spread is beyond `entry_z` (0 otherwise)."""
        if abs(self.z) < entry_z:
            return 0
        s = 1 if self.z > 0 else -1          # spread high => a expensive, b cheap
        return -s if self.role == "a" else s  # sell the rich leg, buy the cheap one


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx ** 0.5 * sy ** 0.5)


def _beta(pa: list[float], pb: list[float]) -> float:
    """OLS slope of a on b (hedge ratio)."""
    mb = statistics.fmean(pb)
    var_b = sum((x - mb) ** 2 for x in pb)
    if var_b <= 0:
        return 0.0
    ma = statistics.fmean(pa)
    cov = sum((x - ma) * (y - mb) for x, y in zip(pa, pb))
    return cov / var_b


class StatArbEngine:
    def __init__(self, pairs: list[Pair], *, window: int = 40, min_window: int = 20) -> None:
        self._pairs = list(pairs)
        self._window = window
        self._min_window = min_window
        self._prices: dict[str, deque[float]] = {}
        self._by_symbol: dict[str, list[Pair]] = {}
        for p in self._pairs:
            self._by_symbol.setdefault(p.a, []).append(p)
            self._by_symbol.setdefault(p.b, []).append(p)

    def update_bar(self, bar: Bar) -> None:
        dq = self._prices.setdefault(bar.instrument.key, deque(maxlen=self._window))
        dq.append(bar.close)

    def _paired(self, a: str, b: str) -> tuple[list[float], list[float]]:
        pa, pb = self._prices.get(a), self._prices.get(b)
        if not pa or not pb:
            return [], []
        k = min(len(pa), len(pb))
        return list(pa)[-k:], list(pb)[-k:]

    def view_for(self, pair: Pair, symbol: str) -> StatArbView | None:
        pa, pb = self._paired(pair.a, pair.b)
        n = len(pa)
        if n < self._min_window:
            return None
        beta = _beta(pa, pb)
        spread = [a - beta * b for a, b in zip(pa, pb)]
        mu, sd = statistics.fmean(spread), (statistics.pstdev(spread) if n >= 2 else 0.0)
        z = (spread[-1] - mu) / sd if sd > 0 else 0.0
        ra = [pa[i] - pa[i - 1] for i in range(1, n)]
        rb = [pb[i] - pb[i - 1] for i in range(1, n)]
        corr = _correlation(ra, rb)
        role = "a" if symbol == pair.a else "b"
        return StatArbView(a=pair.a, b=pair.b, symbol=symbol, role=role, n=n,
                           z=z, beta=beta, correlation=corr, price_a=pa[-1], price_b=pb[-1])

    def assessment(self, symbol: str) -> StatArbView | None:
        best: StatArbView | None = None
        for pair in self._by_symbol.get(symbol, []):
            v = self.view_for(pair, symbol)
            if v is None:
                continue
            if best is None or abs(v.z) > abs(best.z):
                best = v
        return best
