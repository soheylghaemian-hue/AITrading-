"""SQLite backend (§ Phase B) — local/test durable store. File-backed, WAL, synchronous=FULL so a
committed safety-critical write survives a process restart. Money is stored as canonical TEXT."""

from __future__ import annotations

import sqlite3

from .base import SqlStore
from .schema import Migrator


class SqliteStore(SqlStore):
    PLACEHOLDER = "?"
    MONEY_AS_TEXT = True

    def __init__(self, path: str):
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")   # durability for safety-critical commits
        conn.execute("PRAGMA foreign_keys=ON")
        super().__init__(conn)


def open_sqlite(path: str, *, migrate: bool = True) -> SqliteStore:
    store = SqliteStore(path)
    if migrate:
        Migrator(store, "sqlite").apply()
    return store
