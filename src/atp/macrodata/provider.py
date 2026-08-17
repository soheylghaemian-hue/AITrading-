"""Macro data provider abstraction (§ Phase R1.2).

`MacroProvider` is the interface every macro source implements — no hard dependency on one vendor.
Future integrations (Federal Reserve / FRED, Trading Economics, Bloomberg, Refinitiv, Polygon/Massive
Macro) each register here. A real FRED provider is included (the canonical free source for rates,
inflation, employment, USD and VIX). If no key is configured, or a fetch fails, the methods return None
→ nothing persisted → NO DATA. Never fabricated. No broker/IBKR/execution access, no credentials.

This is NOT `atp.macro` (§5, the carry/rates-table strategy package). This layer is READ-ONLY macro
INTELLIGENCE — it measures the global environment; it never trades.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(slots=True)
class MacroMetrics:
    """A point-in-time reading of the global macro environment. Every field is optional — a source that
    doesn't provide a metric leaves it None (NO DATA), never a guessed value."""
    fed_rate: float | None = None          # Fed Funds effective rate (%)
    treasury_10y: float | None = None      # 10Y Treasury yield (%)
    treasury_2y: float | None = None       # 2Y Treasury yield (%)
    cpi: float | None = None               # headline CPI YoY (%)
    core_cpi: float | None = None          # core CPI YoY (%)
    unemployment: float | None = None      # unemployment rate (%)
    vix: float | None = None               # CBOE VIX
    dxy: float | None = None               # US Dollar Index
    oil: float | None = None               # WTI crude ($/bbl)
    gold: float | None = None              # gold ($/oz)

    def any_present(self) -> bool:
        return any(getattr(self, f) is not None for f in
                   ("fed_rate", "treasury_10y", "treasury_2y", "cpi", "core_cpi", "unemployment",
                    "vix", "dxy", "oil", "gold"))


class MacroProvider(ABC):
    name: str = "provider"

    @property
    def configured(self) -> bool:
        return False

    # The six domain reads (each returns the subset of MacroMetrics it owns, or None on NO DATA).
    @abstractmethod
    def get_interest_rates(self) -> MacroMetrics | None: ...
    @abstractmethod
    def get_inflation(self) -> MacroMetrics | None: ...
    @abstractmethod
    def get_employment(self) -> MacroMetrics | None: ...
    @abstractmethod
    def get_currency(self) -> MacroMetrics | None: ...
    @abstractmethod
    def get_volatility(self) -> MacroMetrics | None: ...
    @abstractmethod
    def get_market_regime_data(self) -> MacroMetrics | None: ...

    def snapshot(self) -> MacroMetrics:
        """Merge every domain read into one MacroMetrics. Missing domains contribute nothing."""
        merged = MacroMetrics()
        for getter in (self.get_interest_rates, self.get_inflation, self.get_employment,
                       self.get_currency, self.get_volatility, self.get_market_regime_data):
            try:
                part = getter()
            except Exception:
                part = None
            if part is None:
                continue
            for fld in merged.__slots__:
                v = getattr(part, fld)
                if v is not None and getattr(merged, fld) is None:
                    setattr(merged, fld, v)
        return merged


class NullMacroProvider(MacroProvider):
    name = "null"

    @property
    def configured(self) -> bool:
        return False

    def get_interest_rates(self): return None
    def get_inflation(self): return None
    def get_employment(self): return None
    def get_currency(self): return None
    def get_volatility(self): return None
    def get_market_regime_data(self): return None


def _num(x) -> float | None:
    try:
        return float(x) if x not in (None, "", ".") else None
    except (TypeError, ValueError):
        return None


# FRED series ids for each metric (the canonical free macro source).
FRED_SERIES = {
    "fed_rate": "FEDFUNDS", "treasury_10y": "DGS10", "treasury_2y": "DGS2",
    "cpi": "CPIAUCSL", "core_cpi": "CPILFESL", "unemployment": "UNRATE",
    "vix": "VIXCLS", "dxy": "DTWEXBGS", "oil": "DCOILWTICO",
    # gold: FRED's LBMA fixing series is discontinued → gold comes from Polygon (see _polygon_gold).
}
# CPI series are INDEX levels; request the year-over-year percent change (pc1) so they are real
# inflation rates, not index points (~318) that would look like "318% inflation".
FRED_UNITS = {"cpi": "pc1", "core_cpi": "pc1"}


