"""Phase G1 — OHLC candle pipeline acceptance tests (durable store; no live feed required).

Proves: real trades create correct OHLC bars; candles survive a service restart and an in-progress bar
resumes (never resets); a duplicate bar timestamp is rejected; multiple symbols are isolated; missing /
non-MASSIVE data produces NO candles (never fabricated); and a store failure fails CLOSED.
"""
import pytest

from datetime import datetime, timezone

from atp.store import open_store
from atp.marketdata.ohlc_aggregator import OhlcIngestor, trade_is_ingestable

BASE = int(datetime(2026, 8, 15, 14, 30, 0, tzinfo=timezone.utc).timestamp())   # minute-aligned epoch


def trade(sym, price, size, ts_s, **kw):
    d = {"symbol": sym, "price": price, "size": size, "ts": ts_s * 1000,        # Massive ts are ms
         "source": "MASSIVE", "status": "READY", "realtime": True}
    d.update(kw)
    return d


@pytest.fixture
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))                                 # migrates ohlc_bars


# 1 --------------------------------------------------------------- realtime trades create candles
def test_realtime_trades_create_candles(store):
    ing = OhlcIngestor(store)
    ing.ingest(trade("NVDA", 100.0, 10, BASE + 0))
    ing.ingest(trade("NVDA", 102.0, 5, BASE + 20))
    ing.ingest(trade("NVDA", 99.5, 7, BASE + 40))
    bars = store.list_ohlc_bars("NVDA", "1m", 100)
    assert len(bars) == 1
    b = bars[0]
    assert (float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)) == (100.0, 102.0, 99.5, 99.5, 22.0)
    assert b.source == "MASSIVE"
    assert b.ts.startswith("2026-08-15T14:30:00")
    assert store.count_ohlc_bars("NVDA", "5m") == 1 and store.count_ohlc_bars("NVDA", "1D") == 1


# 2 & 7 ----------------------------------------------------------- historical candles survive restart
def test_ohlc_survives_restart(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    ing = OhlcIngestor(s1)
    ing.ingest(trade("NVDA", 100.0, 10, BASE))          # 14:30 bar
    ing.ingest(trade("NVDA", 101.0, 5, BASE + 65))      # 14:31 bar → two completed 1m bars
    s1.close()
    s2 = open_store(path)                               # "restart": reopen the durable store
    bars = s2.list_ohlc_bars("NVDA", "1m", 100)
    assert len(bars) == 2
    assert float(bars[0].open) == 100.0 and float(bars[1].close) == 101.0


# 2b -------------------------------------------------- an in-progress bar resumes after restart
def test_current_bar_resumes_after_restart(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    OhlcIngestor(s1).ingest(trade("NVDA", 100.0, 10, BASE))     # open=100 in the 14:30 bar
    s1.close()
    s2 = open_store(path)
    i2 = OhlcIngestor(s2)
    i2.recover()                                                # resume forming bars from PostgreSQL/SQLite
    i2.ingest(trade("NVDA", 105.0, 5, BASE + 30))               # SAME 14:30 bucket
    b = s2.list_ohlc_bars("NVDA", "1m", 10)[0]
    assert float(b.open) == 100.0                               # preserved, not reset to 105
    assert float(b.high) == 105.0 and float(b.volume) == 15.0


# 3 --------------------------------------------------------------- duplicate timestamp rejected
def test_duplicate_candle_timestamp_rejected(store):
    ts = "2026-08-15T14:30:00+00:00"
    store.insert_ohlc_bar(symbol="NVDA", interval="1m", ts=ts, open=100, high=101, low=99, close=100, volume=10, source="MASSIVE")
    with pytest.raises(Exception):
        store.insert_ohlc_bar(symbol="NVDA", interval="1m", ts=ts, open=100, high=101, low=99, close=100, volume=10, source="MASSIVE")
    assert store.count_ohlc_bars("NVDA", "1m") == 1


# 4 --------------------------------------------------------------- multiple symbols isolated
def test_multiple_symbols_isolated(store):
    ing = OhlcIngestor(store)
    ing.ingest(trade("NVDA", 100.0, 10, BASE))
    ing.ingest(trade("AAPL", 200.0, 20, BASE))
    ing.ingest(trade("NVDA", 101.0, 5, BASE + 10))
    nvda = store.list_ohlc_bars("NVDA", "1m", 10)[0]
    aapl = store.list_ohlc_bars("AAPL", "1m", 10)[0]
    assert float(nvda.close) == 101.0 and float(nvda.volume) == 15.0
    assert float(aapl.close) == 200.0 and float(aapl.volume) == 20.0
    assert store.list_ohlc_bars("SPY", "1m", 10) == []


# 5 --------------------------------------------------------------- missing / bad data → NO DATA
def test_missing_data_is_no_data(store):
    ing = OhlcIngestor(store)
    assert store.list_ohlc_bars("NVDA", "1m", 10) == []            # no trades → no bars
    assert not trade_is_ingestable(trade("NVDA", 100.0, 1, BASE, source="IBKR"))
    assert not trade_is_ingestable(trade("NVDA", 100.0, 1, BASE, status="STALE"))
    assert not trade_is_ingestable(trade("NVDA", 100.0, 1, BASE, realtime=False))
    assert ing.ingest(trade("NVDA", 100.0, 1, BASE, source="IBKR")) == 0
    assert store.list_ohlc_bars("NVDA", "1m", 10) == []            # rejected trade never made a candle


# 6 --------------------------------------------------------------- Postgres unavailable → fail closed
def test_store_unavailable_fails_closed():
    class FailingStore:
        def upsert_ohlc_bar(self, **kw):
            raise RuntimeError("database unavailable")
        def latest_ohlc_bars(self):
            return []
    ing = OhlcIngestor(FailingStore())
    with pytest.raises(Exception):
        ing.ingest(trade("NVDA", 100.0, 10, BASE))                # persistence fails → raises (never fabricates)
