"""Candle aggregation engine (§ Phase G1 — OHLC from Massive realtime trades).

Pure, deterministic aggregation of individual trades into OHLC bars, per (symbol, interval). It only
ever consumes REAL trades (source=MASSIVE, status=READY, realtime) — see ``trade_is_ingestable``. If no
trades arrive, no bars are produced; gaps are NEVER filled with fabricated values.

Bar semantics (§G1):
  * ts     = interval-aligned bar-open time, ISO-8601 UTC (1D aligns to UTC midnight)
  * open   = first trade price in the bar
  * high   = max trade price
  * low    = min trade price
  * close  = last trade price
  * volume = sum of trade sizes

The service persists the forming bar continuously (upsert), so a restart recovers the durable history
from PostgreSQL — nothing is in-memory-only. Out-of-order trades for an already-rolled bucket are
dropped (a sealed bar is never rewritten and never fabricated).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

INTERVAL_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400}
INTERVALS = tuple(INTERVAL_SECONDS)


@dataclass(slots=True)
class OhlcBar:
    symbol: str
    interval: str
    ts: str                 # ISO-8601 UTC, bar-open, interval-aligned
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf"))


def trade_is_ingestable(trade: dict) -> bool:
    """Quality gate: only build candles from real Massive realtime prints. Anything delayed / non-READY
    / non-MASSIVE / malformed is rejected (→ no candle, never fabricated)."""
    return (
        trade.get("source") == "MASSIVE"
        and trade.get("status") == "READY"
        and trade.get("realtime") is True
        and bool(trade.get("symbol"))
        and _is_num(trade.get("price"))
        and _is_num(trade.get("ts"))
    )


class CandleAggregator:
    """Folds trades into forming bars. `add_trade` returns the bars that changed (to be persisted)."""

    def __init__(self, *, intervals=INTERVALS, source: str = "MASSIVE") -> None:
        self.intervals = tuple(intervals)
        self.source = source
        self._forming: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _bucket_start(ts_epoch: float, interval: str) -> int:
        s = INTERVAL_SECONDS[interval]
        return int(ts_epoch // s) * s

    def add_trade(self, symbol: str, price: float, size: float, ts_epoch: float) -> list[OhlcBar]:
        changed: list[OhlcBar] = []
        for interval in self.intervals:
            start = self._bucket_start(ts_epoch, interval)
            key = (symbol, interval)
            f = self._forming.get(key)
            if f is None or start > f["start"]:
                f = {"start": start, "o": price, "h": price, "l": price, "c": price, "v": 0.0}
                self._forming[key] = f
            elif start < f["start"]:
                continue                    # out-of-order past bucket → never rewrite a sealed bar
            f["h"] = max(f["h"], price)
            f["l"] = min(f["l"], price)
            f["c"] = price
            f["v"] += size
            changed.append(self._bar(symbol, interval, f))
        return changed

    def _bar(self, symbol: str, interval: str, f: dict) -> OhlcBar:
        ts = datetime.fromtimestamp(f["start"], tz=timezone.utc).isoformat()
        return OhlcBar(symbol, interval, ts, f["o"], f["h"], f["l"], f["c"], f["v"], self.source)

    def seed(self, symbol: str, interval: str, bar_ts_iso: str, o: float, h: float, l: float, c: float, v: float) -> None:
        """Resume a forming bar from its durable state after a restart — so a restart continues the
        in-progress bar instead of resetting it. A later trade rolls over to a new bar as usual."""
        start = self._bucket_start(datetime.fromisoformat(bar_ts_iso).timestamp(), interval)
        self._forming[(symbol, interval)] = {"start": start, "o": o, "h": h, "l": l, "c": c, "v": v}


class OhlcIngestor:
    """Applies the quality gate, aggregates, and PERSISTS to the durable store. Testable without the
    service framework. If a store write fails (e.g. PostgreSQL unavailable) it raises — the caller fails
    closed and NEVER fabricates a bar."""

    def __init__(self, store, *, aggregator: CandleAggregator | None = None, source: str = "MASSIVE") -> None:
        self.store = store
        self.agg = aggregator or CandleAggregator(source=source)

    def recover(self) -> int:
        """Seed forming bars from the durable store so a restart continues (never resets) each bar.
        Returns the number of (symbol, interval) series resumed."""
        seeded = 0
        for r in self.store.latest_ohlc_bars():
            self.agg.seed(r.symbol, r.interval, r.ts, float(r.open), float(r.high), float(r.low),
                          float(r.close), float(r.volume))
            seeded += 1
        return seeded

    def ingest(self, trade: dict) -> int:
        """Returns the number of bars written for this trade (0 if the trade is rejected by the gate)."""
        if not trade_is_ingestable(trade):
            return 0
        bars = self.agg.add_trade(
            str(trade["symbol"]), float(trade["price"]), float(trade.get("size") or 0.0),
            float(trade["ts"]) / 1000.0,          # Massive timestamps are epoch milliseconds
        )
        for bar in bars:
            self.store.upsert_ohlc_bar(
                symbol=bar.symbol, interval=bar.interval, ts=bar.ts,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume, source=bar.source)
        return len(bars)
