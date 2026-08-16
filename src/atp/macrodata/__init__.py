"""Macro Intelligence Layer (§ Phase R1.2) — READ-ONLY global-environment intelligence.

Understands the macro backdrop (rates, inflation, employment, volatility, currency, commodities) and
classifies the current risk regime (RISK_ON / RISK_NEUTRAL / RISK_OFF) as an intelligence input for the
AI Brain and the Data Completeness engine. It NEVER trades, generates orders, or touches Trading Core /
Risk Engine / Broker / IBKR / Execution. Missing data → NO DATA, never fabricated.

Distinct from `atp.macro` (§5, the carry/rates-table strategy package) — do not confuse the two.
"""

from .collector import MacroCollector
from .provider import (
    FredMacroProvider,
    MacroMetrics,
    MacroProvider,
    NullMacroProvider,
    resolve_provider,
)
from .readmodel import build_macro, build_macro_context
from .regime import classify_regime, macro_score, signals_and_risks

__all__ = [
    "MacroProvider",
    "NullMacroProvider",
    "FredMacroProvider",
    "MacroMetrics",
    "resolve_provider",
    "MacroCollector",
    "build_macro",
    "build_macro_context",
    "classify_regime",
    "macro_score",
    "signals_and_risks",
]
