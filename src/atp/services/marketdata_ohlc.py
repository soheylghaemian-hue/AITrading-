"""OHLC Market-Data Service (§ Phase G1 — durable candle aggregation).

A 5th independently-supervised process (isolated from Trading Core / Risk / Broker). It subscribes to
the real Massive trade prints on the bus (``md.trades``), aggregates them into 1m/5m/15m/1h/1D OHLC
bars per symbol via ``CandleAggregator``, and persists each forming bar to PostgreSQL (``ohlc_bars``) —
the authoritative history the Market Intelligence Terminal reads through the Control API.

It NEVER trades, never places orders, never touches IBKR. Quality is strict: only trades with
source=MASSIVE, status=READY, realtime=True become candles (``trade_is_ingestable``). If the feed is
unavailable, no trades arrive → no candles (gaps are never filled). If PostgreSQL is unavailable the
service FAILS CLOSED: it persists nothing (never a fabricated bar) and reports DEGRADED.
"""
from __future__ import annotations

import asyncio

from ..marketdata.ohlc_aggregator import OhlcIngestor
from .base import Service
from .marketdata import TRADES_CHANNEL


class MarketDataOhlcService(Service):
    name = "marketdata_ohlc"
    health_port = 9104

    def __init__(self) -> None:
        super().__init__()
        self._ingestor = OhlcIngestor(self.store)
        self._sub_task: asyncio.Task | None = None
        self._bars_written = 0
        self._degraded = False

    async def on_start(self) -> None:
        try:
            resumed = self._ingestor.recover()      # continue in-progress bars across a restart
            self._detail = f"recovered {resumed} bar series; subscribing md.trades"
        except Exception:
            self._detail = "recover skipped (db unavailable); subscribing md.trades"
        self._sub_task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        # Re-subscribe across bus hiccups; a Redis outage never crashes the process and never loses
        # durable history (already in PostgreSQL). While the bus is down, no candles form.
        while not self._stop.is_set():
            try:
                async for trade in self.bus.subscribe(TRADES_CHANNEL):
                    try:
                        if self._ingestor.ingest(trade):
                            self._bars_written += 1
                            self._degraded = False
                    except Exception:
                        # PostgreSQL unavailable -> fail closed: never fabricate a bar; surface DEGRADED.
                        self._degraded = True
                        self._detail = "db write failed -> fail-closed (no candles persisted)"
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._sleep(1.0)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def main(self) -> None:
        while not self._stop.is_set():
            self._detail = ("DEGRADED: db write failed (fail-closed)" if self._degraded
                            else f"aggregating md.trades bars_written={self._bars_written}")
            await self._sleep(self.heartbeat_interval)

    async def on_stop(self) -> None:
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    MarketDataOhlcService().run()


if __name__ == "__main__":
    main()
