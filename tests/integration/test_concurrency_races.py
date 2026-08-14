"""Phase B.5 — additional concurrency race coverage (REAL PostgreSQL).

Extends the single concurrent-fills case in test_postgres_contract.py to the full race matrix the
acceptance requires: BUY+BUY, SELL+SELL, BUY+SELL, 10+ concurrent workers on one instrument, and
independent instruments (which must NOT block each other — each is a distinct positions PK row and
therefore a distinct FOR UPDATE lock).

Every worker runs `apply_fill_atomic` on its OWN Postgres connection in its OWN thread, released
together via a barrier for maximal contention. After each race we assert the durable position row
equals the authoritative replay of all fills (`reconstruct_positions`) — which proves quantity,
average price and realized P&L are simultaneously consistent — plus the exact fill count (no lost /
no duplicate fill) and that every order reached state FILLED. Prices are held equal within a test so
the fold is order-independent and the assertions are deterministic regardless of serialization order.
"""

import os
import threading
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ATP_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATP_TEST_POSTGRES_DSN to run Postgres tests")

from atp.runtime import reconstruct_positions
from atp.runtime.positions import apply_fill_to_position
from atp.store import D, open_store
from atp.store.base import FillRow, utcnow_iso

_TABLES = ["schema_migrations", "accounts", "runtime_state", "risk_config", "risk_state",
           "kill_switch", "daily_pnl", "daily_loss_lock", "positions", "orders", "fills",
           "trades", "decisions", "audit_events", "service_heartbeats", "market_data_health"]


def _reset():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        for t in _TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.close()


@pytest.fixture()
def store():
    _reset()
    s = open_store(DSN)                                  # runs migrations on real Postgres
    yield s
    s.close()


def _new_store():
    return open_store(DSN, migrate=False)                # a fresh, independent connection


def _seed_orders(store, specs):
    for coid, instrument, side, qty, _price, _comm in specs:
        store.insert_order_intent(client_order_id=coid, idempotency_key=coid, instrument=instrument,
                                  side=side, quantity=D(qty), order_type="MARKET", correlation_id="c")


def _run_fills(specs):
    """Each spec = (coid, instrument, side, qty, price, commission). One thread + one connection each,
    released together for maximal lock contention."""
    barrier = threading.Barrier(len(specs))

    def do_fill(coid, instrument, side, qty, price, comm):
        s = _new_store()
        try:
            f = FillRow("fl_" + coid, coid, instrument, side, D(qty), D(price), D(comm), utcnow_iso())
            barrier.wait(timeout=30)                     # all workers hit apply_fill_atomic together
            s.apply_fill_atomic(fill=f, compute=lambda cur: apply_fill_to_position(cur, f))
        finally:
            s.close()

    threads = [threading.Thread(target=do_fill, args=spec) for spec in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _order_state(store, coid):
    return store._one("SELECT state FROM orders WHERE client_order_id=?", (coid,))[0]


def _assert_consistent(store, instrument, *, expected_qty, expected_fills):
    """The durable position row MUST equal the authoritative fills replay, with the exact fill count."""
    replay = reconstruct_positions(store).get(instrument)
    cached = store.get_position(instrument)
    assert cached is not None and replay is not None, "position row missing"
    assert cached.quantity == replay.quantity, "quantity != fills replay"
    assert cached.avg_price == replay.avg_price, "avg_price != fills replay"
    assert cached.realized_pnl == replay.realized_pnl, "realized_pnl != fills replay"
    assert cached.quantity == expected_qty, f"quantity {cached.quantity} != {expected_qty}"
    assert len(store.list_fills(instrument)) == expected_fills, "lost or duplicated fill"
    return cached


# ---------------------------------------------------------------- BUY + BUY  (start 0 → +20)
def test_race_buy_buy(store):
    specs = [("co1", "AAPL", "BUY", "10", "100", "1"),
             ("co2", "AAPL", "BUY", "10", "100", "1")]
    _seed_orders(store, specs)
    _run_fills(specs)
    _assert_consistent(store, "AAPL", expected_qty=Decimal("20"), expected_fills=2)
    assert _order_state(store, "co1") == "FILLED" and _order_state(store, "co2") == "FILLED"


# ---------------------------------------------------------------- SELL + SELL  (start 0 → -20)
def test_race_sell_sell(store):
    specs = [("co1", "MSFT", "SELL", "10", "200", "1"),
             ("co2", "MSFT", "SELL", "10", "200", "1")]
    _seed_orders(store, specs)
    _run_fills(specs)
    _assert_consistent(store, "MSFT", expected_qty=Decimal("-20"), expected_fills=2)
    assert _order_state(store, "co1") == "FILLED" and _order_state(store, "co2") == "FILLED"


# ---------------------------------------------------------------- BUY + SELL  (start 0 → net 0)
def test_race_buy_sell(store):
    specs = [("co1", "TSLA", "BUY", "10", "100", "1"),
             ("co2", "TSLA", "SELL", "10", "100", "1")]
    _seed_orders(store, specs)
    _run_fills(specs)
    # whichever serialized first opened, the second closed → net flat, both fills durable
    _assert_consistent(store, "TSLA", expected_qty=Decimal("0"), expected_fills=2)
    assert _order_state(store, "co1") == "FILLED" and _order_state(store, "co2") == "FILLED"


# ---------------------------------------------------------------- 12 concurrent workers, one symbol
def test_race_many_workers_same_instrument(store):
    n = 12
    specs = [(f"co{i}", "SPY", "BUY", "10", "100", "1") for i in range(n)]
    _seed_orders(store, specs)
    _run_fills(specs)
    _assert_consistent(store, "SPY", expected_qty=Decimal(str(10 * n)), expected_fills=n)
    filled = store._one("SELECT COUNT(*) FROM orders WHERE instrument=? AND state='FILLED'", ("SPY",))[0]
    assert filled == n                                   # every order FILLED exactly once


# ---------------------------------------------------------------- many instruments, no cross-block
def test_race_different_instruments(store):
    symbols = [f"SYM{i}" for i in range(8)]
    specs = ([(f"co_{s}_a", s, "BUY", "10", "100", "1") for s in symbols] +
             [(f"co_{s}_b", s, "BUY", "10", "100", "1") for s in symbols])
    _seed_orders(store, specs)
    _run_fills(specs)                                    # 16 workers across 8 independent PK rows
    for s in symbols:
        _assert_consistent(store, s, expected_qty=Decimal("20"), expected_fills=2)
