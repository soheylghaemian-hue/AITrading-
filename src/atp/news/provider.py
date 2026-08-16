"""News provider (§ Phase G2.1) — real headlines from Massive/Polygon's news REST endpoint.

Massive == Polygon.io, so market news comes from GET /v2/reference/news?ticker=… using the existing
MASSIVE_API_KEY (sent as an Authorization: Bearer header — never in the URL, never logged, never
persisted). If no key is configured, or the fetch fails, or the plan lacks news entitlement, the
provider returns [] → nothing is persisted → the terminal shows NO DATA. Never fabricated.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(slots=True)
class NewsArticle:
    title: str
    url: str | None
    source: str | None
    published_at: str
    summary: str | None
    provider_sentiment: str | None   # positive/negative/neutral from Polygon `insights` (real), else None
    external_id: str | None


def parse_polygon_news(payload: dict | None, symbol: str) -> list[NewsArticle]:
    """Pure parser for a Polygon /v2/reference/news response → NewsArticle list. Drops items without a
    title. Extracts the provider's per-ticker sentiment from `insights` when present (a real signal)."""
    out: list[NewsArticle] = []
    for r in ((payload or {}).get("results") or []):
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        if not title:
            continue
        pub = r.get("publisher") or {}
        psent = None
        for ins in (r.get("insights") or []):
            if isinstance(ins, dict) and (ins.get("ticker") or "").upper() == symbol.upper():
                s = (ins.get("sentiment") or "").lower()
                if s in ("positive", "negative", "neutral"):
                    psent = s
                break
        out.append(NewsArticle(
            title=title,
            url=r.get("article_url") or r.get("amp_url"),
            source=(pub.get("name") if isinstance(pub, dict) else None),
            published_at=(r.get("published_utc") or "").strip(),
            summary=(r.get("description") or None),
            provider_sentiment=psent,
            external_id=r.get("id"),
        ))
    return out


class PolygonNewsProvider:
    """Fetches real news for a symbol. Read-only HTTP GET; no order/trade/IBKR access of any kind."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *, timeout: float = 10.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("MASSIVE_API_KEY")
        self._base = (base_url or os.environ.get("NEWS_API_URL") or "https://api.polygon.io").rstrip("/")
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def fetch(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        if not self._api_key:
            return []                                     # no key → NO DATA (never fabricate)
        n = max(1, min(50, int(limit)))
        q = urlencode({"ticker": symbol.upper(), "order": "desc", "sort": "published_utc", "limit": n})
        url = f"{self._base}/v2/reference/news?{q}"
        try:
            req = Request(url, headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",   # key in header, never in the URL/logs
            })
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []                                     # fetch/entitlement failure → NO DATA (fail-closed)
        return parse_polygon_news(payload, symbol)
