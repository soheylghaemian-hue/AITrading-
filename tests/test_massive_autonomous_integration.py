"""Phase 10.4 — end-to-end: Massive REALTIME → NormalizedQuote → quality_gate → Autonomous input.

Proves the autonomous engine consumes the REAL (scripted-in-test) Massive realtime feed while
staying DISABLED, and that NO order of any kind is placed. Hermetic: a scripted fake WebSocket
stands in for the network; no key, no IBKR, no execution."""

import asyncio
import json
import time

from atp.autonomous import PaperAutonomousEngine
from atp.autonomous.engine import AutonomousStatus
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.marketdata.massive_provider import MASSIVE_SYMBOLS, MassiveProvider
from atp.policy import TradingPolicy
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy import BreakoutStrategy, MeanReversionStrategy, MomentumStrategy

TS = int(time.time() * 1000)


class FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.outbox = []

    async def send(self, s):
        self.outbox.append(json.loads(s))

    async def recv(self):
        if not self._frames:
            await asyncio.sleep(0)
            raise asyncio.CancelledError
        return json.dumps(self._frames.pop(0))

    async def close(self):
        pass


def _connect_fn(frames):
    async def _c(url):
        return FakeWS(frames)
    return _c


def _realtime_frames():
    px = {"AAPL": (303.72, 303.76, 303.75), "NVDA": (224.85, 224.86, 224.85), "SPY": (775.92, 775.94, 775.93)}
    frames = [{"ev": "status", "status": "connected"}, {"ev": "status", "status": "auth_success"}]
    for sym, (bp, ap, last) in px.items():
        frames.append({"ev": "Q", "sym": sym, "bp": bp, "bs": 3, "ap": ap, "as": 5, "bx": 11, "ax": 12, "t": TS})
        frames.append({"ev": "T", "sym": sym, "p": last, "s": 100, "t": TS})
        frames.append({"ev": "A", "sym": sym, "av": 1_000_000, "c": last, "t": TS})
    return frames


def _engine_and_provider(frames):
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0))
    journal = InMemoryJournal()
    desk, broker, _ = asyncio.run(build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()],
        journal=journal, risk=risk))
    engine = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, journal=journal)
    provider = MassiveProvider(MASSIVE_SYMBOLS, api_key="k", connect_fn=_connect_fn(frames), max_age_s=60)
    return engine, provider, broker, journal


def _bars(md, now):
    bars = []
    for row in md:
        if row.get("status") != "DATA_AVAILABLE":
            continue
        bid, ask = row["bid"], row["ask"]
        mid = (bid + ask) / 2.0
        bars.append(Bar(Instrument(row["symbol"], AssetClass.EQUITY), mid, mid, mid, mid, 0.0, now))
    return bars


import datetime as _dt  # noqa: E402

NOW = _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------- the pipeline delivers READY
def test_massive_realtime_flows_to_autonomous_input():
    engine, provider, broker, journal = _engine_and_provider(_realtime_frames())

    async def go():
        await provider.connect()
        for _ in range(9):          # 3 symbols × (Q,T,A)
            await provider._read_once()
        md = provider.market_rows(now=NOW)
        # every symbol is DATA_AVAILABLE + REALTIME from Massive (the gate passed)
        for row in md:
            assert row["source"] == "MASSIVE"
            assert row["status"] == "DATA_AVAILABLE"
            assert row["market_data_type"] == "REALTIME"
            assert row["bid"] and row["ask"] and row["last"]
        res = await engine.observe(now=NOW, bars=_bars(md, NOW), market_data=md)
        return md, res

    md, res = asyncio.run(go())

    # AAPL/NVDA/SPY READY -> Autonomous received each
    for sym in ("AAPL", "NVDA", "SPY"):
        assert sym in res["received"], f"{sym} not received by autonomous engine"
        assert sym in engine.observed_instruments

    # ABSOLUTE SAFETY: no execution of any kind
    assert engine.status is AutonomousStatus.DISABLED
    assert engine._trades_today == 0
    assert journal.all() == []                 # PaperBroker placed no order / recorded no trade
    assert res["fed"] == 3

    # the read-only observation is visible in metrics (proof the engine consumed the live feed)
    m = engine.metrics()
    assert m["observations"] >= 1
    for sym in ("AAPL", "NVDA", "SPY"):
        assert sym in m["observed_instruments"]


