"""Strategy Discovery: search + the mandatory validation gauntlet (§12/§13).

§12 requires that a discovered hypothesis pass Backtest, Out-of-Sample, Walk-Forward and
Monte-Carlo before it may be trusted (paper trading is the final, live gate — out of scope
for the offline suite). This module enumerates candidate `RuleStrategy`s from an explicit
search space and runs each through exactly that gauntlet, accepting only those that clear
every gate on *out-of-sample* data.

Honesty about selection bias (§13): trying many candidates and keeping the best inflates
apparent edge. We do NOT hide this — `DiscoveryResult` reports how many candidates were
tried so the operator can discount for multiple testing, and acceptance is judged on OOS +
walk-forward + Monte-Carlo, never in-sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backtest.engine import Backtester
from ..backtest.metrics import BacktestMetrics
from ..backtest.validation import (
    monte_carlo_trade_order,
    train_test_split,
    walk_forward,
    walk_forward_windows,
)
from ..core.events import Bar
from ..logging_config import get_logger
from ..policy import TradingPolicy
from ..regime.classifier import RegimeClassifier
from .rules import FeaturePredicate, RuleStrategy

log = get_logger("discovery")


@dataclass(slots=True)
class SearchSpace:
    """The candidate universe: threshold grids per feature, optional filter-sets."""

    feature_grid: dict[str, list[float]]
    allow_short: bool = True
    filter_sets: tuple[tuple[FeaturePredicate, ...], ...] = ((),)

    @classmethod
    def default(cls) -> "SearchSpace":
        return cls(
            feature_grid={
                "trend": [0.2, 0.3, 0.5],
                "momentum": [0.002, 0.005, 0.01],
                "zscore": [1.0, 1.5, 2.0],
            }
        )

    def candidates(self) -> list[RuleStrategy]:
        out: list[RuleStrategy] = []
        for feature, thresholds in self.feature_grid.items():
            for th in thresholds:
                for filters in self.filter_sets:
                    out.append(
                        RuleStrategy(
                            signal_feature=feature,
                            entry_threshold=th,
                            filters=filters,
                            allow_short=self.allow_short,
                        )
                    )
        return out


@dataclass(slots=True)
class DiscoveryCriteria:
    min_trades: int = 10               # on the OOS segment
    min_oos_sharpe: float = 0.5
    min_oos_profit_factor: float = 1.1
    min_oos_return: float = 0.0
    max_mc_prob_loss: float = 0.5
    min_wf_win_fraction: float = 0.5   # fraction of walk-forward windows that are profitable
    periods_per_year: float = 252.0


@dataclass(slots=True)
class ValidationReport:
    name: str
    params: dict
    in_sample: BacktestMetrics
    oos: BacktestMetrics
    wf_windows: int
    wf_win_fraction: float
    mc_prob_loss: float
    mc_final_p05: float
    mc_final_p50: float
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(slots=True)
class DiscoveryResult:
    n_candidates: int
    reports: list[ValidationReport]

    @property
    def passed(self) -> list[ValidationReport]:
        winners = [r for r in self.reports if r.passed]
        winners.sort(key=lambda r: r.oos.sharpe, reverse=True)
        return winners

    @property
    def best(self) -> ValidationReport | None:
        winners = self.passed
        return winners[0] if winners else None

    @property
    def selection_note(self) -> str:
        return (
            f"{len(self.passed)}/{self.n_candidates} candidates passed. "
            "Discount for multiple testing: trying many rules and keeping the best inflates "
            "apparent edge — treat survivors as hypotheses for paper trading, not proof."
        )


class StrategyDiscovery:
    def __init__(
        self,
        *,
        policy: TradingPolicy,
        criteria: DiscoveryCriteria | None = None,
        regime: RegimeClassifier | None = None,
        train_frac: float = 0.6,
        wf_train: int | None = None,
        wf_test: int | None = None,
        mc_runs: int = 1000,
        spread_bps: float = 2.0,
        slippage_bps: float = 1.0,
    ) -> None:
        self._policy = policy
        self._criteria = criteria or DiscoveryCriteria()
        self._regime = regime or RegimeClassifier()
        self._train_frac = train_frac
        self._wf_train = wf_train
        self._wf_test = wf_test
        self._mc_runs = mc_runs
        self._spread_bps = spread_bps
        self._slippage_bps = slippage_bps

    def _backtester(self, candidate: RuleStrategy) -> Backtester:
        return Backtester(
            policy=self._policy,
            strategies=[candidate],
            regime=self._regime,
            spread_bps=self._spread_bps,
            slippage_bps=self._slippage_bps,
        )

    async def validate(self, candidate: RuleStrategy, bars: list[Bar]) -> ValidationReport:
        ppy = self._criteria.periods_per_year
        bt = self._backtester(candidate)

        # 1) In-sample & 2) Out-of-sample (chronological split — test strictly after train).
        train, test = train_test_split(bars, self._train_frac)
        is_res = await bt.run(train, ppy)
        oos_res = await bt.run(test, ppy)
        is_m = is_res.metrics(ppy)
        oos_m = oos_res.metrics(ppy)

        # 3) Walk-forward across the whole series (out-of-sample per window).
        wt = self._wf_train or max(20, len(bars) // 5)
        wtest = self._wf_test or max(10, len(bars) // 10)
        windows = walk_forward_windows(bars, wt, wtest)
        wf = await walk_forward(bt, windows, ppy) if windows else None
        if wf and wf.window_metrics:
            wf_win_fraction = sum(1 for m in wf.window_metrics if m.total_return > 0) / len(wf.window_metrics)
            wf_windows = len(wf.window_metrics)
        else:
            wf_win_fraction, wf_windows = 0.0, 0

        # 4) Monte-Carlo on the OOS trades (distribution, not one lucky path).
        mc = monte_carlo_trade_order(oos_res.trade_pnls, self._policy.capital, n_runs=self._mc_runs)

        failures = self._judge(oos_m, wf_win_fraction, wf_windows, mc.prob_loss)
        return ValidationReport(
            name=candidate.name,
            params=candidate.params,
            in_sample=is_m,
            oos=oos_m,
            wf_windows=wf_windows,
            wf_win_fraction=wf_win_fraction,
            mc_prob_loss=mc.prob_loss,
            mc_final_p05=mc.final_equity_p05,
            mc_final_p50=mc.final_equity_p50,
            failures=failures,
        )

    def _judge(self, oos: BacktestMetrics, wf_frac: float, wf_windows: int, mc_prob_loss: float) -> list[str]:
        c = self._criteria
        fails: list[str] = []
        if oos.n_trades < c.min_trades:
            fails.append(f"trades {oos.n_trades} < {c.min_trades}")
        if oos.sharpe < c.min_oos_sharpe:
            fails.append(f"oos_sharpe {oos.sharpe:.2f} < {c.min_oos_sharpe}")
        if oos.profit_factor < c.min_oos_profit_factor:
            fails.append(f"oos_pf {oos.profit_factor:.2f} < {c.min_oos_profit_factor}")
        if oos.total_return < c.min_oos_return:
            fails.append(f"oos_return {oos.total_return:.2%} < {c.min_oos_return:.2%}")
        if mc_prob_loss > c.max_mc_prob_loss:
            fails.append(f"mc_prob_loss {mc_prob_loss:.0%} > {c.max_mc_prob_loss:.0%}")
        if wf_windows == 0:
            fails.append("no walk-forward windows")
        elif wf_frac < c.min_wf_win_fraction:
            fails.append(f"wf_win {wf_frac:.0%} < {c.min_wf_win_fraction:.0%}")
        return fails

    async def discover(self, bars: list[Bar], space: SearchSpace | None = None) -> DiscoveryResult:
        space = space or SearchSpace.default()
        candidates = space.candidates()
        reports = [await self.validate(c, bars) for c in candidates]
        n_passed = sum(1 for r in reports if r.passed)
        log.info("discovery: %d/%d candidates passed the gauntlet", n_passed, len(candidates))
        return DiscoveryResult(n_candidates=len(candidates), reports=reports)
