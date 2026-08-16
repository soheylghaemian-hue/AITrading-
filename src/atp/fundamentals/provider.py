"""Fundamentals provider abstraction (§ Phase G2.2).

`FundamentalsProvider` is the interface every source implements. No hard dependency on one vendor —
future integrations (Financial Modeling Prep, Alpha Vantage, SEC EDGAR, Massive/Polygon, Morningstar,
FactSet) each register here. A real Massive/Polygon provider is included (company profile + financials
via the licensed MASSIVE_API_KEY, sent as an Authorization: Bearer header — never in the URL/logs). If
no key is configured, or the plan lacks fundamentals entitlement, or a fetch fails, methods return
None/empty → nothing persisted → NO DATA. Analyst estimates are None on Polygon (no analyst product) →
NO DATA. Nothing is fabricated. No broker/IBKR/execution access, no credentials handled.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.request import Request, urlopen


@dataclass(slots=True)
class CompanyProfile:
    symbol: str
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    country: str | None = None
    market_cap: float | None = None


@dataclass(slots=True)
class FinancialMetrics:
    symbol: str
    period: str | None = None
    revenue: float | None = None
    revenue_growth: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    eps: float | None = None
    eps_growth: float | None = None
    free_cash_flow: float | None = None
    debt: float | None = None
    cash: float | None = None


@dataclass(slots=True)
class Valuation:
    symbol: str
    market_cap: float | None = None
    pe_ratio: float | None = None
    forward_pe: float | None = None
    price_sales: float | None = None
    enterprise_value: float | None = None


@dataclass(slots=True)
class AnalystEstimates:
    symbol: str
    rating: str | None = None
    target_price: float | None = None
    analyst_count: int | None = None
    upgrade_count: int | None = None
    downgrade_count: int | None = None


@dataclass(slots=True)
class Earnings:
    symbol: str
    eps: float | None = None
    period: str | None = None


class FundamentalsProvider(ABC):
    name: str = "provider"

    @property
    def configured(self) -> bool:
        return False

    @abstractmethod
    def get_company_profile(self, symbol: str) -> CompanyProfile | None: ...
    @abstractmethod
    def get_financials(self, symbol: str) -> FinancialMetrics | None: ...
    @abstractmethod
    def get_earnings(self, symbol: str) -> Earnings | None: ...
    @abstractmethod
    def get_valuation(self, symbol: str) -> Valuation | None: ...
    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates | None: ...


class NullFundamentalsProvider(FundamentalsProvider):
    name = "null"

    def get_company_profile(self, symbol): return None
    def get_financials(self, symbol): return None
    def get_earnings(self, symbol): return None
    def get_valuation(self, symbol): return None
    def get_estimates(self, symbol): return None


# ---------------------------------------------------------------- pure parsers (testable, no network)
def _num(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _stmt_val(financials: dict, statement: str, key: str) -> float | None:
    s = (financials or {}).get(statement) or {}
    v = s.get(key)
    return _num(v.get("value")) if isinstance(v, dict) else None


def parse_polygon_profile(payload: dict | None, symbol: str) -> CompanyProfile | None:
    r = (payload or {}).get("results")
    if not isinstance(r, dict) or not r:
        return None
    return CompanyProfile(
        symbol=symbol.upper(), company_name=r.get("name"),
        sector=r.get("sic_description"), industry=r.get("sic_description"),
        exchange=r.get("primary_exchange"), country=((r.get("locale") or "").upper() or None),
        market_cap=_num(r.get("market_cap")))


def parse_polygon_financials(payload: dict | None, symbol: str) -> FinancialMetrics | None:
    results = (payload or {}).get("results") or []
    if not results:
        return None
    latest = (results[0] or {}).get("financials") or {}
    rev = _stmt_val(latest, "income_statement", "revenues")
    gp = _stmt_val(latest, "income_statement", "gross_profit")
    op = _stmt_val(latest, "income_statement", "operating_income_loss")
    ni = _stmt_val(latest, "income_statement", "net_income_loss")
    eps = (_stmt_val(latest, "income_statement", "diluted_earnings_per_share")
           or _stmt_val(latest, "income_statement", "basic_earnings_per_share"))

    def margin(v):
        return (v / rev) if (v is not None and rev not in (None, 0)) else None

    rev_growth = eps_growth = None
    if len(results) > 1:
        prev = (results[1] or {}).get("financials") or {}
        prev_rev = _stmt_val(prev, "income_statement", "revenues")
        prev_eps = (_stmt_val(prev, "income_statement", "diluted_earnings_per_share")
                    or _stmt_val(prev, "income_statement", "basic_earnings_per_share"))
        if rev is not None and prev_rev:
            rev_growth = (rev - prev_rev) / abs(prev_rev)
        if eps is not None and prev_eps:
            eps_growth = (eps - prev_eps) / abs(prev_eps)

    return FinancialMetrics(
        symbol=symbol.upper(), period=(results[0].get("fiscal_period") or results[0].get("timeframe")),
        revenue=rev, revenue_growth=rev_growth, gross_margin=margin(gp), operating_margin=margin(op),
        net_margin=margin(ni), eps=eps, eps_growth=eps_growth, free_cash_flow=None, debt=None, cash=None)


class PolygonFundamentalsProvider(FundamentalsProvider):
    """Real fundamentals from Massive/Polygon. Read-only HTTP GET; no order/trade/IBKR access."""
    name = "polygon"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, *, timeout: float = 12.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("MASSIVE_API_KEY")
        self._base = (base_url or os.environ.get("FUNDAMENTALS_API_URL") or "https://api.polygon.io").rstrip("/")
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

    def get_company_profile(self, symbol: str) -> CompanyProfile | None:
        return parse_polygon_profile(self._get(f"/v3/reference/tickers/{symbol.upper()}"), symbol)

    def get_financials(self, symbol: str) -> FinancialMetrics | None:
        return parse_polygon_financials(
            self._get(f"/vX/reference/financials?ticker={symbol.upper()}&timeframe=annual&order=desc&limit=2"),
            symbol)

    def get_earnings(self, symbol: str) -> Earnings | None:
        fin = self.get_financials(symbol)
        return Earnings(symbol.upper(), eps=fin.eps, period=fin.period) if fin else None

    def get_valuation(self, symbol: str) -> Valuation | None:
        prof = self.get_company_profile(symbol)
        fin = self.get_financials(symbol)
        mc = prof.market_cap if prof else None
        pe = ps = None
        if fin is not None and mc:
            if fin.net_margin is not None and fin.revenue:
                ni = fin.net_margin * fin.revenue
                if ni > 0:
                    pe = mc / ni
            if fin.revenue:
                ps = mc / fin.revenue
        if mc is None and pe is None and ps is None:
            return None
        return Valuation(symbol.upper(), market_cap=mc, pe_ratio=pe, forward_pe=None,
                         price_sales=ps, enterprise_value=None)

    def get_estimates(self, symbol: str) -> AnalystEstimates | None:
        return None      # Polygon's standard plan has no analyst ratings/targets → NO DATA (never faked)


PROVIDERS: dict[str, type[FundamentalsProvider]] = {
    "null": NullFundamentalsProvider,
    "polygon": PolygonFundamentalsProvider,
    "massive": PolygonFundamentalsProvider,
}


def resolve_provider() -> FundamentalsProvider:
    """Select the configured provider (env ATP_FUNDAMENTALS_PROVIDER); default = Polygon/Massive (real,
    via MASSIVE_API_KEY). With no key it is `configured=False` → NO DATA. Never fabricated."""
    key = (os.environ.get("ATP_FUNDAMENTALS_PROVIDER") or "polygon").strip().lower()
    cls = PROVIDERS.get(key, PolygonFundamentalsProvider)
    try:
        return cls()
    except Exception:
        return NullFundamentalsProvider()
