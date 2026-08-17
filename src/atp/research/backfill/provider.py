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
from urllib.parse import urlparse

from .. import calendars as _cal
from .normalize import MinuteBar


class EntitlementError(Exception):
    code = "PROVIDER_ENTITLEMENT_UNAVAILABLE"


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str = "PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


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

    def _validate_page_url(self, url: str) -> None:
        """A provider `next_url` is attacker-influenced data. BEFORE we follow it and attach the API key,
        require HTTPS and the SAME configured provider origin (host + port). Reject cross-origin, downgraded
        (http) or malformed URLs so the credential is never sent anywhere but the configured provider."""
        try:
            u, base = urlparse(url), urlparse(self._base)
        except ValueError as e:
            raise ProviderError(f"malformed next_url: {e}", code="PROVIDER_UNSAFE_PAGE_URL") from e
        if u.scheme != "https":
            raise ProviderError("next_url is not HTTPS (refusing to attach credential)",
                                code="PROVIDER_UNSAFE_PAGE_URL")
        if not u.hostname or (u.hostname or "").lower() != (base.hostname or "").lower():
            raise ProviderError("next_url host is not the configured provider origin",
                                code="PROVIDER_UNSAFE_PAGE_URL")
        if (u.port or 443) != (base.port or 443):
            raise ProviderError("next_url port is not the configured provider origin",
                                code="PROVIDER_UNSAFE_PAGE_URL")

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

    def fetch_minutes(self, symbol: str, start_date: str, end_date: str, *, adjusted: bool = True,
                      max_pages: int | None = None, max_results: int | None = None) -> FetchResult:
        """Fetch one BOUNDED, session-aligned date chunk. Pages via `next_url` up to `max_pages`; if a
        `next_url` still remains at the limit, fail deterministically (PROVIDER_PAGE_LIMIT_EXCEEDED) rather
        than return a truncated result. `max_results` caps the chunk's cumulative rows. Every `next_url` is
        origin/HTTPS-validated before it is followed with the credential."""
        cap_pages = max_pages if max_pages is not None else self._max_pages
        url = (f"{self._base}/v2/aggs/ticker/{symbol}/range/1/minute/{start_date}/{end_date}"
               f"?adjusted={'true' if adjusted else 'false'}&sort=asc&limit=50000")
        minutes: list[MinuteBar] = []
        pages: list[dict] = []
        request_ids: list[str] = []
        provider_adjusted = None
        total = 0
        for _ in range(cap_pages):
            body = self._get(url)
            if provider_adjusted is None:
                provider_adjusted = bool(body.get("adjusted"))
            elif bool(body.get("adjusted")) != provider_adjusted:
                raise ProviderError("provider mixed adjusted flags across pages")
            res = body.get("results") or []
            total += len(res)
            if max_results is not None and total > max_results:
                raise ProviderError(f"chunk exceeded max_results={max_results} for {symbol}",
                                    code="PROVIDER_RESULT_LIMIT_EXCEEDED")
            pages.append({"results": res, "adjusted": body.get("adjusted"), "count": len(res)})
            request_ids.append(body.get("request_id") or "")
            minutes.extend(_mb(r) for r in res)
            nxt = body.get("next_url")
            if not nxt:
                return FetchResult(minutes=minutes, adjusted=bool(provider_adjusted), pages=pages,
                                   request_ids=request_ids)
            self._validate_page_url(nxt)   # validate BEFORE following with the credential
            url = nxt
            time.sleep(self._delay)
        # We only reach here if the last fetched page STILL had a next_url → do not truncate; fail.
        raise ProviderError(f"more than {cap_pages} pages remain for {symbol} {start_date}..{end_date}",
                            code="PROVIDER_PAGE_LIMIT_EXCEEDED")


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

    def fetch_minutes(self, symbol: str, start_date: str, end_date: str, *, adjusted: bool = True,
                      max_pages: int | None = None, max_results: int | None = None) -> FetchResult:
        # Filter to the requested chunk's NY-session-date range so chunked fetching returns ONLY that
        # chunk's minutes (mirrors the real provider's date-bounded request; prevents cross-chunk overlap).
        lo = datetime.fromisoformat(start_date).date()
        hi = datetime.fromisoformat(end_date).date()
        mins = [m for m in self._m.get(symbol, []) if lo <= m.ts.astimezone(_cal.NY).date() <= hi]
        ps = self._page_size or (len(mins) or 1)
        pages = [{"results": [{"t": int(m.ts.timestamp() * 1000), "o": str(m.open), "h": str(m.high),
                               "l": str(m.low), "c": str(m.close), "v": str(m.volume), "n": m.trade_count}
                              for m in mins[i:i + ps]], "adjusted": self._adjusted}
                 for i in range(0, max(1, len(mins)), ps)]
        return FetchResult(minutes=mins, adjusted=self._adjusted, pages=pages,
                           request_ids=[f"mock-{i}" for i in range(len(pages))])