# --------------------------------------------------------------- no order path is ever taken
def test_observe_never_places_an_order():
    engine, provider, broker, journal = _engine_and_provider(_realtime_frames())
    calls = {"place": 0, "set_quote": 0}
    if hasattr(broker, "place_order"):
        orig = broker.place_order
        async def _spy(*a, **k):
            calls["place"] += 1
            return await orig(*a, **k)
        broker.place_order = _spy  # type: ignore
    if hasattr(broker, "set_quote"):
        so = broker.set_quote
        def _sq(*a, **k):
            calls["set_quote"] += 1
            return so(*a, **k)
        broker.set_quote = _sq  # type: ignore

    async def go():
        await provider.connect()
        for _ in range(9):
            await provider._read_once()
        md = provider.market_rows(now=NOW)
        await engine.observe(now=NOW, bars=_bars(md, NOW), market_data=md)

    asyncio.run(go())
    assert calls["place"] == 0        # placeOrder = 0
    assert calls["set_quote"] == 0    # read-only observe never pushes quotes to the broker


# --------------------------------------------------------------- Massive down -> no IBKR fallback
def test_massive_unavailable_is_honestly_blocked_no_fallback():
    # auth ok but NO market events -> books empty -> gate DATA_NOT_AVAILABLE (never realtime, never IBKR)
    frames = [{"ev": "status", "status": "connected"}, {"ev": "status", "status": "auth_success"}]
    engine, provider, broker, journal = _engine_and_provider(frames)

    async def go():
        await provider.connect()
        md = provider.market_rows(now=NOW)
        for row in md:
            assert row["source"] == "MASSIVE"
            assert row["status"] == "DATA_NOT_AVAILABLE"
            assert row["market_data_type"] is None          # never falsely REALTIME
        res = await engine.observe(now=NOW, bars=_bars(md, NOW), market_data=md)
        return res

    res = asyncio.run(go())
    assert res["received"] == []          # nothing entered the autonomous input
    assert engine._trades_today == 0
    assert journal.all() == []


def test_final_decision_mapping():
    engine, provider, broker, journal = _engine_and_provider(_realtime_frames())
    f = engine._final_from
    assert f({"execution_decision": "NO_TRADE", "reason": "data quality: not tradable"}) == "NO_DATA"
    assert f({"risk_decision": "REJECTED", "reason": "position cap"}) == "REJECTED_BY_RISK"
    assert f({"risk_decision": "APPROVED", "reason": "ok"}) == "PAPER_TRADE_WOULD_BE_EXECUTED"
    assert f({"reason": "no signal"}) == "NO_TRADE"


def test_decision_journal_writes_jsonl(tmp_path):
    import json as _json
    jpath = tmp_path / "decisions.jsonl"
    engine, provider, broker, journal = _engine_and_provider(_realtime_frames())
    engine.set_decision_journal(str(jpath))

    async def go():
        await provider.connect()
        for _ in range(9):
            await provider._read_once()
        md = provider.market_rows(now=NOW)
        # force a gated instrument so at least one NO_DATA decision is journaled
        md.append({"symbol": "ZZZ", "asset_class": "equity", "exchange": "X",
                   "status": "DATA_NOT_AVAILABLE", "market_data_type": None, "bid": None, "ask": None,
                   "source": "MASSIVE", "timestamp": None, "reason": ""})
        bars = _bars(md, NOW) + [Bar(Instrument("ZZZ", AssetClass.EQUITY), 1, 1, 1, 1, 0.0, NOW)]
        await engine.observe(now=NOW, bars=bars, market_data=md)

    asyncio.run(go())
    lines = [_json.loads(x) for x in jpath.read_text().splitlines()]
    assert lines, "decision journal is empty"
    zzz = [d for d in lines if d["instrument"] == "ZZZ"]
    assert zzz and zzz[0]["final_decision"] == "NO_DATA"
    assert all("final_decision" in d for d in lines)


def test_stale_massive_quote_blocked():
    old = TS - 120_000
    frames = [{"ev": "status", "status": "connected"}, {"ev": "status", "status": "auth_success"},
              {"ev": "Q", "sym": "AAPL", "bp": 303.7, "bs": 3, "ap": 303.8, "as": 5, "bx": 11, "ax": 12, "t": old}]
    engine, provider, broker, journal = _engine_and_provider(frames)

    async def go():
        await provider.connect()
        await provider._read_once()
        provider.manager.max_age_s = 5.0
        md = provider.market_rows(now=NOW)
        row = next(r for r in md if r["symbol"] == "AAPL")
        assert row["status"] == "STALE"
        res = await engine.observe(now=NOW, bars=_bars(md, NOW), market_data=md)
        return res

    res = asyncio.run(go())
    assert "AAPL" not in res["received"]
