"""Options read-model assembly (§ Phase G2.3). PURE composition from the store — reused by the Control
API and unit-tested directly. The options intelligence score + signals/risks are computed
deterministically from the real persisted flow. Missing data → None/empty (NO DATA), never fabricated.
No secrets. Intelligence signal only — never a buy/sell decision.
"""
from __future__ import annotations

from .analytics import options_score, signals_and_risks, unusual_activity_label


def build_options(store, symbol: str) -> dict:
    sym = symbol.upper()
    flow = store.get_options_flow(sym)
    score = options_score(flow)
    signals, risks = signals_and_risks(flow)
    return {
        "symbol": sym,
        "options_score": score,
        "call_put_ratio": flow.call_put_ratio if flow else None,
        "implied_volatility": flow.implied_volatility if flow else None,
        "volume": ((flow.call_volume or 0) + (flow.put_volume or 0)) if flow else None,
        "call_volume": flow.call_volume if flow else None,
        "put_volume": flow.put_volume if flow else None,
        "open_interest": flow.open_interest if flow else None,
        "premium_volume": flow.premium_volume if flow else None,
        "unusual_activity": unusual_activity_label(flow),
        "unusual_activity_score": flow.unusual_activity_score if flow else None,
        "large_trade_count": flow.large_trade_count if flow else None,
        "sentiment": flow.sentiment if flow else None,
        "signals": signals,
        "risks": risks,
    }
