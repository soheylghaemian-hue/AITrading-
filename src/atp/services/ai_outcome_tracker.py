"""AI Outcome Lifecycle Controller (§ Phase G3.2 — READ-ONLY, evaluation only).

A 12th independently-supervised process — the dedicated Outcome Scheduler. Each day it checks the
PENDING predictions in the immutable ai_predictions history and, for every (prediction, horizon) whose
1/3/5/20 TRADING-day window has elapsed and whose forward OHLC price exists, it measures + records the
outcome exactly once (immutable — never overwritten, failed predictions never removed).

It uses ONLY OHLC / market data — never broker prices, simulated prices, or manual values. No market
data → the outcome stays PENDING (never fabricated). This service performs NO trading, NO order/broker/
IBKR interaction, NO strategy activation, and holds no credentials. PostgreSQL down → FAIL CLOSED.
"""
from __future__ import annotations

import asyncio
import os

from ..evaluation.metrics import compute_outcomes_summary
from ..evaluation.tracker import evaluate_outcomes
from .base import Service


class AiOutcomeTrackerService(Service):
    name = "ai_outcome_tracker"
    health_port = 9111

    def __init__(self) -> None:
        super().__init__()
        # Predictions mature over trading days, so a daily check is sufficient (overridable for tests).
        self.poll_interval = float(os.environ.get("ATP_OUTCOME_POLL_S", "86400"))  # daily
        self._recorded = 0
        self._degraded = False

    async def main(self) -> None:
        # Evaluate once at startup, then daily.
        await self._cycle()
        while not self._stop.is_set():
            await self._sleep(self.poll_interval)
            if not self._stop.is_set():
                await self._cycle()

    async def _cycle(self) -> None:
        try:
            self._recorded = await asyncio.to_thread(evaluate_outcomes, self.store)
            summary = await asyncio.to_thread(compute_outcomes_summary, self.store)
            self._degraded = False
            self._detail = (f"newly_recorded={self._recorded} evaluated={summary['evaluated_count']} "
                            f"pending={summary['pending_count']}")
        except Exception:
            self._degraded = True                          # DB error -> fail-closed (record nothing)
            self._detail = "DEGRADED: outcome evaluation failed (fail-closed)"

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    AiOutcomeTrackerService().run()


if __name__ == "__main__":
    main()
