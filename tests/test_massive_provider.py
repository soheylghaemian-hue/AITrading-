"""Phase 10.2 — Massive realtime provider: auth, subscribe, normalization, gate, reconnect, security.

All tests are hermetic: a scripted fake WebSocket replaces the network. No API key, no live feed,
no IBKR, no orders. Verifies the provider funnels the stream through the EXISTING quality gate."""

import asyncio
import json
import time

import pytest

from atp.marketdata import MarketDataManager, QualityStatus
from atp.marketdata.massive_provider import (
    MASSIVE_SYMBOLS,
    MassiveAuthError,
    MassiveEntitlementError,
    MassiveProvider,
)


class FakeWS:
    """Scripts server frames. `outbox` records what the client sent (to assert auth/subscribe)."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.outbox: list[dict] = []
        self.closed = False

    async def send(self, s):
        self.outbox.append(json.loads(s))

    async def recv(self):
        if not self._frames:
            # idle: emulate a live-but-quiet socket
            await asyncio.sleep(0)
            raise asyncio.CancelledError
        return json.dumps(self._frames.pop(0))

    async def close(self):
        self.closed = True


def _connect_fn(frames):
    async def _c(url):
        _c.ws = FakeWS(frames)
        return _c.ws
    return _c


def _run(coro):
    return asyncio.run(coro)


def _ts() -> int:
    # Fresh AT CALL TIME (not import time) so the READY freshness gate never rejects merely because the
    # rest of the suite took > max_age_s to reach this test. Stale-path tests subtract from this explicitly.
    return int(time.time() * 1000)


# ------------------------------------------------------------------ missing key
def test_missing_api_key_stops():
    p = MassiveProvider(api_key="", connect_fn=_connect_fn([]))
    assert p.has_key is False
    with pytest.raises(MassiveAuthError):
        _run(p.connect())


# ------------------------------------------------------------------ auth
def test_auth_success_and_subscribe():
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_success"},
        {"ev": "status", "status": "success", "message": "subscribed to: Q.AAPL"},
    ]
    cf = _connect_fn(frames)
    p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=cf)
    _run(p.connect())
    actions = [m.get("action") for m in cf.ws.outbox]
    assert actions == ["auth", "subscribe"]
    # subscribe covers quotes+trades+aggregates for the symbol
    params = cf.ws.outbox[1]["params"]
    assert "Q.AAPL" in params and "T.AAPL" in params and "A.AAPL" in params


def test_auth_failed_raises_with_message():
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_failed", "message": "authentication failed"},
    ]
    p = MassiveProvider(api_key="bad", connect_fn=_connect_fn(frames))
    with pytest.raises(MassiveAuthError) as e:
        _run(p.connect())
    assert "authentication failed" in str(e.value)


def test_entitlement_error_is_distinct():
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_failed",
         "message": "Your plan does not include realtime data, please upgrade"},
    ]
    p = MassiveProvider(api_key="k", connect_fn=_connect_fn(frames))
    with pytest.raises(MassiveEntitlementError):
        _run(p.connect())


# ------------------------------------------------------------------ normalization + gate
def test_quote_trade_normalize_to_ready():
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_success"},
        {"ev": "Q", "sym": "AAPL", "bp": 226.10, "bs": 3, "ap": 226.12, "as": 5, "t": _ts()},
        {"ev": "T", "sym": "AAPL", "p": 226.11, "s": 100, "t": _ts()},
        {"ev": "A", "sym": "AAPL", "av": 1_250_000, "c": 226.11, "t": _ts()},
    ]

    async def go():
        p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=_connect_fn(frames))
        await p.connect()
        for _ in range(3):
            await p._read_once()
        return p.quotes()

    quotes = _run(go())
    q = quotes[0]
    assert q.symbol == "AAPL"
    assert q.source == "MASSIVE"
    assert q.market_data_type == "REALTIME"
    assert q.status == QualityStatus.READY.value       # passed the EXISTING gate
    assert q.bid == 226.10 and q.ask == 226.12
    assert q.last == 226.11
    assert q.bid_size == 300.0 and q.ask_size == 500.0  # round lots -> shares
    assert q.volume == 1_250_000
    assert q.latency_ms is not None


def test_missing_fields_stay_null_and_block():
    # only a trade (no bid/ask) -> gate must NOT mark READY, and bid/ask stay None
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_success"},
        {"ev": "T", "sym": "NVDA", "p": 173.0, "s": 100, "t": _ts()},
    ]

    async def go():
        p = MassiveProvider([MASSIVE_SYMBOLS[1]], api_key="k", connect_fn=_connect_fn(frames))
        await p.connect()
        await p._read_once()
        return p.quotes()

    q = _run(go())[0]
    assert q.bid is None and q.ask is None
    assert q.status != QualityStatus.READY.value        # one-sided -> blocked, never fabricated


def test_stale_timestamp_rejected_by_gate():
    old = _ts() - 60_000  # 60s old
    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_success"},
        {"ev": "Q", "sym": "SPY", "bp": 560.0, "bs": 2, "ap": 560.02, "as": 2, "t": old},
    ]

    async def go():
        p = MassiveProvider([MASSIVE_SYMBOLS[2]], api_key="k", connect_fn=_connect_fn(frames), max_age_s=5.0)
        await p.connect()
        await p._read_once()
        return p.quotes()

    q = _run(go())[0]
    assert q.status == QualityStatus.STALE.value


def test_realtime_only_after_a_live_tick():
    # No tick yet -> market_data_type stays None (never falsely REALTIME); source is always MASSIVE.
    p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=_connect_fn([]))
    raw = p.raw_by_symbol()["AAPL"]
    assert raw["market_data_type"] is None
    assert raw["source"] == "MASSIVE"

    frames = [
        {"ev": "status", "status": "connected"},
        {"ev": "status", "status": "auth_success"},
        {"ev": "Q", "sym": "AAPL", "bp": 226.1, "bs": 3, "ap": 226.12, "as": 5, "bx": 11, "ax": 12, "t": _ts()},
    ]

    async def go():
        p2 = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=_connect_fn(frames))
        await p2.connect()
        await p2._read_once()
        return p2.raw_by_symbol()["AAPL"]

    raw2 = _run(go())
    assert raw2["market_data_type"] == "REALTIME"   # only now, because a live tick arrived
    assert raw2["ask_exch"] == 12 and raw2["bid_exch"] == 11


# ------------------------------------------------------------------ reconnect
def test_reconnect_after_drop():
    # first connection auths then drops (recv raises); run() should reconnect and auth again.
    class DropWS(FakeWS):
        async def recv(self):
            if not self._frames:
                raise ConnectionError("socket dropped")
            return json.dumps(self._frames.pop(0))

    attempts = {"n": 0}

    async def cf(url):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return DropWS([{"ev": "status", "status": "connected"},
                           {"ev": "status", "status": "auth_success"}])
        # second attempt: connect, auth, then keep quiet; we stop shortly after
        return DropWS([{"ev": "status", "status": "connected"},
                       {"ev": "status", "status": "auth_success"},
                       {"ev": "Q", "sym": "AAPL", "bp": 1, "bs": 1, "ap": 2, "as": 1, "t": _ts()}])

    async def go():
        p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=cf)
        task = asyncio.create_task(p.run(reconnect=True, backoff_max=0.05))
        await asyncio.sleep(0.3)
        p.stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
        return attempts["n"]

    assert _run(go()) >= 2  # reconnected at least once


def test_bad_key_does_not_reconnect_loop():
    frames = [{"ev": "status", "status": "connected"},
              {"ev": "status", "status": "auth_failed", "message": "bad key"}]

    async def go():
        p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="bad", connect_fn=_connect_fn(frames))
        with pytest.raises(MassiveAuthError):
            await p.run(reconnect=True)

    _run(go())


# ------------------------------------------------------------------ security
def test_key_never_in_repr():
    p = MassiveProvider(api_key="SUPERSECRETKEY", connect_fn=_connect_fn([]))
    assert "SUPERSECRETKEY" not in repr(p)
    assert "key=set" in repr(p)


def test_no_order_methods_on_provider():
    p = MassiveProvider(api_key="k", connect_fn=_connect_fn([]))
    for bad in ("place_order", "placeOrder", "submit", "buy", "sell", "order"):
        assert not hasattr(p, bad)


# ------------------------------------------------------------------ manager reuse
def test_provider_uses_shared_manager_and_gate():
    p = MassiveProvider([MASSIVE_SYMBOLS[0]], api_key="k", connect_fn=_connect_fn([]))
    assert isinstance(p.manager, MarketDataManager)
    # empty book -> DATA_NOT_AVAILABLE (no fabricated price)
    q = p.quotes()[0]
    assert q.status == QualityStatus.DATA_NOT_AVAILABLE.value
