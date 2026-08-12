"""Performance-decay monitor (§19).

Reads the experience journal (§11), scores each strategy, and — when a strategy breaches the
decay policy on a large-enough sample — moves it to probation or suspends it in the registry.
This is the loop the concept describes: observe → evaluate → *act* (take the failing strategy
offline), automatically and on data, not on a hunch.

It never fabricates an edge: every judgment is a comparison of recorded results against
thresholds the operator sets once in `DecayPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..journal.analytics import GroupStats, TradeAnalytics
from ..journal.store import TradeJournal
from ..logging_config import get_logger
from .registry import StrategyRegistry, StrategyStatus

log = get_logger("governance")


@dataclass(slots=True)
class DecayPolicy:
    min_trades: int = 12               # evidence gate — no action below this
    min_expectancy: float = 0.0        # breach if avg P&L/trade < this
    min_profit_factor: float = 1.0     # breach if PF < this
    min_win_rate: float = 0.0          # off by default (0)
    min_calibration: float = -1e9      # off by default; set > -inf to catch over-promising
    probation_first: bool = False      # first breach -> probation, next -> suspend
    auto_reactivate: bool = False      # reinstate a suspended strategy once it recovers


@dataclass(slots=True)
class GovernanceDecision:
    name: str
    action: str            # "suspend" | "probation" | "reactivate" | "keep"
    reason: str
    stats: GroupStats


class GovernanceMonitor:
    def __init__(self, registry: StrategyRegistry, policy: DecayPolicy | None = None) -> None:
        self._registry = registry
        self._policy = policy or DecayPolicy()

    def _breaches(self, g: GroupStats) -> list[str]:
        p = self._policy
        out: list[str] = []
        if g.expectancy < p.min_expectancy:
            out.append(f"expectancy {g.expectancy:+.2f}")
        if g.profit_factor < p.min_profit_factor:
            out.append(f"PF {g.profit_factor:.2f}")
        if g.win_rate < p.min_win_rate:
            out.append(f"win {g.win_rate:.0%}")
        if g.calibration < p.min_calibration:
            out.append(f"calibration {g.calibration:+.4f}")
        return out

    def evaluate(self, journal: TradeJournal) -> list[GovernanceDecision]:
        """Score every strategy in the journal and update the registry. Returns the actions."""
        analytics = TradeAnalytics.from_journal(journal)
        decisions: list[GovernanceDecision] = []

        for g in analytics.by_strategy():
            if g.n_trades < self._policy.min_trades:
                continue  # not enough evidence to judge

            breaches = self._breaches(g)
            state = self._registry.get(g.label)

            if breaches:
                reason = f"decay: {', '.join(breaches)} (n={g.n_trades})"
                already_watched = state is not None and state.status is StrategyStatus.PROBATION
                if self._policy.probation_first and not already_watched and (
                    state is None or state.status is StrategyStatus.ACTIVE
                ):
                    self._registry.set_probation(g.label, reason)
                    decisions.append(GovernanceDecision(g.label, "probation", reason, g))
                else:
                    self._registry.suspend(g.label, reason)
                    decisions.append(GovernanceDecision(g.label, "suspend", reason, g))
            elif (
                self._policy.auto_reactivate
                and state is not None
                and state.status is StrategyStatus.SUSPENDED
            ):
                reason = f"recovered: expectancy {g.expectancy:+.2f} (n={g.n_trades})"
                self._registry.reactivate(g.label, reason)
                decisions.append(GovernanceDecision(g.label, "reactivate", reason, g))

        return decisions
