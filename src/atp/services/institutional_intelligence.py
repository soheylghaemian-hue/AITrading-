"""Institutional Intelligence Service (§ Phase R1.3 — READ-ONLY, intelligence input).

A 16th independently-supervised process. Each cycle it records (1) 13F quarter-over-quarter position
changes from the SEC 13F provider's holdings history and (2) SEC Form 4 insider BUY/SELL transactions —
both immutable (ON CONFLICT DO NOTHING → history never rewritten). DATA ONLY.

It performs NO trading, NO copy-trading, NO order/broker/IBKR interaction, holds only the read-only SEC
User-Agent, and never touches the Risk Engine or Execution. No SEC User-Agent → NO DATA (never
fabricated). Institutional filings move slowly (13F quarterly, Form 4 continuous) so it polls daily.
PostgreSQL down → FAIL CLOSED.
"""
from __future__ import annotations

import asyncio
import os

from ..institutional.collector import InstitutionalCollector
from ..institutional.form4 import SecForm4Provider
from ..traders.sec13f import Sec13FTraderProvider
from .base import Service


class InstitutionalIntelligenceService(Service):
    name = "institutional_intelligence"
    health_port = 9115

    def __init__(self) -> None:
        super().__init__()
        self.holdings = Sec13FTraderProvider()             # institutional 13F holdings history
        self.insiders = SecForm4Provider()                 # SEC Form 4 insider transactions
        self.collector = InstitutionalCollector(self.store, self.holdings, self.insiders)
        self.poll_interval = float(os.environ.get("ATP_INSTITUTIONAL_POLL_S", "86400"))  # daily; slow-moving
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.holdings.configured:               # SEC needs a descriptive User-Agent
                self._detail = ("no SEC User-Agent (ATP_SEC_USER_AGENT) configured "
                                "-> NO DATA, never fabricated")
                await self._sleep(self.poll_interval)
                continue
            try:
                res = await asyncio.to_thread(self.collector.collect)
                self._degraded = False
                self._detail = f"changes={res['changes']} insider_txns={res['insiders']}"
            except Exception:
                self._degraded = True                      # provider/DB error → fail-closed (persist nothing)
                self._detail = "DEGRADED: institutional collection failed (fail-closed)"
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    InstitutionalIntelligenceService().run()


if __name__ == "__main__":
    main()
