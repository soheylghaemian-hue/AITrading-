"""Options provider abstraction (§ Phase G2.3).

`OptionsProvider` is the interface every source implements. No hard dependency on one vendor — future
integrations (Massive/Polygon Options, CBOE, ORATS, Tradier, IBKR market data, Unusual Whales) each
register here. A real Massive/Polygon provider is included (option-chain snapshot via the licensed
MASSIVE_API_KEY, sent as an Authorization: Bearer header — never in the URL/logs). Options entitlement
is separate from stocks; if the plan lacks it, or the key is unset, or a fetch fails, methods return
empty → nothing persisted → NO DATA. Never fabricated. No broker/IBKR/execution access, no credentials.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(slots=True)
class OptionContract:
    symbol: str
    expiration_date: str
    strike: float | None
    option_type: str                 # call / put
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None


class OptionsProvider(ABC):
    name: str = "provider"

    @property
    def configured(self) -> bool:
        return False

    @abstractmethod
    def get_option_chain(self, symbol: str) -> list[OptionContract]: ...

    # Convenience aggregates (default: derived from the chain). Providers may override with native calls.
    def get_volume(self, symbol: str) -> dict:
        cs = self.get_option_chain(symbol)
        call = sum((c.volume or 0) for c in cs if c.option_type == "call")
        put = sum((c.volume or 0) for c in cs if c.option_type == "put")
        return {"call": call, "put": put, "total": call + put}

    def get_open_interest(self, symbol: str) -> int:
        return sum((c.open_interest or 0) for c in self.get_option_chain(symbol))

    def get_implied_volatility(self, symbol: str) -> float | None:
        ivs = [c.implied_volatility for c in self.get_option_chain(symbol) if c.implied_volatility is not None]
        return round(sum(ivs) / len(ivs), 4) if ivs else None

    @abstractmethod
    def get_unusual_activity(self, symbol: str) -> dict | None: ...

    def probe(self, symbol: str) -> dict:
        """Read-only entitlement probe (§ R1.1). Reports whether this provider can actually return
        licensed options data, WITHOUT masking the reason and WITHOUT exposing the API key. The default
        (for providers without a live endpoint) is simply 'not entitled'. Never fabricates."""
        return {"configured": bool(self.configured), "http_status": None, "entitled": False,
                "reason": "not_supported", "upstream_status": None, "contracts": 0}


class NullOptionsProvider(OptionsProvider):
    name = "null"

    def get_option_chain(self, symbol): return []
    def get_unusual_activity(self, symbol): return None


def _num(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _int(x) -> int | None:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def parse_polygon_options(payload: dict | None, symbol: str) -> list[OptionContract]:
    """Pure parser for a Polygon /v3/snapshot/options response → OptionContract list. Non-call/put or
    title-less contracts are dropped. No fabrication — missing fields stay None."""
    out: list[OptionContract] = []
    for r in ((payload or {}).get("results") or []):
        if not isinstance(r, dict):
            continue
        det = r.get("details") or {}
        ctype = (det.get("contract_type") or "").lower()
        if ctype not in ("call", "put"):
            continue
        day = r.get("day") or {}
        lq = r.get("last_quote") or {}
        lt = r.get("last_trade") or {}
        out.append(OptionContract(
            symbol=symbol.upper(), expiration_date=(det.get("expiration_date") or ""),
            strike=_num(det.get("strike_price")), option_type=ctype,
            bid=_num(lq.get("bid")), ask=_num(lq.get("ask")),
            last=(_num(lt.get("price")) if lt.get("price") is not None else _num(day.get("close"))),
            volume=_int(day.get("volume")), open_interest=_int(r.get("open_interest")),
            implied_volatility=_num(r.get("implied_volatility"))))
    return out


class PolygonOptionsProvider(OptionsProvider):
    """Real option-chain snapshots from Massive/Polygon. Read-only HTTP GET; no order/trade/IBKR access."""
    name = "polygon"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *, timeout: float = 15.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("MASSIVE_API_KEY")
        self._base = (base_url or os.environ.get("OPTIONS_API_URL") or "https://api.polygon.io").rstrip("/")
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _get(self, path: str) -> dict | None:
        if not self._api_key:
            return None
        try:
            req = Request(f"{self._base}{path}", headers={
                "Accept": "application/json", "Authorization": f"Bearer {self._api_key}"})  # key in header only
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def get_option_chain(self, symbol: str) -> list[OptionContract]:
        return parse_polygon_options(self._get(f"/v3/snapshot/options/{symbol.upper()}?limit=250"), symbol)

    def get_unusual_activity(self, symbol: str) -> dict | None:
        cs = self.get_option_chain(symbol)
        return {"contracts": len(cs)} if cs else None

    def probe(self, symbol: str) -> dict:
        """Read-only entitlement probe (§ R1.1): ONE upstream GET whose outcome is reported honestly
        instead of being swallowed to NO DATA — 200 = entitled, 401 = bad key, 403 NOT_AUTHORIZED = the
        plan lacks the Options add-on. Never exposes the API key or the raw payload — only the HTTP
        status, Polygon's own status word, and the parsed contract count. Read-only; no order/trade/IBKR."""
        if not self._api_key:
            return {"configured": False, "http_status": None, "entitled": False,
                    "reason": "no_api_key", "upstream_status": None, "contracts": 0}
        path = f"/v3/snapshot/options/{symbol.upper()}?limit=250"
        req = Request(f"{self._base}{path}", headers={
            "Accept": "application/json", "Authorization": f"Bearer {self._api_key}"})  # key in header only
        try:
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                payload = json.loads(resp.read().decode("utf-8"))
            contracts = len(parse_polygon_options(payload, symbol))
            return {"configured": True, "http_status": getattr(resp, "status", 200), "entitled": True,
                    "reason": "ok", "upstream_status": (payload or {}).get("status"), "contracts": contracts}
        except HTTPError as e:
            upstream = None
            try:                                              # capture Polygon's status word — never the key
                body = json.loads(e.read().decode("utf-8"))
                upstream = body.get("status") or body.get("error")
            except Exception:
                upstream = None
            reason = {401: "auth_failed", 403: "not_entitled", 429: "rate_limited"}.get(e.code, f"http_{e.code}")
            return {"configured": True, "http_status": e.code, "entitled": False, "reason": reason,
                    "upstream_status": upstream, "contracts": 0}
        except URLError:
            return {"configured": True, "http_status": None, "entitled": None, "reason": "unreachable",
                    "upstream_status": None, "contracts": 0}
        except Exception:
            return {"configured": True, "http_status": None, "entitled": None, "reason": "error",
                    "upstream_status": None, "contracts": 0}


PROVIDERS: dict[str, type[OptionsProvider]] = {
    "null": NullOptionsProvider,
    "polygon": PolygonOptionsProvider,
    "massive": PolygonOptionsProvider,
}


def resolve_provider() -> OptionsProvider:
    """Select the configured provider (env ATP_OPTIONS_PROVIDER); default = Polygon/Massive (real, via
    MASSIVE_API_KEY). With no key / no options entitlement it yields nothing → NO DATA. Never fabricated."""
    key = (os.environ.get("ATP_OPTIONS_PROVIDER") or "polygon").strip().lower()
    cls = PROVIDERS.get(key, PolygonOptionsProvider)
    try:
        return cls()
    except Exception:
        return NullOptionsProvider()
