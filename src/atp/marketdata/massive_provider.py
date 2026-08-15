"""Massive (Polygon.io, rebranded 2026) realtime stocks WebSocket provider (§ Phase 10.2).

Opens a persistent WebSocket to the Massive realtime stocks feed, authenticates with MASSIVE_API_KEY
(server-side only — never logged, never exposed), subscribes to quotes/trades/second-aggregates for a
small symbol set, and turns the live stream into `NormalizedQuote`s through the EXISTING
`MarketDataManager` + `quality_gate`. Nothing here bypasses the data-quality gate, fabricates prices,
converts delayed→realtime, or places any order.

Protocol (Massive == Polygon, single api.polygon.io surface):
  * endpoint : wss://socket.polygon.io/stocks   (realtime; delayed lives on delayed.polygon.io —
               we NEVER connect there for the autonomous pipeline)
  * connect  -> [{"ev":"status","status":"connected"}]
  * auth     -> send {"action":"auth","params":"<KEY>"} -> {"ev":"status","status":"auth_success"|"auth_failed"}
  * subscribe-> send {"action":"subscribe","params":"Q.AAPL,T.AAPL,A.AAPL,..."}
  * data     -> JSON arrays of events: Q (quote) bp/bs/ap/as/t(ms); T (trade) p/s/t(ms); A (agg) av/c/t
Docs: https://massive.com/docs/websocket/stocks/quotes  /trades
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .manager import MarketDataManager
from .quote import NormalizedQuote
from .universe import InstrumentSpec

REALTIME_URL = "wss://socket.polygon.io/stocks"
SOURCE = "MASSIVE"

# US equities routed to Massive for the realtime test (§ Phase 10.2). IBKR is NOT used for these.
MASSIVE_SYMBOLS: list[InstrumentSpec] = [
    InstrumentSpec("AAPL", "USA", "SMART", "NASDAQ", "USD", label="NASDAQ"),
    InstrumentSpec("NVDA", "USA", "SMART", "NASDAQ", "USD", label="NASDAQ"),
    InstrumentSpec("SPY", "USA", "SMART", "ARCA", "USD", label="ARCA"),
]


class MassiveError(RuntimeError):
    """Base for provider errors (auth/entitlement/connection)."""


class MassiveAuthError(MassiveError):
    """Authentication failed — bad/missing key. Carries the server's exact message."""


class MassiveEntitlementError(MassiveError):
    """Authenticated, but the plan does not grant realtime for the requested channel/symbol."""


@dataclass
class _Book:
    """Latest realtime state for one symbol, assembled from Q/T/A events."""
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None
    bid_exch: int | None = None       # bid venue id (Polygon/Massive exchange code)
    ask_exch: int | None = None       # ask venue id
    ts_ms: int | None = None          # newest source (SIP) timestamp seen, ms
    recv_ms: float | None = None      # local wall-clock when we received it, ms
    latency_ms: float | None = None   # recv_ms - ts_ms (one-way, best-effort)
    events: int = 0


def _tls_context():
    """A properly-verified TLS context (certifi CA bundle). We NEVER disable verification."""
    import ssl  # noqa: PLC0415
    try:
        import certifi  # noqa: PLC0415
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def _default_connect(url: str):
    """Open a real WebSocket. Imported lazily so the core/tests never require the `websockets` lib."""
    import websockets  # noqa: PLC0415
    ssl_ctx = _tls_context() if url.startswith("wss://") else None
    return await websockets.connect(url, ssl=ssl_ctx, ping_interval=20, ping_timeout=20, max_queue=1024)


