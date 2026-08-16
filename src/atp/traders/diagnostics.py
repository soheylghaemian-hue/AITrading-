"""Trader-intelligence provider audit (§ Phase R1.2) — READ-ONLY.

Reports which trader-intelligence sources are available and which one is selected, without exposing any
secret. Data only: no copy-trading, no broker, no execution. "Trader intelligence" = what high-quality
market participants are DOING (holdings/positioning), never "copy their trades".
"""
from __future__ import annotations

from .provider import resolve_provider

# The evaluated sources (Task 1). `available` = has a legal, programmatic API GIGBAY can consume.
TRADER_PROVIDER_AUDIT = [
    {"category": "Institutional", "provider": "SEC 13F (EDGAR)", "available": True,
     "api": "SEC EDGAR public JSON + filing XML (data.sec.gov, www.sec.gov/Archives)",
     "license": "U.S. public domain / free (descriptive User-Agent required, <=10 req/s)",
     "data_quality": "Institutional quarterly holdings; decades of history; positioning (not returns)"},
    {"category": "Institutional", "provider": "Fund holdings (SEC N-PORT)", "available": True,
     "api": "SEC EDGAR N-PORT", "license": "Public / free",
     "data_quality": "Registered-fund monthly holdings; heavier parsing than 13F"},
    {"category": "Institutional", "provider": "Insider transactions (SEC Form 4)", "available": True,
     "api": "SEC EDGAR Form 4", "license": "Public / free",
     "data_quality": "Insider buys/sells — a different signal (insiders, not funds)"},
    {"category": "Strategy platform", "provider": "Darwinex", "available": "CONDITIONAL",
     "api": "Darwinex API (OAuth)", "license": "Account + API access under Darwinex ToS",
     "data_quality": "Real strategy returns / DARWIN risk metrics — performance-rich"},
    {"category": "Strategy platform", "provider": "Collective2", "available": "CONDITIONAL",
     "api": "Collective2 API", "license": "Subscription + API key",
     "data_quality": "Strategy leaderboards with returns / drawdown"},
    {"category": "Social", "provider": "eToro", "available": False,
     "api": "No official public API for Popular-Investor positions",
     "license": "Restricted; scraping violates ToS",
     "data_quality": "Retail copy-trading signal; ToS-restricted, unverified"},
    {"category": "Social", "provider": "TradingView", "available": False,
     "api": "No official ideas/positions API", "license": "Restricted; scraping forbidden by ToS",
     "data_quality": "Retail ideas; low / unverified quality"},
]

SELECTED = "SEC 13F (EDGAR)"
SELECTION_REASON = (
    "Institutional quality, a legal free public API, decades of historical data, and measurable "
    "positioning — the best first production source. It is holdings intelligence (what institutions "
    "own), not copy-trading. Returns/Sharpe/drawdown are NO DATA in 13F, so manager quality is scored "
    "on the verified filing track record; a performance-rich source (Darwinex / Collective2) is the "
    "recommended second integration to add return-based quality."
)


def audit_trader_providers() -> dict:
    """The provider audit + the currently-resolved provider's readiness. Read-only, exposes no secrets."""
    provider = resolve_provider()
    configured = bool(getattr(provider, "configured", False))
    return {
        "selected_provider": SELECTED,
        "selection_reason": SELECTION_REASON,
        "active_provider": provider.name,
        "configured": configured,
        "trader_access": "AVAILABLE" if (provider.name != "null" and configured) else "NOT AVAILABLE",
        "activation": "Set ATP_TRADER_PROVIDER=sec13f and ATP_SEC_USER_AGENT=\"<name> <contact-email>\".",
        "providers": TRADER_PROVIDER_AUDIT,
    }
