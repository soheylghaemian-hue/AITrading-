"""Macro read-models (§ Phase R1.2) — PURE (reads persisted history only).

`build_macro` = the current global macro environment: score, regime, signals, risks, the raw metrics
and their trend vs the previous snapshot. `build_macro_context` = what that environment means for a
symbol (tailwind / neutral / headwind). No snapshot → NO DATA (never fabricated). Read-only intelligence
input; never a trade, order, or broker action.
"""
from __future__ import annotations

from .regime import classify_regime, macro_score, signals_and_risks

_METRIC_FIELDS = ("fed_rate", "treasury_10y", "treasury_2y", "cpi", "core_cpi", "unemployment", "vix", "dxy", "oil", "gold")
_METRIC_LABELS = {
    "fed_rate": "Fed Funds Rate", "treasury_10y": "10Y Treasury", "treasury_2y": "2Y Treasury",
    "core_cpi": "Core CPI (YoY)",
    "cpi": "CPI (YoY)", "unemployment": "Unemployment", "vix": "VIX", "dxy": "US Dollar Index",
    "oil": "WTI Crude", "gold": "Gold",
}


def _no_data() -> dict:
    return {"score": None, "regime": None, "status": "NO DATA", "signals": [], "risks": [],
            "metrics": {}, "timestamp": None, "source": None}


def _trend(cur_v, prev_v) -> str | None:
    if cur_v is None or prev_v is None:
        return None
    if cur_v > prev_v:
        return "up"
    if cur_v < prev_v:
        return "down"
    return "flat"


def build_macro(store) -> dict:
    """The current macro environment read-model. NO DATA until a real snapshot exists."""
    cur = store.latest_macro_snapshot()
    if cur is None:
        return _no_data()
    history = store.list_macro_snapshots(2)
    prev = history[1] if len(history) > 1 else None

    score = macro_score(cur, prev)
    regime = classify_regime(cur, prev)
    signals, risks = signals_and_risks(cur, prev)
    metrics = {}
    for f in _METRIC_FIELDS:
        v = getattr(cur, f)
        metrics[f] = {"label": _METRIC_LABELS[f], "value": v,
                      "trend": _trend(v, getattr(prev, f) if prev else None)}
    present = sum(1 for f in _METRIC_FIELDS if getattr(cur, f) is not None)
    status = "COMPLETE" if present >= 5 else "PARTIAL" if present > 0 else "NO DATA"
    return {"score": score, "regime": regime, "status": status, "signals": signals, "risks": risks,
            "metrics": metrics, "timestamp": cur.timestamp, "source": cur.source}


# How the current regime maps to a risk asset (all GIGBAY symbols are equities/ETFs → risk assets).
_RELEVANCE = {"RISK_ON": ("TAILWIND", "Risk-supportive macro — a tailwind for equities/risk assets."),
              "RISK_NEUTRAL": ("NEUTRAL", "Mixed macro — no strong directional pull for risk assets."),
              "RISK_OFF": ("HEADWIND", "Risk-averse macro — a headwind for equities/risk assets.")}


def build_macro_context(store, symbol: str) -> dict:
    """Macro relevance for a symbol. The macro environment is global; this frames what the current
    regime means for a (risk-asset) symbol. NO DATA until a real snapshot exists."""
    macro = build_macro(store)
    sym = symbol.upper()
    if macro["regime"] is None:
        return {"symbol": sym, "regime": None, "score": None, "relevance": None, "note": None,
                "status": "NO DATA", "signals": [], "risks": []}
    relevance, note = _RELEVANCE.get(macro["regime"], (None, None))
    return {"symbol": sym, "regime": macro["regime"], "score": macro["score"], "relevance": relevance,
            "note": note, "status": macro["status"], "signals": macro["signals"], "risks": macro["risks"]}
