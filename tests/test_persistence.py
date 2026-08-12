"""Persistence tests (§21): in-memory state store semantics and the Postgres journal's
record/round-trip logic via a fake connection (identical to the SQLite path)."""

from datetime import datetime, timedelta, timezone

from atp.brokers.base import Fill
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument
from atp.journal import PostgresJournal, TradeAssembler, TradeContext, TradeResult
from atp.persistence import InMemoryStateStore

INST = Instrument("AAPL", AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- state store
def test_state_store_set_get_delete():
    s = InMemoryStateStore()
    s.set("equity", 100_000.0)
    assert s.get("equity") == 100_000.0
    s.delete("equity")
    assert s.get("equity") is None
    assert s.get("missing") is None


def test_state_store_json_shapes_match_redis():
    s = InMemoryStateStore()
    s.set("pos", {"AAPL": 100, "MSFT": -5})
    assert s.get("pos") == {"AAPL": 100, "MSFT": -5}
    s.set("pair", ("a", "b"))       # tuple -> list, exactly like Redis JSON
    assert s.get("pair") == ["a", "b"]


def test_state_store_keys_prefix_and_bulk():
    s = InMemoryStateStore()
    s.set_many({"pos:AAPL": 1, "pos:MSFT": 2, "risk:halted": False})
    assert s.keys("pos:") == ["pos:AAPL", "pos:MSFT"]
    assert s.get_all("pos:") == {"pos:AAPL": 1, "pos:MSFT": 2}


# --------------------------------------------------------------------------- postgres journal
class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        head = sql.strip().split()[0].upper()
        if head == "INSERT":
            self._store.append(params)
        elif head == "SELECT":
            self._result = list(self._store)

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self):
        self._store: list = []

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


def _record():
    a = TradeAssembler()
    a.on_fill(Fill(INST, Side.BUY, 10, 100.0, 1.0, T0), TradeContext(strategy="m", regime="up"))
    return a.on_fill(Fill(INST, Side.SELL, 10, 110.0, 1.0, T0 + timedelta(minutes=5)), None)


def test_postgres_journal_record_and_roundtrip():
    rec = _record()
    pg = PostgresJournal(dsn="unused", conn=_FakeConn())
    pg.record(rec)
    loaded = pg.all()

    assert len(loaded) == 1
    got = loaded[0]
    assert got.trade_id == rec.trade_id
    assert got.strategy == "m" and got.regime == "up"
    assert got.realized_pnl == rec.realized_pnl
    assert got.result is TradeResult.WIN
    assert got.entry_ts == rec.entry_ts     # tz-aware datetime preserved
    assert got.features == rec.features


def test_postgres_journal_by_strategy_filter():
    pg = PostgresJournal(dsn="unused", conn=_FakeConn())
    pg.record(_record())
    assert len(pg.by_strategy("m")) == 1
    assert pg.by_strategy("other") == []
