"""Data Completeness Engine (§ Phase C1) — deterministic. READ-ONLY.

For a symbol it inspects the 7 intelligence domains, scores each on how much real data it has (a
fraction of concrete checks that pass), and folds them into a weighted 0-100 completeness score with a
readiness state. PURE apart from store reads: no network, no randomness, fully testable.

    Market Data 20% · Technical 15% · News 15% · Fundamentals 20% · Options 10% · Trader 10% · Macro 10%

Nothing is fabricated: a domain with no data scores 0 and is reported MISSING (never a guessed value).
The score never rises to cover a gap. It performs NO trading, NO order/broker/IBKR/execution.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ..traders.readmodel import build_symbol_consensus

# ---- deterministic domain weights (sum = 1.0) ----
WEIGHTS: dict[str, float] = {
    "market": 0.20, "technical": 0.15, "news": 0.15, "fundamentals": 0.20,
    "options": 0.10, "trader": 0.10, "macro": 0.10,
}
DOMAIN_LABELS: dict[str, str] = {
    "market": "Market Data", "technical": "Technical", "news": "News", "fundamentals": "Fundamentals",
    "options": "Options", "trader": "Trader Intelligence", "macro": "Macro",
}
AVAILABLE_THRESHOLD = 0.5                                   # ≥ half a domain's checks pass → "available"
_OHLC_STALE_DAYS = float(os.environ.get("ATP_COMPLETENESS_OHLC_STALE_DAYS", "5"))

# ---- readiness thresholds (§ C1) ----
READY_MIN = 80.0
PARTIAL_MIN = 50.0


def readiness_state(score: float | None) -> str:
    if score is None:
        return "INSUFFICIENT"
    if score >= READY_MIN:
        return "READY"
    if score >= PARTIAL_MIN:
        return "PARTIAL"
    return "INSUFFICIENT"


def _frac(checks: dict[str, bool]) -> float:
    return (sum(1 for v in checks.values() if v) / len(checks)) if checks else 0.0


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    for cand in (s, s[:19], s[:10]):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _age_days(ts, now: datetime) -> float | None:
    d = _parse_ts(ts)
    return None if d is None else (now - d).total_seconds() / 86400.0


# ---------------------------------------------------------------- per-domain checks (all read-only)
def _market_domain(store, sym: str) -> tuple[float, dict]:
    row = next((r for r in store.list_md_health() if r[0] == sym), None)   # (symbol,source,status,latency,updated_at)
    checks = {
        "realtime_quote": row is not None and bool(row[1]),
        "timestamp": bool(row and row[4]),
        "source_status": bool(row and str(row[2]).upper() == "UP"),
    }
    return _frac(checks), checks


def _technical_domain(store, sym: str, now: datetime) -> tuple[float, dict]:
    intervals = ["1D", "1h", "15m", "5m", "1m"]
    depths: dict[str, int] = {}
    latest_ts = None
    for iv in intervals:
        bars = store.list_ohlc_bars(sym, iv, 60)           # oldest → newest
        depths[iv] = len(bars)
        if bars:
            ts = bars[-1].ts
            if latest_ts is None or str(ts) > str(latest_ts):
                latest_ts = ts
    age = _age_days(latest_ts, now)
    checks = {
        "candles": any(v >= 5 for v in depths.values()),
        "timeframes": sum(1 for v in depths.values() if v >= 1) >= 2,
        "freshness": age is not None and age <= _OHLC_STALE_DAYS,
    }
    return _frac(checks), checks


def _news_domain(store, sym: str) -> tuple[float, dict]:
    items = store.list_news(sym, 50)
    checks = {
        "recent_news": len(items) >= 1,
        "sentiment": any(n.sentiment_score is not None for n in items),
        "impact": any(n.impact_level for n in items),
    }
    return _frac(checks), checks


def _fundamentals_domain(store, sym: str) -> tuple[float, dict]:
    checks = {
        "company_profile": store.get_company(sym) is not None,
        "financial_metrics": store.get_financial_metrics(sym) is not None,
        "valuation": store.get_valuation(sym) is not None,
    }
    return _frac(checks), checks


def _options_domain(store, sym: str) -> tuple[float, dict]:
    flow = store.get_options_flow(sym)
    snaps = store.list_options_snapshots(sym, 1)
    vol = ((flow.call_volume or 0) + (flow.put_volume or 0)) if flow else 0
    checks = {
        "option_chain": len(snaps) >= 1 or flow is not None,
        "iv": flow is not None and flow.implied_volatility is not None,
        "volume": vol > 0,
        "open_interest": flow is not None and flow.open_interest is not None,
    }
    return _frac(checks), checks


def _trader_domain(store, sym: str) -> tuple[float, dict]:
    cons = build_symbol_consensus(store, sym)
    checks = {
        "trader_data": (cons.get("contributor_count") or 0) > 0,
        "consensus": cons.get("consensus") is not None,
        "quality_score": cons.get("weighted_score") is not None,
    }
    return _frac(checks), checks


def _macro_domain(store, sym: str) -> tuple[float, dict]:
    # § R1.2: Macro is a global (symbol-independent) environment snapshot. Available when a real snapshot
    # carries the core metrics. No snapshot → NO DATA (never fabricated).
    snap = store.latest_macro_snapshot()
    if snap is None:
        return 0.0, {"macro_snapshot": False, "rates": False, "volatility": False}
    checks = {
        "macro_snapshot": True,
        "rates": snap.fed_rate is not None or snap.treasury_10y is not None,
        "volatility": snap.vix is not None,
    }
    return _frac(checks), checks


_DOMAINS = {
    "market": lambda store, sym, now: _market_domain(store, sym),
    "technical": lambda store, sym, now: _technical_domain(store, sym, now),
    "news": lambda store, sym, now: _news_domain(store, sym),
    "fundamentals": lambda store, sym, now: _fundamentals_domain(store, sym),
    "options": lambda store, sym, now: _options_domain(store, sym),
    "trader": lambda store, sym, now: _trader_domain(store, sym),
    "macro": lambda store, sym, now: _macro_domain(store, sym),
}


def compute_completeness(store, symbol: str, now: datetime | None = None) -> dict:
    """Deterministic data-completeness read-model for a symbol. Read-only; never fabricates a value."""
    now = now or datetime.now(timezone.utc)
    sym = symbol.upper()
    score = 0.0
    details: dict[str, dict] = {}
    available: list[str] = []
    missing: list[str] = []
    partial: list[str] = []
    for key, weight in WEIGHTS.items():
        frac, checks = _DOMAINS[key](store, sym, now)
        score += weight * frac
        is_available = frac >= AVAILABLE_THRESHOLD
        details[key] = {
            "label": DOMAIN_LABELS[key], "weight": round(weight * 100), "score": round(frac * 100, 1),
            "available": is_available, "checks": checks,
        }
        if is_available:
            available.append(key)
        elif frac == 0.0:
            missing.append(key)
        else:
            partial.append(key)
    overall = round(score * 100, 1)
    return {
        "symbol": sym, "score": overall, "state": readiness_state(overall),
        "available": available, "missing": missing, "partial": partial,
        "details": details, "ts": now.isoformat(),
    }


def snapshot_completeness(store, symbol: str, now: datetime | None = None) -> bool:
    """Append an immutable completeness snapshot (one per symbol per hour). Never rewrites history."""
    now = now or datetime.now(timezone.utc)
    c = compute_completeness(store, symbol, now)
    sid = f"{c['symbol']}:{now.strftime('%Y-%m-%dT%H')}"
    store.insert_data_completeness(
        id=sid, symbol=c["symbol"], timestamp=now.isoformat(), overall_score=c["score"],
        state=c["state"], available_sources=json.dumps(c["available"]),
        missing_sources=json.dumps(c["missing"]))
    return True


def record_completeness(store, symbols, now: datetime | None = None) -> int:
    """Snapshot completeness for each watched symbol. Returns the count recorded this cycle."""
    now = now or datetime.now(timezone.utc)
    recorded = 0
    for sym in symbols:
        try:
            if snapshot_completeness(store, sym, now):
                recorded += 1
        except Exception:                                  # one bad symbol never blocks the rest
            continue
    return recorded
