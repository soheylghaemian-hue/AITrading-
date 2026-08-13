"""Schema migrations (§ Phase B).

Versioned, ordered migrations applied by `Migrator` and tracked in `schema_migrations` — the schema is
NEVER created by hand. One DDL template serves both dialects: money is NUMERIC(20,8) in PostgreSQL and
TEXT (canonical decimal) in SQLite; timestamps are ISO-8601 UTC TEXT; booleans are INTEGER 0/1 in both
so the shared SQL layer passes identical parameters.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _types(dialect: str) -> dict:
    money = "NUMERIC(20,8)" if dialect == "postgres" else "TEXT"
    return {"MONEY": money, "TS": "TEXT", "BOOL": "INTEGER", "TXT": "TEXT", "INT": "INTEGER"}


def _statements(dialect: str) -> list[str]:
    t = _types(dialect)
    m, ts, b, txt = t["MONEY"], t["TS"], t["BOOL"], t["TXT"]
    return [
        f"""CREATE TABLE IF NOT EXISTS accounts (
            account_id {txt} PRIMARY KEY, broker {txt} NOT NULL, base_currency {txt} NOT NULL,
            cash {m} NOT NULL, equity {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS runtime_state (
            id {b} PRIMARY KEY, status {txt} NOT NULL, updated_at {ts} NOT NULL,
            correlation_id {txt}, reason {txt})""",
        f"""CREATE TABLE IF NOT EXISTS risk_config (
            id {b} PRIMARY KEY, capital {m} NOT NULL, risk_per_trade_pct {m} NOT NULL,
            max_daily_loss_pct {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS risk_state (
            id {b} PRIMARY KEY, day_start_equity {m} NOT NULL, peak_equity {m} NOT NULL,
            halted {b} NOT NULL DEFAULT 0, killed {b} NOT NULL DEFAULT 0, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS kill_switch (
            id {b} PRIMARY KEY, engaged {b} NOT NULL DEFAULT 0, actor {txt}, reason {txt}, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS daily_pnl (
            trade_date {txt} PRIMARY KEY, day_start_equity {m} NOT NULL, realized_pnl {m} NOT NULL,
            unrealized_pnl {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS daily_loss_lock (
            trade_date {txt} PRIMARY KEY, engaged {b} NOT NULL DEFAULT 0, reason {txt}, updated_at {ts})""",
        f"""CREATE TABLE IF NOT EXISTS positions (
            instrument {txt} PRIMARY KEY, quantity {m} NOT NULL, avg_price {m} NOT NULL,
            realized_pnl {m} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS orders (
            client_order_id {txt} PRIMARY KEY, idempotency_key {txt} NOT NULL UNIQUE, instrument {txt} NOT NULL,
            side {txt} NOT NULL, quantity {m} NOT NULL, order_type {txt} NOT NULL, state {txt} NOT NULL,
            broker_order_id {txt}, correlation_id {txt}, reason {txt},
            created_at {ts} NOT NULL, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS fills (
            fill_id {txt} PRIMARY KEY, client_order_id {txt} NOT NULL, instrument {txt} NOT NULL,
            side {txt} NOT NULL, quantity {m} NOT NULL, price {m} NOT NULL, commission {m} NOT NULL,
            ts {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS trades (
            trade_id {txt} PRIMARY KEY, instrument {txt} NOT NULL, direction {txt} NOT NULL,
            quantity {m} NOT NULL, entry_price {m} NOT NULL, exit_price {m}, realized_pnl {m},
            opened_at {ts} NOT NULL, closed_at {ts}, agent {txt}, payload {txt})""",
        f"""CREATE TABLE IF NOT EXISTS decisions (
            decision_id {txt} PRIMARY KEY, ts {ts} NOT NULL, instrument {txt} NOT NULL,
            final_decision {txt}, payload {txt}, correlation_id {txt})""",
        f"""CREATE TABLE IF NOT EXISTS audit_events (
            event_id {txt} PRIMARY KEY, ts {ts} NOT NULL, actor {txt} NOT NULL, action {txt} NOT NULL,
            previous_state {txt}, new_state {txt}, reason {txt}, correlation_id {txt})""",
        f"""CREATE TABLE IF NOT EXISTS service_heartbeats (
            service {txt} PRIMARY KEY, status {txt} NOT NULL, detail {txt}, updated_at {ts} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS market_data_health (
            symbol {txt} PRIMARY KEY, source {txt} NOT NULL, status {txt} NOT NULL,
            latency_ms {t['INT']}, updated_at {ts} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_orders_state ON orders(state)",
        "CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(client_order_id)",
        "CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_events(ts)",
        "CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts)",
    ]


# (version, name, builder) — append new migrations, never edit an applied one.
MIGRATIONS = [
    (1, "initial_schema", _statements),
]


class Migrator:
    """Applies pending migrations inside transactions; records them in schema_migrations."""

    def __init__(self, store, dialect: str):
        self._store = store
        self._dialect = dialect

    def _applied(self) -> set[int]:
        try:
            rows = self._store._all("SELECT version FROM schema_migrations")
            return {int(r[0]) for r in rows}
        except Exception:
            return set()

    def apply(self) -> list[int]:
        t = _types(self._dialect)
        with self._store.tx() as cur:
            self._store._exec(cur,
                f"CREATE TABLE IF NOT EXISTS schema_migrations (version {t['INT']} PRIMARY KEY, "
                f"name {t['TXT']} NOT NULL, applied_at {t['TS']} NOT NULL)")
        done = self._applied()
        newly: list[int] = []
        for version, name, builder in sorted(MIGRATIONS):
            if version in done:
                continue
            stmts = builder(self._dialect)
            with self._store.tx() as cur:
                for s in stmts:
                    self._store._exec(cur, s)
                self._store._exec(cur,
                    "INSERT INTO schema_migrations (version,name,applied_at) VALUES (?,?,?)",
                    (version, name, datetime.now(timezone.utc).isoformat()))
            newly.append(version)
        return newly