class MassiveProvider:
    """Persistent Massive realtime stocks WS client feeding the existing MarketDataManager.

    Testable: inject `connect_fn` returning an object with async `send(str)`, async `recv() -> str`
    and async `close()` (the websockets client already satisfies this)."""

    def __init__(self, specs: list[InstrumentSpec] | None = None, *,
                 api_key: str | None = None, url: str | None = None,
                 manager: MarketDataManager | None = None,
                 connect_fn=_default_connect, max_age_s: float = 5.0):
        self.specs = list(specs if specs is not None else MASSIVE_SYMBOLS)
        self._symbols = [s.symbol for s in self.specs]
        # key resolved server-side from env; NEVER stored in logs / repr / dashboard.
        self._api_key = api_key if api_key is not None else os.environ.get("MASSIVE_API_KEY")
        self._url = url or os.environ.get("MASSIVE_WS_URL") or REALTIME_URL
        self.manager = manager or MarketDataManager(self.specs, max_age_s=max_age_s)
        self._connect_fn = connect_fn
        self._books: dict[str, _Book] = {s: _Book() for s in self._symbols}
        self._ws = None
        self._authed = False
        self._sub_ok = False
        self._stop = asyncio.Event()
        # session stats (for the dry-run report) — no PII, no key
        self.total_events = 0
        self.reconnects = 0
        self._lat_sum = 0.0
        self._lat_n = 0
        self._lat_max = 0.0
        self._trades: list[dict] = []          # §G1: per-trade prints buffered for the OHLC aggregator

    # never leak the key through repr/str
    def __repr__(self) -> str:
        return f"MassiveProvider(symbols={self._symbols}, url={self._url}, key={'set' if self._api_key else 'MISSING'})"

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    # -- connection lifecycle --------------------------------------------------
    async def connect(self) -> None:
        """Open WS, authenticate, subscribe. Raises MassiveAuthError / MassiveEntitlementError with
        the exact server message on failure. Never falls back to delayed or IBKR."""
        if not self._api_key:
            raise MassiveAuthError("MASSIVE_API_KEY missing")
        self._ws = await self._connect_fn(self._url)
        await self._await_status("connected", stage="connect")
        await self._send({"action": "auth", "params": self._api_key})
        await self._await_auth()
        params = ",".join(f"{ch}.{s}" for s in self._symbols for ch in ("Q", "T", "A"))
        await self._send({"action": "subscribe", "params": params})
        self._sub_ok = True

    async def _send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    @staticmethod
    def _events(raw: str) -> list[dict]:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]

    async def _await_status(self, want: str, *, stage: str) -> None:
        for _ in range(10):
            for ev in self._events(await self._ws.recv()):
                if ev.get("ev") == "status" and ev.get("status") == want:
                    return
        raise MassiveError(f"no '{want}' status during {stage}")

    async def _await_auth(self) -> None:
        for _ in range(10):
            for ev in self._events(await self._ws.recv()):
                if ev.get("ev") != "status":
                    continue
                st = ev.get("status")
                msg = str(ev.get("message", ""))
                if st == "auth_success":
                    self._authed = True
                    return
                if st in ("auth_failed", "auth_timeout", "error"):
                    # entitlement problems ("not authorized"/"upgrade"/"realtime") vs bad key
                    low = msg.lower()
                    if any(w in low for w in ("entitle", "not authorized", "upgrade", "realtime", "plan", "permission")):
                        raise MassiveEntitlementError(msg or st)
                    raise MassiveAuthError(msg or st)
        raise MassiveAuthError("no auth response from Massive")

    # -- streaming -------------------------------------------------------------
    def _apply(self, ev: dict) -> None:
        sym = ev.get("sym")
        b = self._books.get(sym)
        if b is None:
            return
        kind = ev.get("ev")
        t = ev.get("t")
        now_ms = time.time() * 1000.0
        if kind == "Q":
            bp, ap = ev.get("bp"), ev.get("ap")
            if bp is not None:
                b.bid = float(bp)
            if ap is not None:
                b.ask = float(ap)
            if ev.get("bs") is not None:
                b.bid_size = float(ev["bs"]) * 100.0     # round lots -> shares
            if ev.get("as") is not None:
                b.ask_size = float(ev["as"]) * 100.0
            if ev.get("bx") is not None:
                b.bid_exch = int(ev["bx"])
            if ev.get("ax") is not None:
                b.ask_exch = int(ev["ax"])
        elif kind == "T":
            if ev.get("p") is not None:
                b.last = float(ev["p"])
                if isinstance(t, (int, float)):        # §G1: capture the real print for candle aggregation
                    self._trades.append({"symbol": sym, "price": float(ev["p"]),
                                         "size": float(ev.get("s") or 0.0), "ts": int(t)})
                    if len(self._trades) > 20000:      # bound memory; service drains every ~1s
                        del self._trades[:10000]
        elif kind == "A":
            if ev.get("av") is not None:
                b.volume = float(ev["av"])               # accumulated daily volume (honest)
            if b.last is None and ev.get("c") is not None:
                b.last = float(ev["c"])
        else:
            return
        if isinstance(t, (int, float)):
            b.ts_ms = int(t)
            b.recv_ms = now_ms
            b.latency_ms = max(0.0, now_ms - float(t))
            self._lat_sum += b.latency_ms
            self._lat_n += 1
            self._lat_max = max(self._lat_max, b.latency_ms)
        b.events += 1
        self.total_events += 1

    async def _read_once(self) -> None:
        for ev in self._events(await self._ws.recv()):
            if ev.get("ev") == "status":
                continue
            self._apply(ev)

    async def run(self, *, reconnect: bool = True, backoff_max: float = 30.0) -> None:
        """Stream until stop() — reconnecting with capped backoff on drops. Never trades."""
        delay = 1.0
        while not self._stop.is_set():
            try:
                if self._ws is None:
                    await self.connect()
                    delay = 1.0
                while not self._stop.is_set():
                    await self._read_once()
            except MassiveAuthError:
                raise                                     # do not silently retry a bad key
            except (MassiveEntitlementError,):
                raise
            except Exception:
                await self._safe_close()
                if not reconnect or self._stop.is_set():
                    if not reconnect:
                        raise
                    return
                self.reconnects += 1
                await asyncio.sleep(min(delay, backoff_max))
                delay = min(backoff_max, delay * 2)

    async def drain(self, seconds: float) -> None:
        """Read for a bounded time (used by the one-shot validation)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                await asyncio.wait_for(self._read_once(), timeout=max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                break

    def drain_trades(self) -> list[dict]:
        """§G1: return and clear the trade prints buffered since the last call (the OHLC service drains
        these to aggregate candles). Non-blocking; never touches the quote path."""
        out, self._trades = self._trades, []
        return out

    def stop(self) -> None:
        self._stop.set()

    async def _safe_close(self) -> None:
        ws, self._ws = self._ws, None
        self._authed = self._sub_ok = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None and self._sub_ok:
            try:
                params = ",".join(f"{ch}.{s}" for s in self._symbols for ch in ("Q", "T", "A"))
                await self._send({"action": "unsubscribe", "params": params})
            except Exception:
                pass
        await self._safe_close()

    # -- normalized output (through the EXISTING gate) -------------------------
    def raw_by_symbol(self) -> dict[str, dict]:
        out = {}
        for sym, b in self._books.items():
            ts = (datetime.fromtimestamp(b.ts_ms / 1000.0, tz=timezone.utc) if b.ts_ms else None)
            recv = (datetime.fromtimestamp(b.recv_ms / 1000.0, tz=timezone.utc) if b.recv_ms else None)
            out[sym] = {
                "source": SOURCE,
                "market_data_type": "REALTIME" if b.events else None,   # only REALTIME once a live tick arrived
                "bid": b.bid, "ask": b.ask, "last": b.last,
                "bid_size": b.bid_size, "ask_size": b.ask_size,
                "volume": b.volume,
                "bid_exch": b.bid_exch, "ask_exch": b.ask_exch,
                "timestamp": ts,
                "receive_timestamp": recv,
                "latency_ms": b.latency_ms,
                "events": b.events,
            }
        return out

    def quotes(self, *, now: datetime | None = None) -> list[NormalizedQuote]:
        """Normalize + quality-gate the latest realtime state. READY only when the gate passes."""
        return self.manager.classify(self.raw_by_symbol(), specs=self.specs, now=now)

    def latency_ms(self, symbol: str) -> float | None:
        b = self._books.get(symbol)
        return b.latency_ms if b else None

    def stats(self) -> dict:
        """Session data-quality stats for the dry-run report (no key, no PII)."""
        return {
            "total_events": self.total_events,
            "reconnects": self.reconnects,
            "avg_latency_ms": (self._lat_sum / self._lat_n) if self._lat_n else None,
            "max_latency_ms": self._lat_max if self._lat_n else None,
            "ready_symbols": sum(1 for q in self.quotes() if q.status == "READY"),
        }

    # 5-state market_data rows (the shape the dashboard snapshot + autonomous engine consume).
    # READY -> DATA_AVAILABLE + REALTIME; everything else stays honestly unavailable/stale — we NEVER
    # relabel delayed/absent data as realtime, and there is NO IBKR fallback here.
    _STATUS5 = {
        "READY": "DATA_AVAILABLE", "STALE": "STALE", "DELAYED": "DELAYED",
        "DATA_NOT_AVAILABLE": "DATA_NOT_AVAILABLE",
    }

    def market_rows(self, *, now: datetime | None = None) -> list[dict]:
        quotes = self.quotes(now=now)
        raw = self.raw_by_symbol()
        rows = []
        for q in quotes:
            r = raw.get(q.symbol, {})
            ready = q.status == "READY"
            rows.append({
                "symbol": q.symbol,
                "asset_class": q.asset_class,
                "exchange": q.primary_exchange,
                "status": self._STATUS5.get(q.status, "DATA_NOT_AVAILABLE"),
                "market_data_type": "REALTIME" if ready else None,   # only when a live tick arrived
                "bid": q.bid, "ask": q.ask, "last": q.last,
                "bid_size": q.bid_size, "ask_size": q.ask_size, "volume": q.volume,
                "timestamp": q.timestamp.isoformat() if q.timestamp else None,
                "receive_timestamp": r.get("receive_timestamp").isoformat() if r.get("receive_timestamp") else None,
                "latency_ms": q.latency_ms,
                "source": SOURCE,
                "error_code": None, "error_message": None,
                "reason": q.reason,
            })
        return rows
