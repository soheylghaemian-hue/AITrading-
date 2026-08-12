"""Market-data feeds (§17, §24 Phase 3/14).

The live desk needs a stream of quotes and bars. `MarketFeed` is the seam: the runner reads
events from it and never knows the source. Two implementations:

* `ReplayFeed` — deterministic, offline. Replays a list of bars, synthesizing a bid/ask quote
  around each (as the backtester does), optionally pacing in real time. This is what drives
  paper trading and the offline tests.
* `IBKRMarketFeed` — live, backed by `ib_insync` real-time bars/tickers (lazy-imported). Its
  event plumbing runs only against a real gateway, but the IB→atp mapping is factored into the
  pure `bar_from_rt` / `quote_from_ticker` helpers, which ARE unit-tested (same honesty
  boundary as the broker adapter, ADR-6/ADR-10).
"""

from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from ..core.events import Bar, Instrument, QuoteEvent

# A market event is either a top-of-book quote or a completed bar.
MarketEvent = QuoteEvent | Bar


class MarketFeed(abc.ABC):
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[MarketEvent]:
        """Yield MarketEvents until the stream ends (finite for replay, open-ended for live)."""
        ...


class ReplayFeed(MarketFeed):
    """Replays bars offline, emitting a synthesized quote then the bar for each (§13/§24)."""

    def __init__(self, bars: list[Bar], *, spread_bps: float = 2.0, delay: float = 0.0) -> None:
        self._bars = bars
        self._spread_bps = spread_bps
        self._delay = delay

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def stream(self) -> AsyncIterator[MarketEvent]:
        for bar in self._bars:
            half = bar.close * (self._spread_bps / 2 / 1e4)
            yield QuoteEvent(bar.instrument, bar.close - half, bar.close + half, bar.ts)
            yield bar
            if self._delay:
                await asyncio.sleep(self._delay)


# --- pure IB → atp mappers (unit-tested without ib_insync) -------------------
def bar_from_rt(rt: Any, instrument: Instrument) -> Bar:
    """Map an ib_insync RealTimeBar (or compatible) to an atp Bar."""
    ts = getattr(rt, "time", None)
    if not isinstance(ts, datetime):
        ts = datetime.now(timezone.utc)
    return Bar(
        instrument=instrument,
        open=float(getattr(rt, "open_", getattr(rt, "open", 0.0))),
        high=float(rt.high),
        low=float(rt.low),
        close=float(rt.close),
        volume=float(getattr(rt, "volume", 0.0) or 0.0),
        ts=ts,
    )


def quote_from_ticker(ticker: Any, instrument: Instrument) -> QuoteEvent | None:
    """Map an ib_insync Ticker to an atp QuoteEvent; None if bid/ask aren't both present."""
    bid = getattr(ticker, "bid", None)
    ask = getattr(ticker, "ask", None)
    if bid is None or ask is None or bid != bid or ask != ask:  # None or NaN
        return None
    ts = getattr(ticker, "time", None)
    if not isinstance(ts, datetime):
        ts = datetime.now(timezone.utc)
    return QuoteEvent(instrument, float(bid), float(ask), ts)


class IBKRMarketFeed(MarketFeed):
    """Live real-time bars/quotes from IB Gateway (§17). Live-only: the event bridge runs
    against a real connection; the IB→atp mapping is the tested part (see helpers above)."""

    def __init__(self, instruments: list[Instrument], *, ib: Any = None, factory: Any = None,
                 bar_seconds: int = 5, what_to_show: str = "MIDPOINT") -> None:
        self._instruments = instruments
        self._ib = ib
        self._factory = factory
        self._bar_seconds = bar_seconds
        self._what = what_to_show
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue()

    async def connect(self) -> None:
        if self._ib is None:
            import ib_insync  # noqa: PLC0415 — lazy; only needed for a live connection

            self._ib = ib_insync.IB()
            await self._ib.connectAsync()
        if self._factory is None:
            from ..brokers.ibkr import IBFactory  # noqa: PLC0415

            self._factory = IBFactory()
        for inst in self._instruments:
            contract = self._factory.contract(inst)
            rt_bars = self._ib.reqRealTimeBars(contract, self._bar_seconds, self._what, False)
            rt_bars.updateEvent += self._make_bar_handler(inst)
            ticker = self._ib.reqMktData(contract)
            ticker.updateEvent += self._make_quote_handler(inst)

    def _make_bar_handler(self, inst: Instrument):
        def handler(bars, has_new_bar):  # ib_insync signature
            if has_new_bar and bars:
                self._queue.put_nowait(bar_from_rt(bars[-1], inst))
        return handler

    def _make_quote_handler(self, inst: Instrument):
        def handler(ticker):
            q = quote_from_ticker(ticker, inst)
            if q is not None:
                self._queue.put_nowait(q)
        return handler

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    async def stream(self) -> AsyncIterator[MarketEvent]:
        while True:
            yield await self._queue.get()
