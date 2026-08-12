"""PostgreSQL trade-journal adapter (§21).

Implements the same `TradeJournal` interface as `SQLiteJournal`, so the desk and analytics use
it without change (§3) — it's the durable, multi-writer system of record for production. The
`psycopg` driver is lazy-imported; this module loads fine without it, and the offline suite
exercises the identical logic via `SQLiteJournal`. Schema and column order mirror the SQLite
table exactly (`store._COLUMNS`) so records round-trip identically across backends.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..logging_config import get_logger
from .record import TradeRecord, TradeResult
from .store import _COLUMNS, TradeJournal

log = get_logger("journal.pg")

_CREATE = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    instrument_key  TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    direction       TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    regime          TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    entry_ts        TIMESTAMPTZ NOT NULL,
    exit_ts         TIMESTAMPTZ NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    expected_return DOUBLE PRECISION NOT NULL,
    realized_return DOUBLE PRECISION NOT NULL,
    gross_pnl       DOUBLE PRECISION NOT NULL,
    commission      DOUBLE PRECISION NOT NULL,
    realized_pnl    DOUBLE PRECISION NOT NULL,
    slippage        DOUBLE PRECISION NOT NULL,
    mfe             DOUBLE PRECISION NOT NULL,
    mae             DOUBLE PRECISION NOT NULL,
    bars_held       INTEGER NOT NULL,
    holding_seconds DOUBLE PRECISION NOT NULL,
    result          TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    features        JSONB NOT NULL DEFAULT '{}',
    underlying      TEXT NOT NULL DEFAULT '',
    agent           TEXT NOT NULL DEFAULT '',
    signal_action   TEXT NOT NULL DEFAULT '',
    signal_strength DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_risk   DOUBLE PRECISION NOT NULL DEFAULT 0,
    stop_price      DOUBLE PRECISION NOT NULL DEFAULT 0,
    target_price    DOUBLE PRECISION NOT NULL DEFAULT 0,
    financing_cost  DOUBLE PRECISION NOT NULL DEFAULT 0,
    strategy_version TEXT NOT NULL DEFAULT 'v0'
);
CREATE INDEX IF NOT EXISTS ix_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS ix_trades_regime ON trades(regime);
"""


class PostgresJournal(TradeJournal):
    def __init__(self, dsn: str, *, conn: Any = None) -> None:
        if conn is not None:
            self._conn = conn
        else:
            import psycopg  # noqa: PLC0415 — lazy; only needed for a live connection

            self._conn = psycopg.connect(dsn)
        self._create()

    def _create(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_CREATE)
        self._conn.commit()

    def record(self, trade: TradeRecord) -> None:
        values = (
            trade.trade_id, trade.instrument_key, trade.asset_class, trade.direction,
            trade.strategy, trade.regime, trade.model_version, trade.entry_ts, trade.exit_ts,
            trade.quantity, trade.entry_price, trade.exit_price, trade.confidence,
            trade.expected_return, trade.realized_return, trade.gross_pnl, trade.commission,
            trade.realized_pnl, trade.slippage, trade.mfe, trade.mae, trade.bars_held,
            trade.holding_seconds, trade.result.value, trade.rationale, json.dumps(trade.features),
            trade.underlying, trade.agent, trade.signal_action, trade.signal_strength,
            trade.expected_risk, trade.stop_price, trade.target_price, trade.financing_cost,
            trade.strategy_version,
        )
        placeholders = ", ".join(["%s"] * len(_COLUMNS))
        sql = (
            f"INSERT INTO trades ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT (trade_id) DO NOTHING"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, values)
        self._conn.commit()

    def all(self) -> list[TradeRecord]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM trades ORDER BY exit_ts")
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: tuple) -> TradeRecord:
        d = dict(zip(_COLUMNS, row))
        features = d["features"]
        if isinstance(features, str):
            features = json.loads(features)
        return TradeRecord(
            trade_id=d["trade_id"], instrument_key=d["instrument_key"], asset_class=d["asset_class"],
            direction=d["direction"], strategy=d["strategy"], regime=d["regime"],
            model_version=d["model_version"],
            entry_ts=d["entry_ts"] if isinstance(d["entry_ts"], datetime) else datetime.fromisoformat(d["entry_ts"]),
            exit_ts=d["exit_ts"] if isinstance(d["exit_ts"], datetime) else datetime.fromisoformat(d["exit_ts"]),
            quantity=d["quantity"], entry_price=d["entry_price"], exit_price=d["exit_price"],
            confidence=d["confidence"], expected_return=d["expected_return"],
            realized_return=d["realized_return"], gross_pnl=d["gross_pnl"], commission=d["commission"],
            realized_pnl=d["realized_pnl"], slippage=d["slippage"], mfe=d["mfe"], mae=d["mae"],
            bars_held=d["bars_held"], holding_seconds=d["holding_seconds"],
            result=TradeResult(d["result"]), rationale=d["rationale"], features=features or {},
            underlying=d["underlying"], agent=d["agent"], signal_action=d["signal_action"],
            signal_strength=d["signal_strength"], expected_risk=d["expected_risk"],
            stop_price=d["stop_price"], target_price=d["target_price"],
            financing_cost=d["financing_cost"], strategy_version=d["strategy_version"],
        )

    def close(self) -> None:
        self._conn.close()
