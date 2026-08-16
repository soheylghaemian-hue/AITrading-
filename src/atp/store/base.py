"""Durable source-of-truth Store (§ Phase B).

A single transactional persistence surface for all safety-critical trading state. Two backends share
one SQL implementation (`SqlStore`): SQLite (local/test, file-backed) and PostgreSQL (production).
PostgreSQL is authoritative in production; Redis is NEVER authoritative for trading state.

Design rules honored here:
  * money is exact Decimal (stored NUMERIC in PG, canonical TEXT in SQLite) — never binary float;
  * timestamps are ISO-8601 UTC text everywhere;
  * every control transition writes runtime_state AND an audit_event inside ONE transaction;
  * a fill and its position update commit inside ONE transaction (crash-atomic);
  * if the database is unavailable, callers must fail closed (see runtime.gate).
"""

from __future__ import annotations

import abc
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .money import D, money_str, opt_money_str, to_decimal


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- rows
@dataclass(slots=True)
class RuntimeStateRow:
    status: str
    updated_at: str
    correlation_id: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class KillSwitchRow:
    engaged: bool
    actor: str | None
    reason: str | None
    updated_at: str | None


@dataclass(slots=True)
class DailyPnlRow:
    trade_date: str
    day_start_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    updated_at: str


@dataclass(slots=True)
class DailyLossLockRow:
    trade_date: str
    engaged: bool
    reason: str | None
    updated_at: str | None


@dataclass(slots=True)
class RiskConfigRow:
    capital: Decimal
    risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    updated_at: str


@dataclass(slots=True)
class RiskStateRow:
    day_start_equity: Decimal
    peak_equity: Decimal
    halted: bool
    killed: bool
    updated_at: str


@dataclass(slots=True)
class OrderRow:
    client_order_id: str
    idempotency_key: str
    instrument: str
    side: str
    quantity: Decimal
    order_type: str
    state: str                       # INTENT/AUTHORIZED/REJECTED/SUBMITTED/FILLED/CANCELLED
    broker_order_id: str | None
    correlation_id: str | None
    reason: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class FillRow:
    fill_id: str
    client_order_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    ts: str


@dataclass(slots=True)
class PositionRow:
    instrument: str
    quantity: Decimal
    avg_price: Decimal
    realized_pnl: Decimal
    updated_at: str


@dataclass(slots=True)
class AuditEventRow:
    event_id: str
    ts: str
    actor: str
    action: str
    previous_state: str | None
    new_state: str | None
    reason: str | None
    correlation_id: str | None


@dataclass(slots=True)
class DecisionRow:
    decision_id: str
    ts: str
    instrument: str
    final_decision: str | None
    payload: str | None
    correlation_id: str | None


@dataclass(slots=True)
class NewsItemRow:
    id: str
    symbol: str
    title: str
    source: str | None
    url: str | None
    published_at: str
    content_summary: str | None
    sentiment_score: float | None
    impact_level: str | None
    created_at: str


@dataclass(slots=True)
class OhlcBarRow:
    symbol: str
    interval: str
    ts: str                    # ISO-8601 UTC bar-open, interval-aligned
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    created_at: str


# --------------------------------------------------------------------------- interface
class Store(abc.ABC):
    """Abstract durable store. Concrete backends: SqliteStore, PostgresStore."""

    @abc.abstractmethod
    def ping(self) -> bool: ...
    @abc.abstractmethod
    def close(self) -> None: ...


