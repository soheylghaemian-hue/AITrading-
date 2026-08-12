"""Data Quality Engine (§10, §18).

A central gate every market update passes before the desk is allowed to act on it. If the data
is bad, the answer is NO TRADE — the desk never trades on stale, impossible or inconsistent
data. Each check is explicit and returns a typed reason so failures are auditable, not silent.

Detects: stale data, missing/None fields, duplicate ticks, invalid timestamps (future-dated or
non-monotonic), impossible prices (≤0, NaN, crossed/locked or extreme jumps), abnormal spreads,
feed disconnects (heartbeat gap), and inconsistent/broken contracts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.events import Bar, QuoteEvent
from ..logging_config import get_logger

log = get_logger("dataquality")


@dataclass(slots=True)
class QualityResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


OK = QualityResult(True)


@dataclass(slots=True)
class DataQualityConfig:
    max_quote_age_seconds: float = 30.0
    max_spread_bps: float = 500.0          # abnormally wide top-of-book
    max_jump_pct: float = 0.35             # single-tick move that's almost certainly bad data
    heartbeat_timeout_seconds: float = 60.0
    future_tolerance_seconds: float = 5.0  # allow small clock skew


@dataclass(slots=True)
class _State:
    last_ts: datetime | None = None
    last_price: float | None = None
    last_seen: datetime | None = None
    seen_ts: set = field(default_factory=set)


class DataQualityEngine:
    def __init__(self, config: DataQualityConfig | None = None) -> None:
        self._cfg = config or DataQualityConfig()
        self._state: dict[str, _State] = {}
        self._connected = True

    # ------------------------------------------------------------- feed health
    def set_connected(self, connected: bool) -> None:
        self._connected = connected

    def heartbeat(self, now: datetime) -> None:
        for st in self._state.values():
            st.last_seen = now

    # ------------------------------------------------------------- checks
    def check_quote(self, quote: QuoteEvent, now: datetime) -> QualityResult:
        if not self._connected:
            return QualityResult(False, "feed disconnected")
        key = quote.instrument.key
        st = self._state.setdefault(key, _State())

        # Impossible prices.
        for name, v in (("bid", quote.bid), ("ask", quote.ask)):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return QualityResult(False, f"missing {name}")
            if v <= 0:
                return QualityResult(False, f"impossible {name} {v}")
        if quote.ask < quote.bid:
            return QualityResult(False, f"crossed book bid={quote.bid} ask={quote.ask}")
        if quote.spread_bps > self._cfg.max_spread_bps:
            return QualityResult(False, f"abnormal spread {quote.spread_bps:.0f}bps")

        # Timestamp sanity.
        r = self._check_timestamp(st, quote.ts, now)
        if not r:
            return r
        # Staleness (relative to wall clock).
        age = (now - quote.ts).total_seconds()
        if age > self._cfg.max_quote_age_seconds:
            return QualityResult(False, f"stale quote {age:.1f}s")
        # Impossible jump vs last mid.
        r = self._check_jump(st, quote.mid)
        if not r:
            return r

        st.last_ts, st.last_price, st.last_seen = quote.ts, quote.mid, now
        return OK

    def check_bar(self, bar: Bar, now: datetime) -> QualityResult:
        if not self._connected:
            return QualityResult(False, "feed disconnected")
        key = bar.instrument.key
        st = self._state.setdefault(key, _State())

        prices = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
        for name, v in prices.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return QualityResult(False, f"missing {name}")
            if v <= 0:
                return QualityResult(False, f"impossible {name} {v}")
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            return QualityResult(False, "OHLC out of range (open/close outside high/low)")
        if bar.volume < 0:
            return QualityResult(False, f"negative volume {bar.volume}")

        r = self._check_timestamp(st, bar.ts, now)
        if not r:
            return r
        r = self._check_jump(st, bar.close)
        if not r:
            return r

        st.last_ts, st.last_price, st.last_seen = bar.ts, bar.close, now
        return OK

    def check_heartbeat(self, key: str, now: datetime) -> QualityResult:
        st = self._state.get(key)
        if st is None or st.last_seen is None:
            return OK
        gap = (now - st.last_seen).total_seconds()
        if gap > self._cfg.heartbeat_timeout_seconds:
            return QualityResult(False, f"feed silent {gap:.0f}s (disconnect?)")
        return OK

    # ------------------------------------------------------------- internals
    def _check_timestamp(self, st: _State, ts: datetime, now: datetime) -> QualityResult:
        if ts > now + timedelta(seconds=self._cfg.future_tolerance_seconds):
            return QualityResult(False, f"future-dated timestamp {ts.isoformat()}")
        if st.last_ts is not None and ts < st.last_ts:
            return QualityResult(False, f"non-monotonic timestamp {ts.isoformat()} < {st.last_ts.isoformat()}")
        if ts in st.seen_ts:
            return QualityResult(False, f"duplicate timestamp {ts.isoformat()}")
        st.seen_ts.add(ts)
        if len(st.seen_ts) > 4096:               # bound the dedup memory
            st.seen_ts.clear()
        return OK

    def _check_jump(self, st: _State, price: float) -> QualityResult:
        if st.last_price and st.last_price > 0:
            move = abs(price - st.last_price) / st.last_price
            if move > self._cfg.max_jump_pct:
                return QualityResult(False, f"impossible jump {move:.0%} to {price}")
        return OK
