"""Market Data Service (§ Phase E — real Massive realtime feed).

Production-paper runtime uses the REAL ``MassiveProvider`` (Polygon/Massive realtime stocks WS) for
AAPL / NVDA / SPY. Pipeline: Massive WS → provider → MarketDataManager → NormalizedQuote →
quality_gate → validated event → Redis bus (``md.quotes``) + ``market_data_health`` (PostgreSQL).

Publish a tradable quote event ONLY when: source=MASSIVE, market_data_type=REALTIME, status=READY,
bid/ask valid, timestamp fresh. There is NO delayed / IBKR / mock / synthetic fallback. If Massive is
unavailable the affected symbols are DATA_NOT_AVAILABLE — never fixture. The FIXTURE provider stays
available for TESTS only (``ATP_MD_PROVIDER=fixture``); production never silently falls back to it.

The API key is read from ``MASSIVE_API_KEY`` server-side only and is never logged or exposed via
/health, /ready, the Control API, Redis, or the bus. Bad/missing credentials DEGRADE the service
(AUTH_FAILED / ENTITLEMENT_FAILED / AUTH_MISSING) with a bounded backoff — they never crash-loop it.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from ..marketdata.massive_provider import (
    MASSIVE_SYMBOLS, MassiveAuthError, MassiveEntitlementError, MassiveProvider,
)
from ..marketdata.quality import QualityStatus, quality_gate
from ..marketdata.quote import NormalizedQuote
from ..persistence.state import RedisStateStore
from .base import Service, redis_url

QUOTES_CHANNEL = "md.quotes"
TRADES_CHANNEL = "md.trades"                 # §G1: real per-trade prints for the OHLC candle aggregator
SNAPSHOT_KEY = "md:snapshot"                 # read-model for Control (cache; never authoritative, no secret)
FIXTURE_SYMBOLS = ("AAPL", "MSFT", "SPY")


def fixture_quote(symbol: str, *, seq: int, now: datetime | None = None) -> NormalizedQuote:
    """Deterministic TEST quote (tests only). Two-sided, positive, fresh, source=FIXTURE."""
    now = now or datetime.now(timezone.utc)
    base = 100.0 + (sum(ord(c) for c in symbol) % 50)
    bid = round(base + (seq % 5) * 0.01, 4)
    ask = round(bid + 0.02, 4)
    return NormalizedQuote(
        symbol=symbol, con_id=None, asset_class="STK", currency="USD",
        exchange="TEST", primary_exchange="TEST", source="FIXTURE",
        bid=bid, ask=ask, last=round((bid + ask) / 2, 4),
        bid_size=100.0, ask_size=100.0, volume=1000.0,
        timestamp=now, market_data_type="REALTIME",
    )


def _unavailable_quote(symbol: str, reason: str) -> NormalizedQuote:
    """Honest DATA_NOT_AVAILABLE placeholder when Massive is not streaming. NEVER fixture data."""
    return NormalizedQuote(
        symbol=symbol, con_id=None, asset_class="STK", currency="USD",
        exchange="", primary_exchange="", source="MASSIVE",
        bid=None, ask=None, last=None, bid_size=None, ask_size=None, volume=None,
        timestamp=None, market_data_type=None,
        status=QualityStatus.DATA_NOT_AVAILABLE.value, reason=reason,
    )


class MarketDataService(Service):
    name = "market_data"
    health_port = 9101

    def __init__(self) -> None:
        super().__init__()
        self.kind = os.environ.get("ATP_MD_PROVIDER", "massive").lower()
        self.interval = float(os.environ.get("ATP_MD_INTERVAL", "1.0"))
        self._provider: MassiveProvider | None = None
        self._feed = "INIT"                  # INIT/CONNECTING/STREAMING/AUTH_MISSING/AUTH_FAILED/ENTITLEMENT_FAILED/DISCONNECTED
        self._feed_task: asyncio.Task | None = None
        try:
            self._snap = RedisStateStore(redis_url()) if redis_url() else None
        except Exception:
            self._snap = None
        if self.kind == "massive":
            self.symbols = [s.symbol for s in MASSIVE_SYMBOLS]            # AAPL / NVDA / SPY
        else:
            raw = os.environ.get("ATP_MD_SYMBOLS") or ",".join(FIXTURE_SYMBOLS)
            self.symbols = [s.strip() for s in raw.split(",") if s.strip()]

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def on_start(self) -> None:
        self._detail = f"feed={self.kind.upper()} symbols={len(self.symbols)}"
        if self.kind == "massive":
            self._feed_task = asyncio.create_task(self._massive_feed_loop())

    async def _massive_feed_loop(self) -> None:
        """Own the WS: provider.run() reconnects on network drops with bounded backoff; auth/entitlement
        errors DEGRADE the service on a long backoff — never a tight crash-loop, never a fixture fallback."""
        while not self._stop.is_set():
            provider = MassiveProvider(specs=MASSIVE_SYMBOLS)            # reads MASSIVE_API_KEY from env
            if not provider.has_key:
                self._feed = "AUTH_MISSING"; self._provider = None
                await self._sleep(30); continue
            self._provider = provider
            self._feed = "CONNECTING"
            try:
                self._feed = "STREAMING"
                await provider.run(reconnect=True, backoff_max=30.0)
            except MassiveAuthError:
                self._feed = "AUTH_FAILED"
            except MassiveEntitlementError:
                self._feed = "ENTITLEMENT_FAILED"
            except asyncio.CancelledError:
                raise
            except Exception:
                self._feed = "DISCONNECTED"
            finally:
                try:
                    await provider.close()
                except Exception:
                    pass
                self._provider = None
            if self._stop.is_set():
                break
            await self._sleep(60)                                        # long backoff → no crash-loop on bad creds

    def _current_quotes(self, now: datetime, seq: int) -> list[NormalizedQuote]:
        if self.kind == "fixture":
            out = []
            for sym in self.symbols:
                q = fixture_quote(sym, seq=seq, now=now)
                st, reason = quality_gate(q, now=now)
                q.status, q.reason = st.value, reason
                out.append(q)
            return out
        prov = self._provider
        if prov is not None and self._feed == "STREAMING":
            return prov.quotes(now=now)                                  # normalized + quality-gated
        return [_unavailable_quote(sym, f"massive {self._feed.lower()}") for sym in self.symbols]

    async def main(self) -> None:
        seq = 0
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            ts = now.isoformat()
            quotes = self._current_quotes(now, seq)
            snapshot: dict[str, dict] = {}
            ready = 0
            for q in quotes:
                status = q.status or QualityStatus.DATA_NOT_AVAILABLE.value
                src = q.source or ("MASSIVE" if self.kind == "massive" else "FIXTURE")
                realtime = (q.market_data_type or "").upper() == "REALTIME"
                try:
                    self.store.upsert_md_health(symbol=q.symbol, source=src, status=status,
                                                latency_ms=q.latency_ms, ts=ts)
                except Exception:
                    pass
                tradable = status == "READY" and realtime and q.bid and q.ask and (
                    src == "MASSIVE" or self.kind == "fixture")
                if tradable:
                    ready += 1
                    try:
                        await self.bus.publish(QUOTES_CHANNEL, q.as_dict())
                    except Exception:
                        pass
                snapshot[q.symbol] = {
                    "symbol": q.symbol, "source": src, "status": status, "realtime": realtime,
                    "bid": q.bid, "ask": q.ask, "last": q.last,
                    "bid_size": q.bid_size, "ask_size": q.ask_size, "volume": q.volume,
                    "latency_ms": q.latency_ms,
                    "timestamp": q.timestamp.isoformat() if q.timestamp else None,
                    "updated_at": ts,
                    "error": None if status == "READY" else (q.reason or status),
                }
            if self._snap is not None:
                try:
                    self._snap.set(SNAPSHOT_KEY, {"feed": self._feed, "ts": ts, "symbols": snapshot})
                except Exception:
                    pass
            # §G1: forward real trade prints for the OHLC aggregator — only from the live Massive feed.
            prov = self._provider
            if self.kind == "massive" and self._feed == "STREAMING" and prov is not None:
                for tr in prov.drain_trades():
                    tr["source"], tr["status"], tr["realtime"] = "MASSIVE", "READY", True
                    try:
                        await self.bus.publish(TRADES_CHANNEL, tr)
                    except Exception:
                        pass
            self._detail = f"feed={self._feed} symbols={len(quotes)} ready={ready}"
            seq += 1
            await self._sleep(self.interval)

    async def on_stop(self) -> None:
        prov = self._provider
        if prov is not None:
            try:
                prov.stop()
            except Exception:
                pass
        if self._feed_task is not None:
            self._feed_task.cancel()
            try:
                await self._feed_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    MarketDataService().run()


if __name__ == "__main__":
    main()
