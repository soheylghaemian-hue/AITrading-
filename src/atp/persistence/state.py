"""Realtime state store (§21 "Realtime State: Redis").

Fast key/value state the desk touches every cycle — latest equity, open positions, current
regime, strategy status, last heartbeat — kept behind an interface so the backend is swappable.
`InMemoryStateStore` (tested) is used offline and in single-process runs; `RedisStateStore`
(lazy `redis`) shares that state across processes/dashboards in production. Values are JSON-
serialized, so both backends store the same shapes.

This is *operational* state (fast, ephemeral, overwritten each cycle) — distinct from the
durable trade journal (§11), which is the append-only system of record.
"""

from __future__ import annotations

import abc
import json
from typing import Any


class StateStore(abc.ABC):
    @abc.abstractmethod
    def set(self, key: str, value: Any) -> None: ...

    @abc.abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def keys(self, prefix: str = "") -> list[str]: ...

    # Convenience helpers shared by all backends.
    def set_many(self, mapping: dict[str, Any]) -> None:
        for k, v in mapping.items():
            self.set(k, v)

    def get_all(self, prefix: str = "") -> dict[str, Any]:
        return {k: self.get(k) for k in self.keys(prefix)}


class InMemoryStateStore(StateStore):
    """Process-local store. Serializes through JSON so it matches Redis semantics exactly
    (e.g. tuples come back as lists), catching shape bugs offline."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, key: str, value: Any) -> None:
        self._data[key] = json.dumps(value)

    def get(self, key: str) -> Any | None:
        raw = self._data.get(key)
        return json.loads(raw) if raw is not None else None

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))


class RedisStateStore(StateStore):
    """Redis-backed store (§21). `redis` is lazy-imported; live-only. Same JSON shapes as
    `InMemoryStateStore`, so swapping backends changes nothing for callers."""

    def __init__(self, url: str = "redis://localhost:6379/0", *, namespace: str = "atp", client: Any = None) -> None:
        self._ns = namespace
        if client is not None:
            self._r = client
        else:
            import redis  # noqa: PLC0415 — lazy; only needed for a live connection

            self._r = redis.Redis.from_url(url, decode_responses=True)

    def _k(self, key: str) -> str:
        return f"{self._ns}:{key}"

    def set(self, key: str, value: Any) -> None:
        self._r.set(self._k(key), json.dumps(value))

    def get(self, key: str) -> Any | None:
        raw = self._r.get(self._k(key))
        return json.loads(raw) if raw is not None else None

    def delete(self, key: str) -> None:
        self._r.delete(self._k(key))

    def keys(self, prefix: str = "") -> list[str]:
        pattern = f"{self._ns}:{prefix}*"
        cut = len(self._ns) + 1
        return sorted(k[cut:] for k in self._r.scan_iter(match=pattern))