# --------------------------------------------------------------------------- shared SQL impl
class SqlStore(Store):
    """DB-agnostic SQL implementation. Subclasses supply a DB-API connection, the parameter
    placeholder, and money adaptation. All writes go through short explicit transactions."""

    PLACEHOLDER = "?"          # SQLite; PostgresStore overrides with "%s"
    MONEY_AS_TEXT = True       # SQLite stores money as canonical TEXT; PG stores NUMERIC
    LOCK_CLAUSE = ""           # SQLite serializes writers; PostgresStore uses " FOR UPDATE"

    def __init__(self, conn):
        self._conn = conn

    # -- low level ---------------------------------------------------------
    def _q(self, sql: str) -> str:
        return sql if self.PLACEHOLDER == "?" else sql.replace("?", self.PLACEHOLDER)

    def _m(self, value):
        """Adapt a Decimal for storage."""
        if value is None:
            return None
        return money_str(value) if self.MONEY_AS_TEXT else Decimal(str(value))

    @contextmanager
    def tx(self):
        """One explicit transaction. Commit on success, rollback on any error."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def _exec(self, cur, sql, params=()):
        cur.execute(self._q(sql), params)

    def _one(self, sql, params=()):
        cur = self._conn.cursor()
        try:
            cur.execute(self._q(sql), params)
            return cur.fetchone()
        finally:
            cur.close()

    def _all(self, sql, params=()):
        cur = self._conn.cursor()
        try:
            cur.execute(self._q(sql), params)
            return cur.fetchall()
        finally:
            cur.close()

    def ping(self) -> bool:
        try:
            self._one("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- runtime state + transitions (atomic with audit) -------------------
    def get_runtime_state(self) -> RuntimeStateRow | None:
        r = self._one("SELECT status, updated_at, correlation_id, reason FROM runtime_state WHERE id=1")
        return RuntimeStateRow(r[0], r[1], r[2], r[3]) if r else None

    def transition(self, *, new_status: str, actor: str, reason: str | None,
                   correlation_id: str | None = None, previous: str | None = None,
                   action: str | None = None) -> AuditEventRow:
        """Persist runtime_state AND an audit_event in ONE transaction."""
        cid = correlation_id or new_id()
        now = utcnow_iso()
        prev = previous if previous is not None else (self.get_runtime_state().status
                                                      if self.get_runtime_state() else None)
        evt = AuditEventRow(new_id(), now, actor, action or f"TRANSITION:{new_status}",
                            prev, new_status, reason, cid)
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO runtime_state (id,status,updated_at,correlation_id,reason) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
                "correlation_id=excluded.correlation_id, reason=excluded.reason",
                (new_status, now, cid, reason))
            self._insert_audit(cur, evt)
        return evt

    # -- audit -------------------------------------------------------------
    def _insert_audit(self, cur, evt: AuditEventRow):
        self._exec(cur,
            "INSERT INTO audit_events (event_id,ts,actor,action,previous_state,new_state,reason,correlation_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (evt.event_id, evt.ts, evt.actor, evt.action, evt.previous_state,
             evt.new_state, evt.reason, evt.correlation_id))

    def audit(self, *, actor: str, action: str, reason: str | None = None,
              previous_state: str | None = None, new_state: str | None = None,
              correlation_id: str | None = None) -> AuditEventRow:
        evt = AuditEventRow(new_id(), utcnow_iso(), actor, action, previous_state,
                            new_state, reason, correlation_id or new_id())
        with self.tx() as cur:
            self._insert_audit(cur, evt)
        return evt

    def recent_audit(self, limit: int = 50) -> list[AuditEventRow]:
        rows = self._all("SELECT event_id,ts,actor,action,previous_state,new_state,reason,correlation_id "
                         "FROM audit_events ORDER BY ts DESC LIMIT ?", (limit,))
        return [AuditEventRow(*r) for r in rows]

    # -- kill switch (durable latch) --------------------------------------
    def get_kill_switch(self) -> KillSwitchRow:
        r = self._one("SELECT engaged, actor, reason, updated_at FROM kill_switch WHERE id=1")
        if not r:
            return KillSwitchRow(False, None, None, None)
        return KillSwitchRow(bool(r[0]), r[1], r[2], r[3])

    def set_kill_switch(self, *, engaged: bool, actor: str, reason: str | None,
                        correlation_id: str | None = None) -> AuditEventRow:
        cid = correlation_id or new_id()
        now = utcnow_iso()
        evt = AuditEventRow(new_id(), now, actor, "KILL" if engaged else "RESET",
                            None, "KILLED" if engaged else "RESET", reason, cid)
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET engaged=excluded.engaged, actor=excluded.actor, "
                "reason=excluded.reason, updated_at=excluded.updated_at",
                (1 if engaged else 0, actor, reason, now))
            self._insert_audit(cur, evt)
        return evt

    # -- daily P&L + loss lock (durable) ----------------------------------
    def get_daily_pnl(self, trade_date: str) -> DailyPnlRow | None:
        r = self._one("SELECT trade_date, day_start_equity, realized_pnl, unrealized_pnl, updated_at "
                      "FROM daily_pnl WHERE trade_date=?", (trade_date,))
        if not r:
            return None
        return DailyPnlRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4])

    def upsert_daily_pnl(self, *, trade_date: str, day_start_equity, realized_pnl, unrealized_pnl) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO daily_pnl (trade_date,day_start_equity,realized_pnl,unrealized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "day_start_equity=excluded.day_start_equity, realized_pnl=excluded.realized_pnl, "
                "unrealized_pnl=excluded.unrealized_pnl, updated_at=excluded.updated_at",
                (trade_date, self._m(day_start_equity), self._m(realized_pnl),
                 self._m(unrealized_pnl), now))

    def try_reserve_daily_risk(self, *, trade_date: str, amount, limit) -> bool:
        """Concurrency-safe risk-budget reservation: lock today's daily_pnl row, and only if the
        requested `amount` still fits the remaining budget (`limit` − loss-so-far) reserve it by
        booking it as realized loss. Two racing authorizations cannot jointly exceed the budget:
        the second waits on the row lock and then sees the reduced remaining. Fails closed if there
        is no budget context."""
        with self.tx() as cur:
            self._exec(cur, "SELECT realized_pnl, unrealized_pnl FROM daily_pnl WHERE trade_date=?"
                       + self.LOCK_CLAUSE, (trade_date,))
            row = cur.fetchone()
            if row is None:
                return False
            realized, unreal = to_decimal(row[0]), to_decimal(row[1])
            loss_so_far = max(D(0), -(realized + unreal))
            remaining = D(limit) - loss_so_far
            if D(amount) > remaining:
                return False
            self._exec(cur, "UPDATE daily_pnl SET realized_pnl=?, updated_at=? WHERE trade_date=?",
                       (self._m(realized - D(amount)), utcnow_iso(), trade_date))
            return True

    def get_daily_loss_lock(self, trade_date: str) -> DailyLossLockRow:
        r = self._one("SELECT trade_date, engaged, reason, updated_at FROM daily_loss_lock WHERE trade_date=?",
                      (trade_date,))
        if not r:
            return DailyLossLockRow(trade_date, False, None, None)
        return DailyLossLockRow(r[0], bool(r[1]), r[2], r[3])

    def set_daily_loss_lock(self, *, trade_date: str, engaged: bool, reason: str | None,
                            actor: str = "risk", correlation_id: str | None = None) -> AuditEventRow:
        now = utcnow_iso()
        evt = AuditEventRow(new_id(), now, actor, "DAILY_LOSS_LOCK" if engaged else "DAILY_LOSS_UNLOCK",
                            None, "HALTED" if engaged else None, reason, correlation_id or new_id())
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO daily_loss_lock (trade_date,engaged,reason,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(trade_date) DO UPDATE SET engaged=excluded.engaged, reason=excluded.reason, "
                "updated_at=excluded.updated_at",
                (trade_date, 1 if engaged else 0, reason, now))
            self._insert_audit(cur, evt)
        return evt

    # -- risk config + state ----------------------------------------------
    def get_risk_config(self) -> RiskConfigRow | None:
        r = self._one("SELECT capital, risk_per_trade_pct, max_daily_loss_pct, updated_at "
                      "FROM risk_config WHERE id=1")
        return RiskConfigRow(to_decimal(r[0]), to_decimal(r[1]), to_decimal(r[2]), r[3]) if r else None

    def upsert_risk_config(self, *, capital, risk_per_trade_pct, max_daily_loss_pct) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO risk_config (id,capital,risk_per_trade_pct,max_daily_loss_pct,updated_at) "
                "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET capital=excluded.capital, "
                "risk_per_trade_pct=excluded.risk_per_trade_pct, "
                "max_daily_loss_pct=excluded.max_daily_loss_pct, updated_at=excluded.updated_at",
                (self._m(capital), self._m(risk_per_trade_pct), self._m(max_daily_loss_pct), now))

    def get_risk_state(self) -> RiskStateRow | None:
        r = self._one("SELECT day_start_equity, peak_equity, halted, killed, updated_at "
                      "FROM risk_state WHERE id=1")
        return RiskStateRow(to_decimal(r[0]), to_decimal(r[1]), bool(r[2]), bool(r[3]), r[4]) if r else None

    def upsert_risk_state(self, *, day_start_equity, peak_equity, halted: bool, killed: bool) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO risk_state (id,day_start_equity,peak_equity,halted,killed,updated_at) "
                "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET day_start_equity=excluded.day_start_equity, "
                "peak_equity=excluded.peak_equity, halted=excluded.halted, killed=excluded.killed, "
                "updated_at=excluded.updated_at",
                (self._m(day_start_equity), self._m(peak_equity), 1 if halted else 0,
                 1 if killed else 0, now))

    # -- orders (idempotent lifecycle) ------------------------------------
    def get_order_by_idempotency(self, key: str) -> OrderRow | None:
        r = self._one("SELECT client_order_id,idempotency_key,instrument,side,quantity,order_type,state,"
                      "broker_order_id,correlation_id,reason,created_at,updated_at FROM orders "
                      "WHERE idempotency_key=?", (key,))
        return self._order_row(r) if r else None

    def get_order(self, client_order_id: str) -> OrderRow | None:
        r = self._one("SELECT client_order_id,idempotency_key,instrument,side,quantity,order_type,state,"
                      "broker_order_id,correlation_id,reason,created_at,updated_at FROM orders "
                      "WHERE client_order_id=?", (client_order_id,))
        return self._order_row(r) if r else None

    def _order_row(self, r) -> OrderRow:
        return OrderRow(r[0], r[1], r[2], r[3], to_decimal(r[4]), r[5], r[6], r[7], r[8], r[9], r[10], r[11])

    def insert_order_intent(self, *, client_order_id: str, idempotency_key: str, instrument: str,
                            side: str, quantity, order_type: str, correlation_id: str,
                            reason: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO orders (client_order_id,idempotency_key,instrument,side,quantity,order_type,"
                "state,broker_order_id,correlation_id,reason,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, idempotency_key, instrument, side, self._m(quantity), order_type,
                 "INTENT", None, correlation_id, reason, now, now))

    def update_order_state(self, *, client_order_id: str, state: str,
                           broker_order_id: str | None = None, reason: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, reason=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (state, broker_order_id, reason, now, client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, reason=?, updated_at=? WHERE client_order_id=?",
                           (state, reason, now, client_order_id))

    # -- fill + position update in ONE transaction (crash-atomic) ---------
    def apply_fill(self, *, fill: FillRow, position: PositionRow, order_state: str = "FILLED",
                   broker_order_id: str | None = None) -> None:
        """Insert the fill, upsert the resulting position, and advance the order (with its broker
        acknowledgement) — all in ONE transaction. A crash cannot leave a fill recorded without its
        position update, nor mark an order filled without persisting the fill."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO fills (fill_id,client_order_id,instrument,side,quantity,price,commission,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fill.fill_id, fill.client_order_id, fill.instrument, fill.side,
                 self._m(fill.quantity), self._m(fill.price), self._m(fill.commission), fill.ts))
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET quantity=excluded.quantity, "
                "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                (position.instrument, self._m(position.quantity), self._m(position.avg_price),
                 self._m(position.realized_pnl), now))
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (order_state, broker_order_id, now, fill.client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (order_state, now, fill.client_order_id))

    def apply_fill_atomic(self, *, fill: FillRow, compute, order_state: str = "FILLED",
                          broker_order_id: str | None = None) -> PositionRow:
        """Concurrency-safe fill application. Inside ONE transaction: ensure the position row exists
        (a zero row is inserted if absent — semantically identical to "no position"), lock it
        (SELECT … FOR UPDATE on PostgreSQL; SQLite serializes writers), recompute the position from
        the locked value via `compute(current_row_or_None) -> PositionRow`, then persist fill +
        position + order together. Two concurrent fills on the same instrument cannot interleave —
        including the FIRST fill on a brand-new instrument, since FOR UPDATE cannot lock a row that
        does not yet exist."""
        now = utcnow_iso()
        with self.tx() as cur:
            # Guarantee a lockable row BEFORE FOR UPDATE. Without this, two concurrent first fills on a
            # new instrument both see "no row", both compute from zero, and one update is lost. A zero
            # row folds identically to None in compute() (qty/avg/realized = 0), so this is safe.
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO NOTHING",
                (fill.instrument, self._m(D(0)), self._m(D(0)), self._m(D(0)), now))
            self._exec(cur,
                "SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions "
                "WHERE instrument=?" + self.LOCK_CLAUSE, (fill.instrument,))
            row = cur.fetchone()
            current = (PositionRow(row[0], to_decimal(row[1]), to_decimal(row[2]),
                                   to_decimal(row[3]), row[4]) if row else None)
            new_pos: PositionRow = compute(current)
            self._exec(cur,
                "INSERT INTO fills (fill_id,client_order_id,instrument,side,quantity,price,commission,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fill.fill_id, fill.client_order_id, fill.instrument, fill.side,
                 self._m(fill.quantity), self._m(fill.price), self._m(fill.commission), fill.ts))
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET quantity=excluded.quantity, "
                "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                (new_pos.instrument, self._m(new_pos.quantity), self._m(new_pos.avg_price),
                 self._m(new_pos.realized_pnl), now))
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (order_state, broker_order_id, now, fill.client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (order_state, now, fill.client_order_id))
        return new_pos

    def get_position(self, instrument: str) -> PositionRow | None:
        r = self._one("SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions "
                      "WHERE instrument=?", (instrument,))
        return PositionRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4]) if r else None

    def list_positions(self) -> list[PositionRow]:
        rows = self._all("SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions")
        return [PositionRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4]) for r in rows]

    def list_fills(self, instrument: str | None = None) -> list[FillRow]:
        if instrument:
            rows = self._all("SELECT fill_id,client_order_id,instrument,side,quantity,price,commission,ts "
                             "FROM fills WHERE instrument=? ORDER BY ts", (instrument,))
        else:
            rows = self._all("SELECT fill_id,client_order_id,instrument,side,quantity,price,commission,ts "
                             "FROM fills ORDER BY ts")
        return [FillRow(r[0], r[1], r[2], r[3], to_decimal(r[4]), to_decimal(r[5]), to_decimal(r[6]), r[7])
                for r in rows]

    # -- OHLC bars (§ Phase G1) -------------------------------------------
    def upsert_ohlc_bar(self, *, symbol: str, interval: str, ts: str, open: float, high: float,
                        low: float, close: float, volume: float, source: str) -> None:
        """Insert or update the (forming) bar for (symbol, interval, ts). Idempotent: re-writing the same
        bucket updates high/low/close/volume; a duplicate never creates a second row."""
        now = utcnow_iso()
        def m(v):  # float -> exact decimal in the shared money encoding
            return self._m(D(str(v)))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ohlc_bars (symbol,interval,ts,open,high,low,close,volume,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,interval,ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume, source=excluded.source",
                (symbol, interval, ts, m(open), m(high), m(low), m(close), m(volume), source, now))

    def insert_ohlc_bar(self, *, symbol: str, interval: str, ts: str, open: float, high: float,
                        low: float, close: float, volume: float, source: str) -> None:
        """Strict insert — RAISES on a duplicate (symbol, interval, ts) via the PK/unique constraint."""
        now = utcnow_iso()
        def m(v):
            return self._m(D(str(v)))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ohlc_bars (symbol,interval,ts,open,high,low,close,volume,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, ts, m(open), m(high), m(low), m(close), m(volume), source, now))

    def list_ohlc_bars(self, symbol: str, interval: str, limit: int = 500) -> list[OhlcBarRow]:
        """Most-recent `limit` bars for (symbol, interval), returned oldest→newest (chart order)."""
        n = max(1, min(5000, int(limit)))
        rows = self._all(
            "SELECT symbol,interval,ts,open,high,low,close,volume,source,created_at FROM ohlc_bars "
            "WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?", (symbol, interval, n))
        out = [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                          to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]
        out.reverse()
        return out

    def count_ohlc_bars(self, symbol: str, interval: str) -> int:
        r = self._one("SELECT COUNT(*) FROM ohlc_bars WHERE symbol=? AND interval=?", (symbol, interval))
        return int(r[0]) if r else 0

    def latest_ohlc_bars(self) -> list[OhlcBarRow]:
        """The most-recent bar for every (symbol, interval) — used to resume forming bars after a
        service restart, so an in-progress bar is never reset/corrupted by a restart."""
        rows = self._all(
            "SELECT o.symbol,o.interval,o.ts,o.open,o.high,o.low,o.close,o.volume,o.source,o.created_at "
            "FROM ohlc_bars o JOIN (SELECT symbol,interval,MAX(ts) AS mx FROM ohlc_bars "
            "GROUP BY symbol,interval) g ON o.symbol=g.symbol AND o.interval=g.interval AND o.ts=g.mx")
        return [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                           to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]

    # -- decisions / heartbeats / market-data health ----------------------
    def insert_decision(self, *, decision_id: str, ts: str, instrument: str, payload_json: str,
                        final_decision: str | None, correlation_id: str | None = None) -> None:
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO decisions (decision_id,ts,instrument,final_decision,payload,correlation_id) "
                "VALUES (?,?,?,?,?,?)",
                (decision_id, ts, instrument, final_decision, payload_json, correlation_id))

    # -- news items (§ Phase G2.1, read-only intelligence) ----------------
    def upsert_news_item(self, *, id: str, symbol: str, title: str, source: str | None, url: str | None,
                         published_at: str, content_summary: str | None, sentiment_score: float | None,
                         impact_level: str | None) -> None:
        """Insert or update a news item (idempotent on the deterministic `id`). Stores ONLY article
        fields + the derived sentiment/impact — never a provider key or secret."""
        now = utcnow_iso()
        ss = None if sentiment_score is None else str(float(sentiment_score))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO news_items (id,symbol,title,source,url,published_at,content_summary,"
                "sentiment_score,impact_level,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, source=excluded.source, "
                "url=excluded.url, published_at=excluded.published_at, "
                "content_summary=excluded.content_summary, sentiment_score=excluded.sentiment_score, "
                "impact_level=excluded.impact_level",
                (id, symbol, title, source, url, published_at, content_summary, ss, impact_level, now))

    def list_news(self, symbol: str, limit: int = 50) -> list[NewsItemRow]:
        """Most-recent `limit` news items for a symbol (newest first). Empty when none collected."""
        n = max(1, min(200, int(limit)))
        rows = self._all(
            "SELECT id,symbol,title,source,url,published_at,content_summary,sentiment_score,"
            "impact_level,created_at FROM news_items WHERE symbol=? ORDER BY published_at DESC LIMIT ?",
            (symbol, n))
        return [NewsItemRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                            (float(r[7]) if r[7] is not None else None), r[8], r[9]) for r in rows]

    def count_news(self, symbol: str | None = None) -> int:
        if symbol:
            r = self._one("SELECT COUNT(*) FROM news_items WHERE symbol=?", (symbol,))
        else:
            r = self._one("SELECT COUNT(*) FROM news_items")
        return int(r[0]) if r else 0

    def list_decisions(self, limit: int = 50) -> list[DecisionRow]:
        """Most-recent `limit` AI decisions (newest first) for the dashboard read-model. Read-only;
        returns an empty list when none have been recorded (the UI shows NO DATA)."""
        n = max(1, min(500, int(limit)))
        rows = self._all(
            "SELECT decision_id,ts,instrument,final_decision,payload,correlation_id FROM decisions "
            "ORDER BY ts DESC LIMIT ?", (n,))
        return [DecisionRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def upsert_heartbeat(self, *, service: str, status: str, detail: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO service_heartbeats (service,status,detail,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(service) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                "updated_at=excluded.updated_at",
                (service, status, detail, now))

    def list_heartbeats(self) -> list[tuple]:
        return self._all("SELECT service,status,detail,updated_at FROM service_heartbeats ORDER BY service")

    def upsert_md_health(self, *, symbol: str, source: str, status: str, latency_ms, ts: str) -> None:
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO market_data_health (symbol,source,status,latency_ms,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET source=excluded.source, "
                "status=excluded.status, latency_ms=excluded.latency_ms, updated_at=excluded.updated_at",
                (symbol, source, status, latency_ms, ts))

    def list_md_health(self) -> list[tuple]:
        return self._all("SELECT symbol,source,status,latency_ms,updated_at FROM market_data_health "
                         "ORDER BY symbol")
