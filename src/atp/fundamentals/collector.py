"""Fundamentals collector (§ Phase G2.2). Pulls company profile / financials / valuation / estimates
from the provider and upserts them into PostgreSQL. Idempotent (PK on symbol) → restart-safe. Persists
ONLY real provider data — never a fabricated revenue, margin or valuation. Raises on a store failure so
the service can fail closed. No execution, no broker, no IBKR access anywhere.
"""
from __future__ import annotations


class FundamentalsCollector:
    def __init__(self, store, provider) -> None:
        self.store = store
        self.provider = provider

    def collect(self, symbol: str) -> bool:
        """Fetch + persist fundamentals for one symbol. Returns True if any real data was persisted."""
        sym = symbol.upper()
        got = False
        prof = self.provider.get_company_profile(sym)
        if prof is not None:
            self.store.upsert_company(symbol=sym, company_name=prof.company_name, sector=prof.sector,
                                      industry=prof.industry, exchange=prof.exchange, country=prof.country)
            got = True
        fin = self.provider.get_financials(sym)
        if fin is not None:
            self.store.upsert_financial_metrics(
                symbol=sym, period=fin.period, revenue=fin.revenue, revenue_growth=fin.revenue_growth,
                gross_margin=fin.gross_margin, operating_margin=fin.operating_margin, net_margin=fin.net_margin,
                eps=fin.eps, eps_growth=fin.eps_growth, free_cash_flow=fin.free_cash_flow,
                debt=fin.debt, cash=fin.cash)
            got = True
        val = self.provider.get_valuation(sym)
        if val is not None:
            self.store.upsert_valuation(symbol=sym, market_cap=val.market_cap, pe_ratio=val.pe_ratio,
                                        forward_pe=val.forward_pe, price_sales=val.price_sales,
                                        enterprise_value=val.enterprise_value)
            got = True
        est = self.provider.get_estimates(sym)
        if est is not None:
            self.store.upsert_analyst_estimates(
                symbol=sym, rating=est.rating, target_price=est.target_price, analyst_count=est.analyst_count,
                upgrade_count=est.upgrade_count, downgrade_count=est.downgrade_count)
            got = True
        return got
