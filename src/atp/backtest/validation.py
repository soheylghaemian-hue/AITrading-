"""Out-of-sample, walk-forward and Monte-Carlo validation (§11).

The goal (Master-Prompt §10/§11/§25): never trust an in-sample curve. These helpers make
it structurally easy to (a) hold out data, (b) roll train/test windows forward in time,
and (c) resample trade outcomes to see the *distribution* of results, not one lucky path.

Dependency-free (uses stdlib `random`) so it runs in the offline suite.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..core.events import Bar
from .engine import Backtester, BacktestResult
from .metrics import BacktestMetrics, compute_metrics, max_drawdown


def train_test_split(bars: list[Bar], train_frac: float = 0.7) -> tuple[list[Bar], list[Bar]]:
    """Chronological split — the test set is strictly *after* the train set (no leakage)."""
    if not 0 < train_frac < 1:
        raise ValueError("train_frac must be in (0, 1)")
    cut = int(len(bars) * train_frac)
    return bars[:cut], bars[cut:]


def walk_forward_windows(
    bars: list[Bar], train_size: int, test_size: int, step: int | None = None
) -> list[tuple[list[Bar], list[Bar]]]:
    """Rolling (train, test) windows moving forward in time.

    Each test window immediately follows its train window; windows never overlap the
    future into the past. `step` defaults to `test_size` (non-overlapping test blocks).
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    step = step or test_size
    windows: list[tuple[list[Bar], list[Bar]]] = []
    start = 0
    while start + train_size + test_size <= len(bars):
        train = bars[start : start + train_size]
        test = bars[start + train_size : start + train_size + test_size]
        windows.append((train, test))
        start += step
    return windows


@dataclass(slots=True)
class WalkForwardResult:
    window_metrics: list[BacktestMetrics] = field(default_factory=list)
    combined_metrics: BacktestMetrics | None = None

    @property
    def n_windows(self) -> int:
        return len(self.window_metrics)


async def walk_forward(
    backtester: Backtester,
    windows: list[tuple[list[Bar], list[Bar]]],
    periods_per_year: float = 252.0,
) -> WalkForwardResult:
    """Run the backtester on each window's OUT-OF-SAMPLE (test) segment and aggregate.

    The train segment is where a future, parameter-fitting strategy would calibrate; the
    reported metrics come only from the untouched test segments — the honest number.
    """
    result = WalkForwardResult()
    combined_equity: list[float] = []
    combined_trades: list[float] = []
    for _train, test in windows:
        res: BacktestResult = await backtester.run(test, periods_per_year)
        result.window_metrics.append(res.metrics(periods_per_year))
        # Chain equity curves so the combined view reflects sequential deployment.
        if combined_equity and res.equity_curve:
            scale = combined_equity[-1] / res.equity_curve[0] if res.equity_curve[0] else 1.0
            combined_equity.extend(v * scale for v in res.equity_curve)
        else:
            combined_equity.extend(res.equity_curve)
        combined_trades.extend(res.trade_pnls)

    if combined_equity:
        result.combined_metrics = compute_metrics(
            combined_equity, combined_trades, periods_per_year
        )
    return result


@dataclass(slots=True)
class MonteCarloResult:
    n_runs: int
    starting_equity: float
    final_equity_p05: float
    final_equity_p50: float
    final_equity_p95: float
    max_drawdown_p50: float
    max_drawdown_p95: float
    prob_loss: float          # fraction of runs ending below starting equity


def monte_carlo_trade_order(
    trade_pnls: list[float],
    starting_equity: float,
    n_runs: int = 1000,
    seed: int | None = 42,
) -> MonteCarloResult:
    """Bootstrap the ORDER of realized trades to estimate the outcome distribution.

    Resampling trade order (with replacement) breaks any lucky sequencing and exposes the
    tail risk a single equity path hides (§11 'tail risk', §13 drawdown). It answers:
    across many plausible orderings of these trades, how bad can the drawdown get?
    """
    rng = random.Random(seed)
    if not trade_pnls:
        return MonteCarloResult(
            n_runs=0,
            starting_equity=starting_equity,
            final_equity_p05=starting_equity,
            final_equity_p50=starting_equity,
            final_equity_p95=starting_equity,
            max_drawdown_p50=0.0,
            max_drawdown_p95=0.0,
            prob_loss=0.0,
        )

    finals: list[float] = []
    mdds: list[float] = []
    n = len(trade_pnls)
    for _ in range(n_runs):
        sample = [trade_pnls[rng.randrange(n)] for _ in range(n)]
        curve = [starting_equity]
        eq = starting_equity
        for p in sample:
            eq += p
            curve.append(eq)
        finals.append(eq)
        mdds.append(max_drawdown(curve))

    finals.sort()
    mdds.sort()

    def pct(sorted_xs: list[float], q: float) -> float:
        if not sorted_xs:
            return 0.0
        idx = min(len(sorted_xs) - 1, max(0, int(q * len(sorted_xs))))
        return sorted_xs[idx]

    return MonteCarloResult(
        n_runs=n_runs,
        starting_equity=starting_equity,
        final_equity_p05=pct(finals, 0.05),
        final_equity_p50=pct(finals, 0.50),
        final_equity_p95=pct(finals, 0.95),
        max_drawdown_p50=pct(mdds, 0.50),
        max_drawdown_p95=pct(mdds, 0.95),
        prob_loss=sum(1 for f in finals if f < starting_equity) / len(finals),
    )
