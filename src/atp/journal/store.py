"""Trade journal storage (§11, §21).

The `TradeJournal` interface is the seam: the desk records completed trades through it and
never knows the backend. `InMemoryJournal` is for tests/backtests; `SQLiteJournal` gives
durable local persistence with zero third-party dependencies (stdlib `sqlite3`). A Postgres
adapter (§21) later implements the same interface — same pattern as the broker abstraction.
"""

from __future__ import annotations

import abc
import json
import sqlite3
from datetime import datetime

from ..logging_config import get_logger
from .record import TradeRecord, TradeResult

log = get_logger("journal")


class TradeJournal(abc.ABC):
    @abc.abstractmethod
    def record(self, trade: TradeRecord) -> None: ...

    @abc.abstractmethod
    def all(self) -> list[TradeRecord]: ...

    def by_strategy(self, strategy: str) -> list[TradeRecord]:
        return [t for t in self.all() if t.strategy == strategy]

    def by_regime(self, regime: str) -> list[TradeRecord]:
        return [t for t in self.all() if t.regime == regime]

    def __len__(self) -> int:
        return len(self.all())


class InMemoryJournal(TradeJournal):
    def __init__(self) -> None:
        self._trades: list[TradeRecord] = []

    def record(self, trade: TradeRecord) -> None:
        self._trades.append(trade)
        log.debug("journaled %s %s pnl=%.2f", trade.strategy, trade.instrument_key, trade.realized_pnl)

    def all(self) -> list[TradeRecord]:
        return list(self._trades)


_COLUMNS = [
    "trade_id", "instrument_key", "asset_class", "direction", "strategy", "regime",
    "model_version", "entry_ts", "exit_ts", "quantity", "entry_price", "exit_price",
    "confidence", "expected_return", "realized_return", "gross_pnl", "commission",
    "realized_pnl", "slippage", "mfe", "mae", "bars_held", "holding_seconds", "result",
    "rationale", "features",
    # extended learning attribution (§1)
    "underlying", "agent", "signal_action", "signal_strength", "expected_risk",
    "stop_price", "target_price", "financing_cost", "strategy_version",
]


class SQLiteJournal(TradeJournal):
    """Durable journal backed by SQLite. `path=":memory:"` for an ephemeral DB."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                trade_id        TEXT PRIMARY KEY,
                instrument_key  TEXT NOT NULL,
                asset_class     TEXT NOT NULL,
                direction       TEXT NOT NULL,
                strategy        TEXT NOT NULL,
                regime          TEXT NOT NULL,
                model_version   TEXT NOT NULL,
                entry_ts        TEXT NOT NULL,
                exit_ts         TEXT NOT NULL,
                quantity        REAL NOT NULL,
                entry_price     REAL NOT NULL,
                exit_price      REAL NOT NULL,
                confidence      REAL NOT NULL,
                expected_return REAL NOT NULL,
                realized_return REAL NOT NULL,
                gross_pnl       REAL NOT NULL,
                commission      REAL NOT NULL,
                realized_pnl    REAL NOT NULL,
                slippage        REAL NOT NULL,
                mfe             REAL NOT NULL,
                mae             REAL NOT NULL,
                bars_held       INTEGER NOT NULL,
                holding_seconds REAL NOT NULL,
                result          TEXT NOT NULL,
                rationale       TEXT NOT NULL DEFAULT '',
                features        TEXT NOT NULL DEFAULT '{}',
                underlying      TEXT NOT NULL DEFAULT '',
                agent           TEXT NOT NULL DEFAULT '',
                signal_action   TEXT NOT NULL DEFAULT '',
                signal_strength REAL NOT NULL DEFAULT 0,
                expected_risk   REAL NOT NULL DEFAULT 0,
                stop_price      REAL NOT NULL DEFAULT 0,
                target_price    REAL NOT NULL DEFAULT 0,
                financing_cost  REAL NOT NULL DEFAULT 0,
                strategy_version TEXT NOT NULL DEFAULT 'v0'
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_trades_strategy ON trades(strategy)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_trades_regime ON trades(regime)")
        self._conn.commit()

    def record(self, trade: TradeRecord) -> None:
        values = (
            trade.trade_id, trade.instrument_key, trade.asset_class, trade.direction,
            trade.strategy, trade.regime, trade.model_version,
            trade.entry_ts.isoformat(), trade.exit_ts.isoformat(),
            trade.quantity, trade.entry_price, trade.exit_price,
            trade.confidence, trade.expected_return, trade.realized_return,
            trade.gross_pnl, trade.commission, trade.realized_pnl, trade.slippage,
            trade.mfe, trade.mae, trade.bars_held, trade.holding_seconds,
            trade.result.value, trade.rationale, json.dumps(trade.features),
            trade.underlying, trade.agent, trade.signal_action, trade.signal_strength,
            trade.expected_risk, trade.stop_price, trade.target_price, trade.financing_cost,
            trade.strategy_version,
        )
        placeholders = ", ".join("?" * len(_COLUMNS))
        self._conn.execute(
            f"INSERT OR REPLACE INTO trades ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()

    def all(self) -> list[TradeRecord]:
        rows = self._conn.execute("SELECT * FROM trades ORDER BY exit_ts").fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(r: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            trade_id=r["trade_id"], instrument_key=r["instrument_key"], asset_class=r["asset_class"],
            direction=r["direction"], strategy=r["strategy"], regime=r["regime"],
            model_version=r["model_version"],
            entry_ts=datetime.fromisoformat(r["entry_ts"]), exit_ts=datetime.fromisoformat(r["exit_ts"]),
            quantity=r["quantity"], entry_price=r["entry_price"], exit_price=r["exit_price"],
            confidence=r["confidence"], expected_return=r["expected_return"],
            realized_return=r["realized_return"], gross_pnl=r["gross_pnl"], commission=r["commission"],
            realized_pnl=r["realized_pnl"], slippage=r["slippage"], mfe=r["mfe"], mae=r["mae"],
            bars_held=r["bars_held"], holding_seconds=r["holding_seconds"],
            result=TradeResult(r["result"]), rationale=r["rationale"], features=json.loads(r["features"]),
            underlying=r["underlying"], agent=r["agent"], signal_action=r["signal_action"],
            signal_strength=r["signal_strength"], expected_risk=r["expected_risk"],
            stop_price=r["stop_price"], target_price=r["target_price"],
            financing_cost=r["financing_cost"], strategy_version=r["strategy_version"],
        )

    def close(self) -> None:
        self._conn.close()
