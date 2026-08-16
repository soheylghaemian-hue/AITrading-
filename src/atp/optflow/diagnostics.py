"""Options data-provider entitlement audit (§ Phase R1.1) — READ-ONLY.

Answers one question honestly: is a LICENSED options data provider actually AVAILABLE? It resolves the
configured provider (default Massive/Polygon) and probes it per symbol — never exposing the API key.
Data only: no trading, no order/broker/IBKR/execution. If the probe is blocked (403 NOT_AUTHORIZED),
unauthenticated (401), or unconfigured, the verdict is NOT AVAILABLE and the data stays NO DATA — never
fabricated. Activation is entitlement-gated, not code-gated: the moment the plan includes Options, the
existing `atp.optflow` collector/provider returns real data with no code change.
"""
from __future__ import annotations

from .provider import resolve_provider

# Recommended licensed sources when options data is NOT AVAILABLE (Task 3).
RECOMMENDED_PROVIDERS = [
    {"name": "Polygon Options (add-on / upgrade)",
     "note": "Enable the Options entitlement on the existing Massive/Polygon plan. Zero code change — "
             "the current PolygonOptionsProvider lights up immediately."},
    {"name": "CBOE DataShop / LiveVol",
     "note": "Authoritative US options: full chains, greeks, IV, open interest."},
    {"name": "ORATS",
     "note": "Options analytics API: implied-vol surface, greeks, historical vols."},
    {"name": "Tradier Market Data",
     "note": "Affordable REST option chains + greeks (brokerage market-data API)."},
]

VALIDATION_SYMBOLS = ["NVDA", "AAPL", "SPY"]
OPTIONS_CHECKS = ["option chain", "IV", "volume", "open interest", "call/put ratio"]


def audit_options_provider(symbols=None) -> dict:
    """Probe the configured options provider for each symbol and return an AVAILABLE / NOT AVAILABLE
    verdict. Read-only; exposes no secrets. Recommends licensed providers when NOT AVAILABLE."""
    syms = [s.upper() for s in (symbols or VALIDATION_SYMBOLS)]
    provider = resolve_provider()
    per_symbol = {s: provider.probe(s) for s in syms}
    entitled = any(v.get("entitled") for v in per_symbol.values())
    return {
        "provider": provider.name,
        "configured": bool(getattr(provider, "configured", False)),
        "options_access": "AVAILABLE" if entitled else "NOT AVAILABLE",
        "checks": OPTIONS_CHECKS,
        "symbols": per_symbol,
        "recommended_providers": None if entitled else RECOMMENDED_PROVIDERS,
    }
