"""Fundamentals Intelligence Service (§ Phase G2.2 — READ-ONLY).

An 8th independently-supervised process (isolated from Trading Core / Risk / Broker / OHLC / News /
Traders). It polls the configured fundamentals provider (Massive/Polygon by default, via the licensed
MASSIVE_API_KEY), persists company profile / financials / valuation to PostgreSQL, and the Control API
serves a deterministic company quality score + strengths/risks. It never trades, never places orders,
never touches IBKR, and holds no broker credentials.

Fundamentals move slowly, so it polls every ~6h. No key / no entitlement / a fetch failure → nothing
persisted → NO DATA (never a fabricated revenue or margin). PostgreSQL unavailable → FAIL CLOSED
(DEGRADED).
"""
from __future__ import annotations

import asyncio
import os

from ..fundamentals.collector import FundamentalsCollector
from ..fundamentals.provider import resolve_provider
from .base import Service

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def fundamentals_symbols() -> list[str]:
    raw = os.environ.get("ATP_FUNDAMENTALS_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)


class FundamentalsIntelligenceService(Service):
    name = "fundamentals_intelligence"
    health_port = 9107

    def __init__(self) -> None:
        super().__init__()
        self.provider = resolve_provider()
        self.collector = FundamentalsCollector(self.store, self.provider)
        self.symbols = fundamentals_symbols()
        self.poll_interval = float(os.environ.get("ATP_FUNDAMENTALS_POLL_S", "21600"))  # 6h; slow-moving
        self._loaded = 0
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.provider.configured:
                self._detail = (f"no fundamentals provider configured (provider={self.provider.name}) "
                                "-> NO DATA, never fabricated")
                await self._sleep(self.poll_interval)
                continue
            loaded = 0
            errors = 0
            for sym in self.symbols:
                try:
                    if await asyncio.to_thread(self.collector.collect, sym):
                        loaded += 1
                except Exception:
                    errors += 1                            # DB/provider error -> fail-closed (persist nothing)
            self._loaded = loaded
            self._degraded = errors > 0
            self._detail = (f"DEGRADED: {errors}/{len(self.symbols)} symbols failed (fail-closed); "
                            f"companies={self._loaded}" if self._degraded
                            else f"provider={self.provider.name}; companies_loaded={self._loaded}")
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    FundamentalsIntelligenceService().run()


if __name__ == "__main__":
    main()
