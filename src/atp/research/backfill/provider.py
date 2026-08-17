"""§ R3.0A — historical 1-minute aggregate provider (MASSIVE = Polygon.io).

A read-only market-DATA client — it NEVER creates, submits, or routes an order and never touches the
broker/execution path. `PolygonAggregatesProvider` calls Polygon's REST aggregates
(/v2/aggs/ticker/{sym}/range/1/minute/{from}/{to}?adjusted=true) with the MASSIVE_API_KEY as an
`Authorization: Bearer` header (never in the URL, never logged), cursor-paginating via `next_url` with a
rate-limit delay and bounded retry/backoff. `MockAggregatesProvider` serves fixtures so the pipeline is
fully testable without any live/paid request. Tests use the mock; the real client is exercised only by
the single authorized entitlement probe and (later, after separate approval) the bounded backfill.
"""
from __future__ import annotations

import abc
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .normalize import MinuteBar


class EntitlementError(Exception):
    code = "PROVIDER_ENTITLEMENT_UNAVAILABLE"


class ProviderError(Exception):
    code = "PROVIDER_ERROR"


@dataclass(slots=True)
class FetchResult:
    minutes: list[MinuteBar]
    adjusted: bool                     # the `adjusted` flag the provider actually returned
    pages: list[dict] = field(default_factory=list)   # raw page payloads (for the raw-pages checksum)
    request_ids: list[str] = field(default_factory=list)


class MinuteAggregatesProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def configured(self) -> bool: ...

    @abc.abstractmethod
    def probe(self, symbol: str, day: str) -> dict: ...

    @abc.abstractmethod
    def fetch_minutes(self, symbol: str, start_date: str, end_date: str, *, adjusted: bool = True) -> FetchResult: ...


def _mb(r: dict) -> MinuteBar:
    return MinuteBar(
        ts=datetime.fromtimestamp(int(r["t"]) / 1000.0, tz=timezone.utc),
        open=Decimal(str(r["o"])), high=Decimal(str(r["h"])), low=Decimal(str(r["l"])),
        close=Decimal(str(r["c"])), volume=Decimal(str(r["v"])),
        trade_count=(int(r["n"]) if r.get("n") is not None else None))


class PolygonAggregatesProvider(MinuteAggregatesProvider):
    def __init__(self, api_key: str | None, *, base_url: str = "https://api.polygon.io",
                 delay_s: float = 0.25, max_retries: int = 4, timeout_s: float = 20.0,
                 max_pages: int = 200) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._delay, self._retries, self._timeout, self._max_pages = delay_s, max_retries, timeout_s, max_pages

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _get(self, url: str) -> dict:
        """One authenticated GET with the key in the Authorization header only (never in the URL/logs)."""
        backoff = self._delay
        for attempt in range(self._retries + 1):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._key}",
                                                       "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise EntitlementError(f"HTTP {e.code}") from e
                if e.code == 429 or 500 <= e.code < 600:
                    if attempt >= self._retries:
                        raise ProviderError(f"HTTP {e.code} after {attempt + 1} tries") from e
                    time.sleep(backoff); backoff *= 2; continue
                raise ProviderError(f"HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt >= self._retries:
                    raise ProviderError(str(e)) from e
                time.sleep(backoff); backoff *= 2
        raise ProviderError("exhausted retries")

    def probe(self, symbol: str, day: str) -> dict:
        """One minimal, read-only entitlement probe (one symbol, one completed day, 1-minute, adjusted).
        Records status/entitlement metadata ONLY — never the key. Does NOT create a dataset or backfill."""
        if not self.configured:
            return {"configured": False, "entitled": False, "http_status": None,
                    "reason": "MASSIVE_API_KEY not set"}
        url = (f"{self._base}/v2/aggs/ticker/{symbol}/range/1/minute/{day}/{day}"
               f"?adjusted=true&sort=asc&limit=50")
        try:
            body = self._get(url)
        except EntitlementError as e:
            return {"configured": True, "entitled": False, "http_status": 401, "reason": str(e)}
        except ProviderError as e:
            return {"configured": True, "entitled": False, "http_status": None, "reason": str(e)}
        results = body.get("results") or []
        return {"configured": True, "entitled": (body.get("status") in ("OK", "DELAYED")) and len(results) >= 0,
                "http_status": 200, "result_count": len(results),
                "returned_adjusted": bool(body.get("adjusted")), "has_pagination": bool(body.get("next_url")),
                "request_id": body.get("request_id"), "reason": None}

    def fetch_minutes(self, symbol: str, start_date: str, end_date: str, *, adjusted: bool = True) -> FetchResult:
        url = (f"{self._base}/v2/aggs/ticker/{symbol}/range/1/minute/{start_date}/{end_date}"
               f"?adjusted={'true' if adjusted else 'false'}&sort=asc&limit=50000")
        minutes: list[MinuteBar] = []
        pages: list[dict] = []
        request_ids: list[str] = []
        provider_adjusted = None
        for _ in range(self._max_pages):
            body = self._get(url)
            if provider_adjusted is None:
                provider_adjusted = bool(body.get("adjusted"))
            elif bool(body.get("adjusted")) != provider_adjusted:
                raise ProviderError("provider mixed adjusted flags across pages")
            res = body.get("results") or []
            pages.append({"results": res, "adjusted": body.get("adjusted"), "count": len(res)})
            request_ids.append(body.get("request_id") or "")
            minutes.extend(_mb(r) for r in res)
            nxt = body.get("next_url")
            if not nxt:
                break
            url = nxt
            time.sleep(self._delay)
        return FetchResult(minutes=minutes, adjusted=bool(provider_adjusted), pages=pages, request_ids=request_ids)


class MockAggregatesProvider(MinuteAggregatesProvider):
    """Deterministic fixture provider for tests. `minutes_by_symbol` maps symbol → list[MinuteBar]; `pages`
    optionally splits a symbol's minutes into multiple raw pages to exercise pagination + page checksums."""

    def __init__(self, minutes_by_symbol: dict[str, list[MinuteBar]], *, adjusted: bool = True,
                 page_size: int | None = None, probe_result: dict | None = None) -> None:
        self._m = minutes_by_symbol
        self._adjusted = adjusted
        self._page_size = page_size
        self._probe = probe_result

    @property
    def configured(self) -> bool:
        return True

    def probe(self, symbol: str, day: str) -> dict:
        return self._probe or {"configured": True, "entitled": True, "http_status": 200, "result_count": 1,
                               "returned_adjusted": self._adjusted, "has_pagination": False,
                               "request_id": "mock-req", "reason": None}

    def fetch_minutes(self, symbol: str, start_date: str, end_date: str, *, adjusted: bool = True) -> FetchResult:
        mins = list(self._m.get(symbol, []))
        ps = self._page_size or (len(mins) or 1)
        pages = [{"results": [{"t": int(m.ts.timestamp() * 1000), "o": str(m.open), "h": str(m.high),
                               "l": str(m.low), "c": str(m.close), "v": str(m.volume), "n": m.trade_count}
                              for m in mins[i:i + ps]], "adjusted": self._adjusted}
                 for i in range(0, max(1, len(mins)), ps)]
        return FetchResult(minutes=mins, adjusted=self._adjusted, pages=pages,
                           request_ids=[f"mock-{i}" for i in range(len(pages))])