def parse_fred_observation(payload: dict | None) -> float | None:
    """Pure parser for a FRED /series/observations response → the latest numeric value (or None).
    FRED marks missing points as '.', which parse to None. No fabrication."""
    obs = ((payload or {}).get("observations") or [])
    for o in obs:                                          # sorted desc → first real value wins
        v = _num((o or {}).get("value"))
        if v is not None:
            return v
    return None


def parse_polygon_prev(payload: dict | None) -> float | None:
    """Latest close from a Polygon /v2/aggs/ticker/{t}/prev response → float (or None). No fabrication."""
    res = ((payload or {}).get("results") or [])
    if res and isinstance(res[0], dict):
        return _num(res[0].get("c"))
    return None


class FredMacroProvider(MacroProvider):
    """Real macro reads from FRED (St. Louis Fed). Read-only HTTP GET; no order/trade/IBKR access.
    NOTE: FRED authenticates via an `api_key` QUERY param (it offers no header auth) — the key is never
    logged (the provider swallows errors and never surfaces the URL). Set FRED_API_KEY to activate."""
    name = "fred"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *, timeout: float = 15.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")
        self._base = (base_url or os.environ.get("MACRO_API_URL") or "https://api.stlouisfed.org").rstrip("/")
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _series_latest(self, series_id: str, units: str = "lin") -> float | None:
        if not self._api_key:
            return None
        # Ask for a short recent window (not just 1): the newest month/day can be unreleased ('.') —
        # especially for pc1 (year-over-year). The parser takes the most recent REAL value, so a
        # not-yet-published latest point falls back to the last released one instead of NO DATA.
        q = urlencode({"series_id": series_id, "api_key": self._api_key, "file_type": "json",
                       "sort_order": "desc", "limit": "12", "units": units})
        try:
            req = Request(f"{self._base}/fred/series/observations?{q}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                return parse_fred_observation(json.loads(resp.read().decode("utf-8")))
        except (HTTPError, URLError, ValueError, TimeoutError):
            return None
        except Exception:
            return None

    def _polygon_gold(self) -> float | None:
        """Gold ($/oz) from Polygon (C:XAUUSD prev close) via MASSIVE_API_KEY — FRED's gold fixing series
        is discontinued. Read-only; key in the header only. No key / not entitled / error → None (NO DATA)."""
        key = os.environ.get("MASSIVE_API_KEY")
        if not key:
            return None
        base = (os.environ.get("OPTIONS_API_URL") or "https://api.polygon.io").rstrip("/")
        try:
            req = Request(f"{base}/v2/aggs/ticker/C:XAUUSD/prev",
                          headers={"Accept": "application/json", "Authorization": f"Bearer {key}"})
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https host
                return parse_polygon_prev(json.loads(resp.read().decode("utf-8")))
        except (HTTPError, URLError, ValueError, TimeoutError):
            return None
        except Exception:
            return None

    def _read(self, *fields: str) -> MacroMetrics | None:
        m = MacroMetrics()
        got = False
        for fld in fields:
            v = self._series_latest(FRED_SERIES[fld], FRED_UNITS.get(fld, "lin"))
            if v is not None:
                setattr(m, fld, v)
                got = True
        return m if got else None

    def get_interest_rates(self): return self._read("fed_rate", "treasury_10y", "treasury_2y")
    def get_inflation(self): return self._read("cpi", "core_cpi")
    def get_employment(self): return self._read("unemployment")
    def get_currency(self): return self._read("dxy")
    def get_volatility(self): return self._read("vix")

    def get_market_regime_data(self):
        m = self._read("oil") or MacroMetrics()            # WTI from FRED (works)
        gold = self._polygon_gold()                        # gold from Polygon (FRED series discontinued)
        if gold is not None:
            m.gold = gold
        return m if m.any_present() else None


PROVIDERS: dict[str, type[MacroProvider]] = {
    "null": NullMacroProvider,
    "fred": FredMacroProvider,
    "fred_api": FredMacroProvider,
}


def resolve_provider() -> MacroProvider:
    """Select the configured macro provider (env ATP_MACRO_PROVIDER; default = FRED, real, via
    FRED_API_KEY). With no key it yields nothing → NO DATA. Never fabricated. Future: Trading Economics
    / Bloomberg / Refinitiv / Polygon-Macro register in PROVIDERS."""
    key = (os.environ.get("ATP_MACRO_PROVIDER") or "fred").strip().lower()
    cls = PROVIDERS.get(key, FredMacroProvider)
    try:
        return cls()
    except Exception:
        return NullMacroProvider()
