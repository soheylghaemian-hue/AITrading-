"""PostgreSQL backend (§ Phase B) — the PRODUCTION source of truth. psycopg is lazy-imported so the
package (and the SQLite test path) load without it. Money is stored as NUMERIC and round-trips as
exact Decimal; the same migrations run via the Migrator with the postgres dialect."""

from __future__ import annotations

from .base import SqlStore
from .schema import Migrator


class PostgresStore(SqlStore):
    PLACEHOLDER = "%s"
    MONEY_AS_TEXT = False          # NUMERIC columns — psycopg adapts Decimal exactly

    def __init__(self, dsn: str):
        import psycopg  # noqa: PLC0415 — lazy; only needed for a live connection
        conn = psycopg.connect(dsn, autocommit=False)
        super().__init__(conn)


def open_postgres(dsn: str, *, migrate: bool = True) -> PostgresStore:
    store = PostgresStore(dsn)
    if migrate:
        Migrator(store, "postgres").apply()
    return store
