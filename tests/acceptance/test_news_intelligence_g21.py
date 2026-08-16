"""Phase G2.1 — News Intelligence Layer (read-only).

Covers: news ingestion, database persistence, empty state, API response shape, no secrets (the
provider key never lands in the URL or the persisted rows), and restart durability. Touches no
Trading Core / Risk / Broker / IBKR / Execution / OHLC code.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from atp.news.analysis import analyze_sentiment, classify_impact, sentiment_label
from atp.news.collector import NewsCollector
from atp.news.provider import NewsArticle, PolygonNewsProvider, parse_polygon_news
from atp.store import NewsItemRow, open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates news_items (migration 4)


class FakeProvider:
    def __init__(self, articles): self._articles = articles
    @property
    def configured(self): return True
    def fetch(self, symbol, limit=20): return list(self._articles)


def _article(title, url, published, summary=None, sent=None):
    return NewsArticle(title=title, url=url, source="MarketWatch", published_at=published,
                       summary=summary, provider_sentiment=sent, external_id=None)


POLYGON_SAMPLE = {
    "status": "OK", "count": 2,
    "results": [
        {"id": "a", "publisher": {"name": "MarketWatch"}, "title": "NVDA earnings beat, stock surges",
         "article_url": "https://ex.com/a", "published_utc": "2026-08-16T10:00:00Z",
         "description": "Strong quarter", "tickers": ["NVDA"],
         "insights": [{"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "beat"}]},
        {"id": "b", "publisher": {"name": "Reuters"}, "title": "Regulator opens probe into chipmaker",
         "article_url": "https://ex.com/b", "published_utc": "2026-08-16T09:00:00Z",
         "description": "investigation lawsuit", "tickers": ["NVDA"]},
        {"publisher": {}, "title": "", "article_url": "https://ex.com/c", "published_utc": "x"},  # dropped: no title
    ],
}


def test_parse_polygon_news_extracts_real_fields_and_provider_sentiment():
    arts = parse_polygon_news(POLYGON_SAMPLE, "NVDA")
    assert len(arts) == 2                                  # empty-title item dropped
    assert arts[0].title == "NVDA earnings beat, stock surges"
    assert arts[0].url == "https://ex.com/a" and arts[0].source == "MarketWatch"
    assert arts[0].provider_sentiment == "positive"       # real per-ticker signal
    assert arts[1].provider_sentiment is None             # no insights → None (not fabricated)
    assert parse_polygon_news({}, "NVDA") == []           # empty payload → NO DATA


def test_sentiment_and_impact_are_deterministic_transforms():
    assert analyze_sentiment("anything", "positive") == (0.6, "positive")   # provider label wins
    assert analyze_sentiment("earnings beat and surge") [1] == "positive"   # lexicon on real text
    assert analyze_sentiment("probe lawsuit and plunge")[1] == "negative"
    assert analyze_sentiment("company holds annual meeting") == (0.0, "neutral")  # no signal → neutral
    assert classify_impact("Q3 earnings guidance") == "HIGH"
    assert classify_impact("analyst price target revenue") == "MEDIUM"
    assert classify_impact("store opens downtown") == "LOW"
    assert sentiment_label(0.6) == "positive" and sentiment_label(-0.6) == "negative"
    assert sentiment_label(0.0) == "neutral" and sentiment_label(None) is None


def test_ingestion_persists_and_is_idempotent(store):
    articles = [
        _article("NVDA earnings beat, surges", "https://ex.com/a", "2026-08-16T10:00:00Z", "strong", "positive"),
        _article("Regulator probe lawsuit", "https://ex.com/b", "2026-08-16T09:00:00Z", "investigation"),
    ]
    coll = NewsCollector(store, FakeProvider(articles))
    assert coll.collect("NVDA", 20) == 2
    rows = store.list_news("NVDA")
    assert [r.title for r in rows] == ["NVDA earnings beat, surges", "Regulator probe lawsuit"]  # newest first
    assert rows[0].sentiment_score == 0.6 and rows[0].impact_level == "HIGH"
    assert isinstance(rows[0], NewsItemRow)
    # re-collect the same articles → upsert, never duplicate (restart-safe)
    coll.collect("NVDA", 20)
    assert store.count_news("NVDA") == 2


def test_empty_state_persists_nothing(store):
    assert NewsCollector(store, FakeProvider([])).collect("AAPL") == 0
    assert store.list_news("AAPL") == []
    assert store.count_news("AAPL") == 0


def test_no_secret_in_persisted_rows(store):
    NewsCollector(store, FakeProvider([_article("t", "https://ex.com/a", "2026-08-16T10:00:00Z")])).collect("NVDA")
    blob = json.dumps([asdict(r) for r in store.list_news("NVDA")]).lower()
    for secret in ("apikey", "api_key", "bearer", "authorization", "massive_api_key"):
        assert secret not in blob


def test_provider_never_puts_the_key_in_the_url(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"results": []}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr("atp.news.provider.urlopen", fake_urlopen)
    PolygonNewsProvider(api_key="SECRETKEY123").fetch("NVDA", 5)
    assert "SECRETKEY123" not in captured["url"]           # never in the URL/logs
    assert captured["auth"] == "Bearer SECRETKEY123"        # only in the header


def test_unconfigured_provider_yields_no_data():
    assert PolygonNewsProvider(api_key="").configured is False
    assert PolygonNewsProvider(api_key="").fetch("NVDA", 5) == []


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    NewsCollector(s1, FakeProvider([_article("Durable headline", "https://ex.com/a", "2026-08-16T10:00:00Z", "earnings")])).collect("NVDA")
    s1.close()
    s2 = open_store(path)                                  # "restart": reopen the durable store
    rows = s2.list_news("NVDA")
    assert len(rows) == 1 and rows[0].title == "Durable headline" and rows[0].impact_level == "HIGH"


def test_api_item_shape(store):
    NewsCollector(store, FakeProvider([
        _article("NVDA earnings beat", "https://ex.com/a", "2026-08-16T10:00:00Z", "strong", "positive")])).collect("NVDA")
    r = store.list_news("NVDA")[0]
    item = {"id": r.id, "symbol": r.symbol, "title": r.title, "source": r.source, "url": r.url,
            "published_at": r.published_at, "summary": r.content_summary,
            "sentiment_score": r.sentiment_score, "sentiment": sentiment_label(r.sentiment_score),
            "impact": r.impact_level}
    assert item["symbol"] == "NVDA" and item["sentiment"] == "positive" and item["impact"] == "HIGH"
    assert set(item) >= {"title", "source", "published_at", "sentiment", "impact", "url"}
