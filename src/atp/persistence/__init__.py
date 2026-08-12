"""Persistence (§21): realtime state store (Redis) and durable journal (Postgres).

In-memory/SQLite backends are dependency-free and tested; Redis/Postgres adapters lazy-import
their drivers and implement the same interfaces (`StateStore`, `TradeJournal`)."""

from .state import InMemoryStateStore, RedisStateStore, StateStore

__all__ = ["StateStore", "InMemoryStateStore", "RedisStateStore"]
