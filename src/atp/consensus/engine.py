"""AI Consensus Engine (§ Phase G3) — deterministic. Reads the other intelligence read-models and folds
them into one AI market view. PURE apart from store reads; no network, no randomness, fully testable.

Weights: Market Data 20% · News 15% · Fundamentals 20% · Options 15% · Trader Intelligence 15% ·
Risk 15%. Only components with real data contribute (weights renormalize). Direction is a vote of the
DIRECTIONAL sources; if bullish and bearish sources both fire, the conflict is surfaced and the view
leans NEUTRAL — disagreement is never hidden. Nothing is fabricated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..fundamentals.readmodel import build_fundamentals
from ..optflow.readmodel import build_options
from ..traders.readmodel import build_symbol_consensus

WEIGHTS = {"Market Data": 0.20, "News": 0.15, "Fundamentals": 0.20, "Options": 0.15,
           "Trader Intelligence": 0.15, "Risk": 0.15}
_MIN_COVERAGE = 0.5      # < half the weights present → PARTIAL
_MIN_COMPONENTS = 3


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _dir_label(v: float) -> str:
    return "bullish" if v > 0 else "bearish" if v < 0 else "neutral"


def _component(name, score, direction_value, reason, risk_flags):
    return {"component_name": name, "score": round(score, 1), "weight": WEIGHTS[name],
            "direction": _dir_label(direction_value), "_dv": direction_value,
            "reason": reason, "risk_flags": risk_flags or []}


# ---------------------------------------------------------------- per-source component scoring
def _news_component(store, symbol):
    news = store.list_news(symbol, 20)
    scores = [n.sentiment_score for n in news if n.sentiment_score is not None]
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    score = _clamp(50 + avg * 50)
    dv = 1.0 if avg > 0.15 else -1.0 if avg < -0.15 else 0.0
    reason = "Positive news sentiment" if dv > 0 else "Negative news sentiment" if dv < 0 else "Mixed news sentiment"
    return _component("News", score, dv, reason, [])


def _market_component(store, symbol):
    bars = store.list_ohlc_bars(symbol, "1D", 60)
    if len(bars) < 5:
        bars = store.list_ohlc_bars(symbol, "1m", 200)
    closes = [float(b.close) for b in bars]
    if len(closes) < 5:
        return None
    first, last = closes[0], closes[-1]
    mean = sum(closes) / len(closes)
    mom = (last - first) / first if first else 0.0
    score = _clamp(50 + mom * 200)                         # +25% → 100, -25% → 0
    dv = 1.0 if last > mean * 1.005 else -1.0 if last < mean * 0.995 else 0.0
    reason = "Positive price momentum" if dv > 0 else "Negative price momentum" if dv < 0 else "Flat price action"
    return _component("Market Data", score, dv, reason, [])


def _risk_component(store, symbol):
    rs = store.get_risk_state()
    if rs is not None and (rs.killed or rs.halted):
        return _component("Risk", 5.0, 0.0, "Risk engine halted/killed", ["Risk engine halted"])
    today = datetime.now(timezone.utc).date().isoformat()
    dp = store.get_daily_pnl(today)
    if dp is None or not (float(dp.day_start_equity or 0) > 0):
        return None                                        # no loss data → NO DATA (never a fabricated default)
    daily = float(dp.realized_pnl) + float(dp.unrealized_pnl)
    loss_pct = max(0.0, -daily / float(dp.day_start_equity))
    score = _clamp(100 - loss_pct * 300)                   # 3% daily loss → ~91
    return _component("Risk", score, 0.0, f"Risk budget: {loss_pct * 100:.1f}% daily loss used",
                      (["Elevated portfolio risk"] if score < 50 else []))


def _fundamentals_component(store, symbol):
    fnd = build_fundamentals(store, symbol)
    q = fnd["quality_score"]
    if q is None:
        return None, fnd
    dv = 0.5 if q >= 70 else -0.5 if q < 40 else 0.0       # quality is a mild directional factor
    return _component("Fundamentals", q, dv, f"Company quality {q}/100", fnd["risks"]), fnd


def _options_component(store, symbol):
    opt = build_options(store, symbol)
    s = opt["options_score"]
    if s is None:
        return None, opt
    sent = opt.get("sentiment")
    dv = 1.0 if sent == "Bullish" else -1.0 if sent == "Bearish" else 0.0
    return _component("Options", s, dv, f"Options positioning {sent or 'neutral'}", opt["risks"]), opt


def _traders_component(store, symbol):
    trd = build_symbol_consensus(store, symbol)
    s = trd["weighted_score"]
    if s is None:
        return None, trd
    c = trd.get("consensus")
    dv = 1.0 if c == "BULLISH" else -1.0 if c == "BEARISH" else 0.0
    return _component("Trader Intelligence", s, dv, f"Trader consensus {c or 'neutral'}", []), trd


# ---------------------------------------------------------------- direction + conflicts
def _direction_and_conflicts(components):
    votes = [(c["component_name"], c["_dv"]) for c in components if c["_dv"] != 0]
    bull = [n for n, v in votes if v > 0]
    bear = [n for n, v in votes if v < 0]
    conflicts = [f"{b} bullish but {s} bearish" for b in bull for s in bear][:4]
    net = sum(v for _, v in votes)
    if conflicts and abs(net) < 1.5:
        direction = "NEUTRAL"
    elif net >= 1.2:
        direction = "BULLISH"
    elif net <= -1.2:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return direction, conflicts


def _no_data(sym):
    return {"symbol": sym, "score": None, "direction": None, "confidence": None, "status": "NO DATA",
            "coverage": 0.0, "components": [], "strengths": [], "risks": [], "conflicts": []}


def build_ai_consensus(store, symbol: str) -> dict:
    """The transparent AI market view for a symbol — computed fresh from the current intelligence."""
    sym = symbol.upper()
    comps: list[dict] = []
    strengths: list[str] = []
    risks: list[str] = []

    fnd_c, fnd = _fundamentals_component(store, sym)
    if fnd_c:
        comps.append(fnd_c)
        strengths += fnd["strengths"]
        risks += fnd["risks"]

    news_c = _news_component(store, sym)
    if news_c:
        comps.append(news_c)
        (strengths if news_c["_dv"] > 0 else risks if news_c["_dv"] < 0 else []).append(
            "Positive news flow" if news_c["_dv"] > 0 else "Negative news flow")

    opt_c, opt = _options_component(store, sym)
    if opt_c:
        comps.append(opt_c)
        strengths += opt["signals"]
        risks += opt["risks"]

    trd_c, trd = _traders_component(store, sym)
    if trd_c:
        comps.append(trd_c)
        if trd_c["_dv"] > 0:
            strengths.append("Bullish trader consensus")
        elif trd_c["_dv"] < 0:
            risks.append("Bearish trader consensus")

    md_c = _market_component(store, sym)
    if md_c:
        comps.append(md_c)
        if md_c["_dv"] > 0:
            strengths.append("Positive price momentum")
        elif md_c["_dv"] < 0:
            risks.append("Negative price momentum")

    risk_c = _risk_component(store, sym)
    if risk_c:
        comps.append(risk_c)
        risks += risk_c["risk_flags"]

    if not comps:
        return _no_data(sym)

    total_w = sum(c["weight"] for c in comps)
    score = round(sum(c["score"] * c["weight"] for c in comps) / total_w, 1)
    coverage = round(total_w, 3)                            # weights sum to 1.0 when all present
    direction, conflicts = _direction_and_conflicts(comps)
    agreement = 0.7 if conflicts else 1.0
    confidence = round(score * coverage * agreement, 1)
    status = "COMPLETE" if (coverage >= _MIN_COVERAGE and len(comps) >= _MIN_COMPONENTS) else "PARTIAL"

    return {
        "symbol": sym, "score": score, "direction": direction, "confidence": confidence,
        "status": status, "coverage": coverage,
        "components": [{k: v for k, v in c.items() if k != "_dv"} for c in comps],
        "strengths": _dedupe(strengths), "risks": _dedupe(risks), "conflicts": conflicts,
    }


# --------------------------------------------------------------- § R3.1A exact same-read consensus trace
class _RecordingStore:
    """Wraps a store so a single consensus computation reads each source EXACTLY ONCE: every read method is
    memoized by (name, args, kwargs) and its first result recorded. A later mutation of the underlying store
    can never change what this computation sees (it returns the memoized first read), and the recorded reads
    are the exact rows that produced the score — so the snapshot is built from them, never a second query."""

    def __init__(self, store):
        self._store = store
        self._cache: dict = {}
        self.reads: list = []

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            if key in self._cache:
                return self._cache[key]
            result = attr(*args, **kwargs)
            self._cache[key] = result
            self.reads.append({"method": name, "args": args, "result": result})
            return result
        return wrapper


# consensus component_name → the store read that anchors its provenance (first matching recorded read).
_COMPONENT_ANCHOR = {
    "News": "list_news", "Market Data": "list_ohlc_bars", "Fundamentals": "get_financial_metrics",
    "Options": "get_options_flow", "Trader Intelligence": "list_trader_positions_for_symbol", "Risk": None,
}


def _first_read(rec: _RecordingStore, method: str):
    for r in rec.reads:
        if r["method"] == method:
            return r["result"]
    return None


def _anchor_timestamps(rec: _RecordingStore, component_name: str) -> dict:
    """Genuine timestamps for a component, extracted from the EXACT recorded read (no new query). Never
    fabricated: absent fields stay None. Provenance classification (vs capture time) happens downstream."""
    method = _COMPONENT_ANCHOR.get(component_name)
    out = {"source_provider": None, "source_event_ts": None, "source_published_or_filed_ts": None,
           "source_observed_ts": None}
    if method is None:                                  # Risk (internal state) — no external source timestamp
        out["source_provider"] = "risk_engine"
        return out
    res = _first_read(rec, method)
    if not res:
        return out

    def s(v):
        return None if v in (None, "") else str(v)

    if method == "list_news":
        latest = max(res, key=lambda n: getattr(n, "published_at", "") or "")
        pub = s(getattr(latest, "published_at", None))
        out.update({"source_provider": getattr(latest, "source", None) or "news",
                    "source_event_ts": pub, "source_published_or_filed_ts": pub,
                    "source_observed_ts": s(getattr(latest, "created_at", None))})
    elif method == "list_ohlc_bars":
        b = res[-1]
        out.update({"source_provider": getattr(b, "source", None) or "market_data",
                    "source_event_ts": s(getattr(b, "ts", None)),
                    "source_observed_ts": s(getattr(b, "created_at", None))})
    elif method == "get_financial_metrics":
        out.update({"source_provider": "fundamentals",
                    "source_event_ts": s(getattr(res, "period", None)),
                    "source_observed_ts": s(getattr(res, "updated_at", None))})
    elif method == "get_options_flow":
        t = s(getattr(res, "timestamp", None))
        out.update({"source_provider": "options", "source_event_ts": t, "source_observed_ts": t})
    elif method == "list_trader_positions_for_symbol":
        t = s(getattr(res[0], "timestamp", None)) if res else None
        out.update({"source_provider": "traders", "source_event_ts": t, "source_observed_ts": t})
    return out


def _decision_price_meta(rec: _RecordingStore) -> dict:
    """The observed decision-price reference from the SAME market read — precision preserved (string of the
    raw Decimal close, never a binary float) with its exact source bar timestamp (§ correction 10)."""
    bars = _first_read(rec, "list_ohlc_bars")
    if bars:
        b = bars[-1]
        return {"decision_price": str(getattr(b, "close", "")), "decision_price_bar_ts": str(getattr(b, "ts", "")),
                "decision_price_source": getattr(b, "source", None) or "market_data",
                "decision_price_provenance_status": "OBSERVED_ONLY"}
    return {"decision_price": None, "decision_price_bar_ts": None, "decision_price_source": None,
            "decision_price_provenance_status": "UNKNOWN"}


def build_ai_consensus_traced(store, symbol: str) -> tuple[dict, list[dict], dict]:
    """ONE computation returning (assessment, trace, meta). The assessment is byte-identical to
    `build_ai_consensus(store, symbol)`; the trace carries, per contributing component, the exact canonical
    value that produced the score plus genuine source timestamps from the SAME reads; meta carries the
    observed decision price from that same read. The caller builds the snapshot solely from these — it must
    not query the store again."""
    rec = _RecordingStore(store)
    assessment = build_ai_consensus(rec, symbol)
    trace = []
    for c in assessment.get("components") or []:
        name = c.get("component_name")
        trace.append({"component_name": name,
                      "canonical_value": {k: c.get(k) for k in
                                          ("component_name", "score", "weight", "direction", "reason",
                                           "risk_flags")},
                      "component_score": c.get("score"), "component_status": c.get("direction"),
                      **_anchor_timestamps(rec, name)})
    return assessment, trace, _decision_price_meta(rec)


def persist_ai_consensus(store, assessment: dict) -> None:
    """Persist a computed assessment (+ its components) as an audit/history snapshot. Read-only wrt
    trading; writes only to the ai_assessment* tables."""
    sym = assessment["symbol"]
    store.upsert_ai_assessment(symbol=sym, overall_score=assessment["score"],
                               direction_bias=assessment["direction"], confidence=assessment["confidence"],
                               status=assessment["status"])
    for c in assessment["components"]:
        store.upsert_ai_assessment_component(
            assessment_id=sym, component_name=c["component_name"], score=c["score"], weight=c["weight"],
            direction=c["direction"], reason=c["reason"], risk_flags=json.dumps(c["risk_flags"]))
