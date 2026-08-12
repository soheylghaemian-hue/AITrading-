"""Feed hub (§5/§17): refresh a set of context feeds together.

Holds the context feeds and refreshes them on demand — the live runner calls it periodically so
options/rates/events state stays current alongside the market data. One place to register every
data source the desk depends on.
"""

from __future__ import annotations

from datetime import datetime

from ..logging_config import get_logger
from .base import ContextFeed

log = get_logger("feeds")


class FeedHub:
    def __init__(self, feeds: list[ContextFeed] | None = None) -> None:
        self._feeds = list(feeds or [])

    def add(self, feed: ContextFeed) -> None:
        self._feeds.append(feed)

    @property
    def feeds(self) -> list[ContextFeed]:
        return list(self._feeds)

    async def refresh_all(self, now: datetime) -> dict[str, int]:
        """Refresh every feed; return {feed_name: updates_applied}."""
        out: dict[str, int] = {}
        for feed in self._feeds:
            try:
                out[feed.name] = await feed.refresh(now)
            except Exception as exc:  # noqa: BLE001 — one bad feed must not stop the others
                log.error("feed %s refresh failed: %r", feed.name, exc)
                out[feed.name] = 0
        return out
