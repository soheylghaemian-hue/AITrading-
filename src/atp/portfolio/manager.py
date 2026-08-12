"""Master Portfolio Manager (§9).

The specialists each propose opportunities in isolation; the Master Portfolio Manager looks at
them *together* and decides where capital actually goes. It evaluates all ranked opportunities
against a portfolio-level budget — total gross-exposure headroom, a maximum number of
positions, a per-name cap — and diversifies by declining a new name that is highly correlated
(same direction) to something already funded or held. **Cash is a valid decision:** if nothing
clears, it funds nothing.

It sits between the Opportunity Engine (which ranks) and sizing/execution (which the Risk
Engine still vetoes per order). It does not size positions — it decides *which* opportunities
are worth pursuing given the whole book.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..brokers.base import Account
from ..core.enums import Action
from ..logging_config import get_logger

log = get_logger("portfolio")


@dataclass(slots=True)
class AllocationDecision:
    opportunity: object          # atp.opportunity.engine.Opportunity
    allocate: bool
    weight: float                # target notional as a fraction of equity (0 if not allocated)
    reason: str

    @property
    def key(self) -> str:
        return self.opportunity.instrument.key


class MasterPortfolioManager:
    def __init__(
        self,
        *,
        max_positions: int = 10,
        max_gross_leverage: float = 1.0,
        per_position_cap: float = 0.20,
        correlation_threshold: float = 0.6,
    ) -> None:
        self._max_positions = max_positions
        self._max_gross_leverage = max_gross_leverage
        self._per_position_cap = per_position_cap
        self._corr_threshold = correlation_threshold

    def allocate(
        self,
        opportunities: list,
        account: Account,
        *,
        correlation_fn: Callable[[str, str], float] | None = None,
    ) -> list[AllocationDecision]:
        """Decide which ranked opportunities to fund given the whole book. Best-first order."""
        equity = account.equity
        decisions: list[AllocationDecision] = []
        if equity <= 0:
            return [AllocationDecision(o, False, 0.0, "non-positive equity") for o in opportunities]

        budget_gross = max(0.0, self._max_gross_leverage * equity - account.gross_exposure)
        open_positions = {k: p for k, p in account.positions.items() if p.quantity != 0}
        slots = max(0, self._max_positions - len(open_positions))
        directions: dict[str, int] = {
            k: (1 if p.quantity > 0 else -1) for k, p in open_positions.items()
        }

        for opp in opportunities:
            key = opp.instrument.key
            action = opp.signal.action
            direction = opp.signal.direction

            # Exits / risk reductions are never gated by portfolio budget.
            if action is Action.CLOSE or direction == 0:
                decisions.append(AllocationDecision(opp, True, 1.0, "exit"))
                continue

            holding = key in open_positions
            if not holding and slots <= 0:
                decisions.append(AllocationDecision(opp, False, 0.0, "max positions reached"))
                continue

            per_cap = min(self._per_position_cap * equity, budget_gross)
            if per_cap <= 1e-9:
                decisions.append(AllocationDecision(opp, False, 0.0, "no gross-exposure budget left"))
                continue

            if correlation_fn is not None and self._correlated_duplicate(key, direction, directions, correlation_fn):
                decisions.append(AllocationDecision(opp, False, 0.0, "correlated to a funded name — diversify"))
                continue

            weight = per_cap / equity
            decisions.append(AllocationDecision(opp, True, weight, "funded"))
            budget_gross -= per_cap
            if not holding:
                slots -= 1
            directions[key] = direction

        return decisions

    def funded(self, decisions: list[AllocationDecision]) -> list:
        return [d.opportunity for d in decisions if d.allocate]

    def _correlated_duplicate(self, key, direction, directions, corr_fn) -> bool:
        for other, other_dir in directions.items():
            if other == key or other_dir != direction:
                continue
            if abs(corr_fn(key, other)) >= self._corr_threshold:
                return True
        return False
