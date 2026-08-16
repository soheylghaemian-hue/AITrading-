"""Fundamentals read-model assembly (§ Phase G2.2). PURE composition from the store — reused by the
Control API and unit-tested directly. The company quality score + strengths/risks are computed
deterministically from the real persisted metrics. Missing data → None/empty (NO DATA), never
fabricated. No secrets. Intelligence signal only — never a buy/sell decision.
"""
from __future__ import annotations

from .quality import company_quality, quality_breakdown, strengths_and_risks


def build_fundamentals(store, symbol: str) -> dict:
    sym = symbol.upper()
    company = store.get_company(sym)
    fin = store.get_financial_metrics(sym)
    val = store.get_valuation(sym)
    est = store.get_analyst_estimates(sym)

    quality = company_quality(fin, val)
    subs = quality_breakdown(fin, val) if fin is not None else {}
    strengths, risks = strengths_and_risks(fin, val)

    return {
        "symbol": sym,
        "company": ({
            "company_name": company.company_name, "sector": company.sector, "industry": company.industry,
            "exchange": company.exchange, "country": company.country,
        } if company else None),
        "quality_score": quality,
        "quality_breakdown": {k: v for k, v in subs.items()} or None,
        "financials": ({
            "period": fin.period, "revenue": fin.revenue, "revenue_growth": fin.revenue_growth,
            "gross_margin": fin.gross_margin, "operating_margin": fin.operating_margin,
            "net_margin": fin.net_margin, "eps": fin.eps, "eps_growth": fin.eps_growth,
            "free_cash_flow": fin.free_cash_flow, "debt": fin.debt, "cash": fin.cash,
        } if fin else None),
        "valuation": ({
            "market_cap": val.market_cap, "pe_ratio": val.pe_ratio, "forward_pe": val.forward_pe,
            "price_sales": val.price_sales, "enterprise_value": val.enterprise_value,
        } if val else None),
        "analyst_estimates": ({
            "rating": est.rating, "target_price": est.target_price, "analyst_count": est.analyst_count,
            "upgrade_count": est.upgrade_count, "downgrade_count": est.downgrade_count,
        } if est else None),
        "strengths": strengths,
        "risks": risks,
    }
