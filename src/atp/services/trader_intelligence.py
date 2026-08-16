"""Trader Intelligence Service (§ Phase G2.5 — READ-ONLY).

A 7th independently-supervised process (isolated from Trading Core / Risk / Broker / OHLC / News). It
polls the configured, LICENSED trader provider, persists traders / performance / positions to
PostgreSQL, and the Control API serves the quality-weighted consensus. It never trades, never places
orders, never touches IBKR, and holds no broker credentials.

No provider is configured by default (ATP_TRADER_PROVIDER unset → NullTraderProvider), so it collects
nothing and everything shows NO DATA — never a fabricated trader. If PostgreSQL is unavailable it
FAILS CLOSED and reports DEGRADED.
"""
from __future__ import annotations

import asyncio
import os

from ..traders.collector import TraderCollector
from ..traders.provider import resolve_provider
from .base import Service


class TraderIntelligenceService(Service):
    name = "trader_intelligence"
    health_port = 9106

    def __init__(self) -> None:
        super().__init__()
        self.provider = resolve_provider()
        self.collector = TraderCollector(self.store, self.provider)
        self.poll_interval = float(os.environ.get("ATP_TRADER_POLL_S", "900"))  # 15 min; slow-moving data
        self._ingested = 0
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.provider.configured:
                self._detail = (f"no licensed trader provider configured (provider={self.provider.name}) "
                                "-> NO DATA, never fabricated")
                await self._sleep(self.poll_interval)
                continue
            try:
                got = await asyncio.to_thread(self.collector.collect)
                self._ingested = got
                self._degraded = False
                self._detail = f"provider={self.provider.name}; traders={self._ingested}"
            except Exception:
                self._degraded = True                      # DB/provider error -> fail-closed (persist nothing)
                self._detail = "DEGRADED: collect failed (fail-closed)"
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    TraderIntelligenceService().run()


if __name__ == "__main__":
    main()
