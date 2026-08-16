"""AI Evaluation Service (§ Phase G3.1 — READ-ONLY, honest performance tracking).

An 11th independently-supervised process. Each cycle it (1) snapshots the current AI consensus for each
watched symbol into the IMMUTABLE ai_predictions history, and (2) measures + records any due outcomes
from real OHLC. It performs NO external fetch, NO trading, NO order/broker/IBKR interaction, and holds
no credentials. History is never rewritten — old scores never change, failed predictions never removed.

No prices → no outcomes (NO DATA), never fabricated. PostgreSQL unavailable → FAIL CLOSED (DEGRADED).
"""
from __future__ import annotations

import asyncio
import os

from ..consensus.engine import build_ai_consensus
from ..evaluation.tracker import snapshot_prediction
from .base import Service

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def evaluation_symbols() -> list[str]:
    raw = os.environ.get("ATP_EVALUATION_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)


class AiEvaluationService(Service):
    name = "ai_evaluation"
    health_port = 9110

    def __init__(self) -> None:
        super().__init__()
        self.symbols = evaluation_symbols()
        self.poll_interval = float(os.environ.get("ATP_EVALUATION_POLL_S", "900"))  # 15 min
        self._snapshots = 0
        self._degraded = False

    async def main(self) -> None:
        # Snapshots the current AI consensus into the immutable prediction history. Outcome measurement
        # is owned by the dedicated Outcome Lifecycle Controller (atp-ai-outcome-tracker, § G3.2).
        while not self._stop.is_set():
            snaps = 0
            errors = 0
            for sym in self.symbols:
                try:
                    a = await asyncio.to_thread(build_ai_consensus, self.store, sym)
                    if await asyncio.to_thread(snapshot_prediction, self.store, sym, a):
                        snaps += 1
                except Exception:
                    errors += 1
            self._snapshots = snaps
            self._degraded = errors > 0
            self._detail = (f"DEGRADED: {errors} error(s) (fail-closed); snapshots={snaps}"
                            if self._degraded else f"snapshots={snaps}")
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    AiEvaluationService().run()


if __name__ == "__main__":
    main()
