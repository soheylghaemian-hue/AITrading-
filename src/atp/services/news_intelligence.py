"""News Intelligence Service (§ Phase G2.1 — READ-ONLY).

A 6th independently-supervised process (isolated from Trading Core / Risk / Broker / OHLC). It polls
the real news provider (Massive/Polygon) for each watched symbol, runs deterministic sentiment/impact
analysis on the real text, and upserts the results into PostgreSQL (news_items) — the store the Market
Intelligence Terminal reads through the Control API.

It NEVER trades, never places orders, never touches IBKR. It runs INDEPENDENT of market hours (news
flows around the clock). If no MASSIVE_API_KEY is set, or the provider returns nothing, it persists
nothing (NO DATA) — never a fabricated headline. If PostgreSQL is unavailable it FAILS CLOSED and
reports DEGRADED.
"""
from __future__ import annotations

import asyncio
import os

from ..news.collector import NewsCollector
from ..news.provider import PolygonNewsProvider
from .base import Service

DEFAULT_NEWS_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def news_symbols() -> list[str]:
    raw = os.environ.get("ATP_NEWS_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_NEWS_SYMBOLS)


class NewsIntelligenceService(Service):
    name = "news_intelligence"
    health_port = 9105

    def __init__(self) -> None:
        super().__init__()
        self.provider = PolygonNewsProvider()
        self.collector = NewsCollector(self.store, self.provider)
        self.symbols = news_symbols()
        self.poll_interval = float(os.environ.get("ATP_NEWS_POLL_S", "300"))  # 5 min; not tick-critical
        self._ingested = 0
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.provider.configured:
                self._detail = "no MASSIVE_API_KEY -> news disabled (NO DATA, never fabricated)"
                await self._sleep(self.poll_interval)
                continue
            got = 0
            errors = 0
            for sym in self.symbols:
                try:
                    # provider.fetch does blocking HTTP + store writes -> off the event loop.
                    got += await asyncio.to_thread(self.collector.collect, sym, 20)
                except Exception:
                    errors += 1                            # DB/provider error -> fail-closed (persist nothing)
            self._ingested += got
            self._degraded = errors > 0
            self._detail = (f"DEGRADED: {errors}/{len(self.symbols)} symbols failed (fail-closed); "
                            f"news_items={self._ingested}" if self._degraded
                            else f"collecting {len(self.symbols)} symbols; news_items={self._ingested}")
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    NewsIntelligenceService().run()


if __name__ == "__main__":
    main()
