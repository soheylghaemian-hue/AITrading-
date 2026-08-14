"""Event bus for inter-service messaging (§ Phase C).

Redis pub/sub is the transport. It is a BUS/CACHE ONLY and is NEVER authoritative for trading state
— all durable state lives in PostgreSQL (``atp.store``). If Redis is unavailable, no authoritative
state is lost: publishers degrade (quotes are simply not delivered) and the Trading Core fails closed
via stale market-data health. An in-memory bus backs tests and single-process runs.

Payloads are plain JSON-serialisable dicts (e.g. ``NormalizedQuote.as_dict()``).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class Bus:
    """Abstract transport. Implementations must be safe to use from one asyncio loop."""

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        raise NotImplementedError

    def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator yielding events published to ``channel`` after subscription."""
        raise NotImplementedError

    async def close(self) -> None:
        pass


class InMemoryBus(Bus):
    """Process-local bus for tests / single-process runs. Mirrors Redis JSON round-trip semantics."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = {}

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        payload = json.loads(json.dumps(event))          # match the Redis serialise/parse round-trip
        for q in list(self._subs.get(channel, [])):
            q.put_nowait(payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(channel, []).append(q)
        try:
            while True:
                yield await q.get()
        finally:
            try:
                self._subs[channel].remove(q)
            except ValueError:
                pass


class RedisBus(Bus):
    """Redis pub/sub transport (``redis`` is imported lazily so tests don't need it).

    Bus/cache only. A Redis outage surfaces as an exception to the caller, which must degrade — it
    must never be treated as authoritative state loss.
    """

    def __init__(self, url: str, *, namespace: str = "atp") -> None:
        import redis.asyncio as aioredis          # lazy: only when a live bus is actually used
        self._r = aioredis.from_url(url, decode_responses=True)
        self._ns = namespace

    def _chan(self, channel: str) -> str:
        return f"{self._ns}:{channel}"

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        await self._r.publish(self._chan(channel), json.dumps(event))

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._r.pubsub()
        await pubsub.subscribe(self._chan(channel))
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield json.loads(msg["data"])
        finally:
            try:
                await pubsub.unsubscribe(self._chan(channel))
            finally:
                aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                if aclose is not None:
                    res = aclose()
                    if asyncio.iscoroutine(res):
                        await res

    async def close(self) -> None:
        aclose = getattr(self._r, "aclose", None) or getattr(self._r, "close", None)
        if aclose is not None:
            res = aclose()
            if asyncio.iscoroutine(res):
                await res


def open_bus(url: str | None = None, **kw) -> Bus:
    """Return a ``RedisBus`` when a URL is given, otherwise an ``InMemoryBus`` (tests/single-process)."""
    return RedisBus(url, **kw) if url else InMemoryBus()
