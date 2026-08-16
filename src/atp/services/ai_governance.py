"""AI Decision Governance Service (§ Phase G3.3 — READ-ONLY, evaluation only).

A 13th independently-supervised process — the Governance Engine. Each cycle it computes the
deterministic governance verdict (APPROVED / PARTIAL / CONFLICT / BLOCKED) for every prediction in the
immutable ai_predictions history that doesn't have one yet, and records it exactly once (immutable —
ON CONFLICT DO NOTHING, so a restart never rewrites an old verdict).

It only evaluates decision QUALITY and READINESS. It performs NO trading, NO order/broker/IBKR
interaction, NO strategy activation, holds no credentials, and never touches the Risk Engine or
Execution. PostgreSQL down → FAIL CLOSED (records nothing; systemd restarts it).
"""
from __future__ import annotations

import asyncio
import os

from ..aigov.engine import build_governance_feed, record_governance
from .base import Service


class AiGovernanceService(Service):
    name = "ai_governance"
    health_port = 9112

    def __init__(self) -> None:
        super().__init__()
        # Predictions snapshot ~hourly, so an hourly governance pass is ample (overridable for tests).
        self.poll_interval = float(os.environ.get("ATP_GOVERNANCE_POLL_S", "3600"))
        self._recorded = 0
        self._degraded = False

    async def main(self) -> None:
        await self._cycle()                                 # evaluate at startup, then hourly
        while not self._stop.is_set():
            await self._sleep(self.poll_interval)
            if not self._stop.is_set():
                await self._cycle()

    async def _cycle(self) -> None:
        try:
            self._recorded = await asyncio.to_thread(record_governance, self.store)
            feed = await asyncio.to_thread(build_governance_feed, self.store, 500)
            self._degraded = False
            counts = feed.get("status_counts", {})
            self._detail = (f"newly_recorded={self._recorded} total={feed['count']} "
                            f"approved={counts.get('APPROVED', 0)} partial={counts.get('PARTIAL', 0)} "
                            f"conflict={counts.get('CONFLICT', 0)} blocked={counts.get('BLOCKED', 0)}")
        except Exception:
            self._degraded = True                           # DB error → fail-closed (record nothing)
            self._detail = "DEGRADED: governance evaluation failed (fail-closed)"

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    AiGovernanceService().run()


if __name__ == "__main__":
    main()
