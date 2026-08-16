"""News collector (§ Phase G2.1). Fetches real articles from the provider, runs the deterministic
sentiment/impact analysis on the real text, and upserts them into PostgreSQL (news_items).

Idempotent: each item's `id` is a deterministic hash of (symbol, url) so re-collecting the same
article never duplicates it (survives service restarts). Persists ONLY article fields + derived
sentiment/impact — never the provider key. If the store write fails it raises → the service reports
DEGRADED (fail-closed); nothing is fabricated.
"""
from __future__ import annotations

import hashlib

from .analysis import analyze_sentiment, classify_impact
from .provider import NewsArticle


def news_id(symbol: str, key: str) -> str:
    """Deterministic id for an article → idempotent upserts (no duplicate on re-ingest / restart)."""
    return hashlib.sha1(f"{symbol.upper()}|{key}".encode("utf-8")).hexdigest()


class NewsCollector:
    def __init__(self, store, provider) -> None:
        self.store = store
        self.provider = provider

    def collect(self, symbol: str, limit: int = 20) -> int:
        """Fetch → analyze → persist for one symbol. Returns the number of items ingested. Raises on a
        store failure so the caller can fail closed."""
        sym = symbol.upper()
        ingested = 0
        for a in self.provider.fetch(sym, limit):
            n = self._persist(sym, a)
            ingested += n
        return ingested

    def _persist(self, symbol: str, a: NewsArticle) -> int:
        if not a.title or not a.published_at:
            return 0                                      # incomplete → skip (never patch with fakes)
        text = f"{a.title} {a.summary or ''}"
        score, _label = analyze_sentiment(text, a.provider_sentiment)
        impact = classify_impact(text)
        nid = news_id(symbol, a.url or a.external_id or a.title)
        self.store.upsert_news_item(
            id=nid, symbol=symbol, title=a.title, source=a.source, url=a.url,
            published_at=a.published_at, content_summary=a.summary,
            sentiment_score=score, impact_level=impact)
        return 1
