"""Market Data Service (§ Phase C — service A).

Owns the market-data boundary: feed, reconnect/backoff, heartbeat, stale detection, NormalizedQuote,
the quality gate, and ``market_data_health``. Publishes ONLY validated (READY) quote events onto the
bus channel ``md.quotes``. It makes NO trading decisions and performs NO broker execution.

For Phase C acceptance there are NO Massive/IBKR credentials and NO real feed: quotes come from an
explicitly-marked deterministic ``FixtureFeed`` (``source="FIXTURE"``). Swapping in the real
``MassiveProvider`` later changes only the feed, not this boundary.

Crash behaviour: when this process dies it stops refreshing ``market_data_health`` — the rows go
stale, and the Trading Core blocks new affected trade inputs (fail-closed via freshness).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from ..marketdata.quality import QualityStatus, quality_gate
from ..marketdata.quote import NormalizedQuote
from .base import Service

DEFAULT_SYMBOLS = ("AAPL", "MSFT", "SPY")
QUOTES_CHANNEL = "md.quotes"


def fixture_quote(symbol: str, *, seq: int, now: datetime | None = None) -> NormalizedQuote:
    """A deterministic, explicitly-marked TEST quote (no real feed, no credentials).

    Two-sided, positive, fresh, ``source="FIXTURE"`` so ``quality_gate`` classifies it READY. Prices
    are a stable function of the symbol + sequence so acceptance runs are reproducible.
    """
    now = now or datetime.now(timezone.utc)
    base = 100.0 + (sum(ord(c) for c in symbol) % 50)      # deterministic (no hash randomisation)
    bid = round(base + (seq % 5) * 0.01, 4)
    ask = round(bid + 0.02, 4)
    return NormalizedQuote(
        symbol=symbol, con_id=None, asset_class="STK", currency="USD",
        exchange="TEST", primary_exchange="TEST", source="FIXTURE",
        bid=bid, ask=ask, last=round((bid + ask) / 2, 4),
        bid_size=100.0, ask_size=100.0, volume=1000.0,
        timestamp=now, market_data_type="REALTIME",
    )


class MarketDataService(Service):
    name = "market_data"
    health_port = 9101

    def __init__(self) -> None:
        super().__init__()
        raw = os.environ.get("ATP_MD_SYMBOLS") or ",".join(DEFAULT_SYMBOLS)
        self.symbols = [s.strip() for s in raw.split(",") if s.strip()]
        self.interval = float(os.environ.get("ATP_MD_INTERVAL", "1.0"))
        self.source = "FIXTURE"

    async def on_start(self) -> None:
        self._detail = f"feed=FIXTURE symbols={len(self.symbols)}"

    async def main(self) -> None:
        seq = 0
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            ts = now.isoformat()
            for sym in self.symbols:
                q = fixture_quote(sym, seq=seq, now=now)
                status, reason = quality_gate(q, now=now)
                q.status, q.reason = status.value, reason
                # Health is written for EVERY symbol (READY or not) so staleness is detectable.
                try:
                    self.store.upsert_md_health(symbol=q.symbol, source=self.source,
                                                status=status.value, latency_ms=q.latency_ms, ts=ts)
                except Exception:
                    pass
                # Only VALIDATED quotes reach the bus. A Redis outage here loses no authoritative
                # state — consumers fail closed on stale market_data_health.
                if status is QualityStatus.READY:
                    try:
                        await self.bus.publish(QUOTES_CHANNEL, q.as_dict())
                    except Exception:
                        pass
            seq += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


def main() -> None:
    MarketDataService().run()


if __name__ == "__main__":
    main()
