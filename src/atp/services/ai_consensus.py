"""AI Consensus Service (§ Phase G3 — READ-ONLY orchestration).

A 10th independently-supervised process. It periodically recomputes the AI market assessment for each
watched symbol from the OTHER intelligence layers (all already in PostgreSQL) and persists a snapshot
to ai_assessments / ai_assessment_components (history/audit). It performs NO external fetch, NO trading,
NO order/broker/IBKR interaction, and holds no credentials. The Control API serves a FRESH computation
(so the view is never stale); this service only records history.

Missing intelligence → the assessment is PARTIAL / NO DATA, never fabricated. PostgreSQL unavailable →
FAIL CLOSED (DEGRADED).
"""
from __future__ import annotations

import asyncio
import os

from ..consensus.engine import build_ai_consensus, persist_ai_consensus
from .base import Service

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def consensus_symbols() -> list[str]:
    raw = os.environ.get("ATP_CONSENSUS_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)


class AiConsensusService(Service):
    name = "ai_consensus"
    health_port = 9109

    def __init__(self) -> None:
        super().__init__()
        self.symbols = consensus_symbols()
        self.poll_interval = float(os.environ.get("ATP_CONSENSUS_POLL_S", "300"))  # 5 min
        self._assessed = 0
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            assessed = 0
            errors = 0
            for sym in self.symbols:
                try:
                    assessment = await asyncio.to_thread(build_ai_consensus, self.store, sym)
                    if assessment["status"] != "NO DATA":
                        await asyncio.to_thread(persist_ai_consensus, self.store, assessment)
                        assessed += 1
                except Exception:
                    errors += 1                            # DB error -> fail-closed (persist nothing)
            self._assessed = assessed
            self._degraded = errors > 0
            self._detail = (f"DEGRADED: {errors}/{len(self.symbols)} failed (fail-closed); "
                            f"assessed={self._assessed}" if self._degraded
                            else f"assessments={self._assessed}/{len(self.symbols)}")
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    AiConsensusService().run()


if __name__ == "__main__":
    main()
