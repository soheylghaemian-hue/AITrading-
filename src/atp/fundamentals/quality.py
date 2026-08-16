"""Fundamentals Quality Engine (§ Phase G2.2) — deterministic 0-100 company quality. PURE, testable.

Weighted components (per spec):
    Growth            25%   — revenue (and EPS) growth
    Profitability     25%   — net / operating / gross margins
    Balance Sheet     20%   — cash vs debt
    Cash Flow         15%   — free cash flow vs revenue
    Valuation         15%   — P/E (lower is better, without rewarding distress)

Only components with real data contribute; missing ones are dropped and the weights renormalize (never
a fabricated sub-score). No metrics at all → None (NO DATA). Strengths/risks are a transparent labelling
of the SAME real metrics — never invented. This is an intelligence signal, never a buy/sell decision.
"""
from __future__ import annotations

WEIGHTS = {"growth": 0.25, "profitability": 0.25, "balance_sheet": 0.20, "cash_flow": 0.15, "valuation": 0.15}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _growth(fin) -> float | None:
    parts = []
    if fin.revenue_growth is not None:
        parts.append(_clamp(50 + 130 * fin.revenue_growth))     # +35% → ~95, 0% → 50, -20% → 24
    if fin.eps_growth is not None:
        parts.append(_clamp(50 + 100 * fin.eps_growth))
    return round(sum(parts) / len(parts), 2) if parts else None


def _profitability(fin) -> float | None:
    if fin.net_margin is not None:
        return round(_clamp(30 + 140 * fin.net_margin), 2)      # 44% net → ~92
    if fin.operating_margin is not None:
        return round(_clamp(20 + 130 * fin.operating_margin), 2)
    if fin.gross_margin is not None:
        return round(_clamp(120 * fin.gross_margin), 2)
    return None


def _balance_sheet(fin) -> float | None:
    if fin.cash is None and fin.debt is None:
        return None
    cash = fin.cash or 0.0
    debt = fin.debt or 0.0
    if debt <= 0:
        return 100.0
    return round(_clamp(50 + 50 * (cash - debt) / max(cash, debt, 1.0)), 2)


def _cash_flow(fin) -> float | None:
    if fin.free_cash_flow is None or not fin.revenue:
        return None
    return round(_clamp(50 + 100 * fin.free_cash_flow / fin.revenue), 2)


def _valuation(val) -> float | None:
    if val is None or val.pe_ratio is None:
        return None
    return round(_clamp(100 - max(0.0, val.pe_ratio - 20.0) * 1.2), 2)   # P/E 20 → 100, 50 → ~64, 90 → 16


def quality_breakdown(fin, val=None) -> dict[str, float | None]:
    if fin is None:
        return {k: None for k in WEIGHTS}
    return {
        "growth": _growth(fin),
        "profitability": _profitability(fin),
        "balance_sheet": _balance_sheet(fin),
        "cash_flow": _cash_flow(fin),
        "valuation": _valuation(val),
    }


def company_quality(fin, val=None) -> float | None:
    """Overall 0-100 quality — weighted mean over components with real data. None when no data."""
    if fin is None:
        return None
    subs = quality_breakdown(fin, val)
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = subs.get(k)
        if v is not None:
            num += w * v
            den += w
    if den == 0:
        return None
    return round(num / den, 1)


def strengths_and_risks(fin, val=None) -> tuple[list[str], list[str]]:
    """Deterministic ✓ strengths / ⚠ risks derived from the SAME real metrics (never fabricated)."""
    strengths: list[str] = []
    risks: list[str] = []
    if fin is None:
        return strengths, risks
    if fin.revenue_growth is not None and fin.revenue_growth > 0.15:
        strengths.append("Revenue growth")
    if fin.net_margin is not None and fin.net_margin > 0.15:
        strengths.append("High margins")
    if fin.free_cash_flow is not None and fin.free_cash_flow > 0:
        strengths.append("Strong cash flow")
    if fin.cash is not None and fin.debt is not None and fin.cash > fin.debt:
        strengths.append("Strong balance sheet")

    if val is not None and val.pe_ratio is not None and val.pe_ratio > 40:
        risks.append("High valuation")
    if fin.debt is not None and fin.cash is not None and fin.debt > fin.cash * 2:
        risks.append("High leverage")
    if fin.revenue_growth is not None and fin.revenue_growth < 0:
        risks.append("Declining revenue")
    if fin.net_margin is not None and fin.net_margin < 0:
        risks.append("Unprofitable")
    return strengths, risks
