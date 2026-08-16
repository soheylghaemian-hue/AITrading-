"""Insider cluster detection (§ Phase R1.4) — deterministic, PURE.

Detects meaningful CLUSTERS of insider buying/selling from SEC Form 4 transactions: multiple insiders
trading the same way within a time window, weighted by role (a CEO/CFO matters more than a junior
officer). Produces an ACCUMULATION / DISTRIBUTION / NONE label and a 0-100 cluster score. Intelligence
only — never a trading signal, order, or execution. No transactions → NO DATA (never a fabricated
cluster). A single insider's activity is NOT a cluster (a cluster needs multiple distinct insiders).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

ACCUMULATION = "ACCUMULATION"
DISTRIBUTION = "DISTRIBUTION"
NONE = "NONE"

# Supported look-back windows (days).
WINDOWS = [7, 30, 90]
PRIMARY_WINDOW = 30                                    # the headline window
NEUTRAL_SCORE = 50.0

# Role → weighted importance (per spec). Matched case-insensitively against the Form 4 title.
_ROLE_WEIGHTS = [
    ("chief executive", 5), ("ceo", 5),
    ("chief financial", 5), ("cfo", 5),
    ("chairman", 4),
    ("director", 3),
    ("chief", 2), ("president", 2), ("officer", 2), ("evp", 2), ("svp", 2),
    ("vice president", 2), ("vp", 2), ("executive", 2),
]
DEFAULT_WEIGHT = 1                                      # 10% owner / unknown role

# A cluster must move at least this much value (USD) in the dominant direction to count (else NONE).
def _min_cluster_value() -> float:
    try:
        return float(os.environ.get("ATP_INSIDER_CLUSTER_MIN_VALUE", "1000000"))
    except ValueError:
        return 1_000_000.0


def role_weight(title: str | None) -> int:
    """Role importance weight from a Form 4 title. CEO/CFO 5, Chairman 4, Director 3, Executive 2, else 1."""
    t = (title or "").strip().lower()
    if not t:
        return DEFAULT_WEIGHT
    for needle, w in _ROLE_WEIGHTS:
        if needle in t:
            return w
    return DEFAULT_WEIGHT


def _get(t, field: str):
    return t.get(field) if isinstance(t, dict) else getattr(t, field, None)


def _parse_date(s):
    if not s:
        return None
    try:
        d = datetime.strptime(str(s)[:10], "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _within(t, cutoff, now):
    d = _parse_date(_get(t, "transaction_date"))
    return d is not None and cutoff <= d <= now


def detect_cluster(transactions: list, window_days: int, now: datetime | None = None) -> dict:
    """Detect the insider cluster within a look-back window. Returns cluster_type, a 0-100 score, the
    distinct participant count and aggregate shares/value. No in-window transactions → NO DATA."""
    now = now or datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = now - timedelta(days=window_days)
    inwin = [t for t in transactions if _within(t, cutoff, now)]
    if not inwin:
        return {"time_window": f"{window_days}d", "cluster_type": None, "score": None,
                "insider_count": 0, "buy_count": 0, "sell_count": 0, "total_shares": 0.0,
                "total_value": 0.0, "participants": []}

    # Aggregate per distinct insider: their net role-weighted direction + shares/value.
    per: dict[str, dict] = {}
    for t in inwin:
        name = (_get(t, "insider_name") or "?").strip()
        ttype = _get(t, "transaction_type")
        shares = float(_get(t, "shares") or 0)
        price = float(_get(t, "price") or 0)
        p = per.setdefault(name, {"name": name, "title": _get(t, "title"), "weight": role_weight(_get(t, "title")),
                                  "buy_shares": 0.0, "sell_shares": 0.0, "value": 0.0})
        if ttype == "BUY":
            p["buy_shares"] += shares
        elif ttype == "SELL":
            p["sell_shares"] += shares
        p["value"] += shares * price

    buyers = [p for p in per.values() if p["buy_shares"] > p["sell_shares"]]
    sellers = [p for p in per.values() if p["sell_shares"] > p["buy_shares"]]
    buy_w = sum(p["weight"] for p in buyers)
    sell_w = sum(p["weight"] for p in sellers)
    total_w = buy_w + sell_w

    buy_count = sum(1 for t in inwin if _get(t, "transaction_type") == "BUY")
    sell_count = sum(1 for t in inwin if _get(t, "transaction_type") == "SELL")
    buy_shares = sum(float(_get(t, "shares") or 0) for t in inwin if _get(t, "transaction_type") == "BUY")
    sell_shares = sum(float(_get(t, "shares") or 0) for t in inwin if _get(t, "transaction_type") == "SELL")
    buy_value = sum(p["value"] for p in buyers)
    sell_value = sum(p["value"] for p in sellers)

    # Directional lean in [-1, 1], role-weighted; dampened by conviction (more distinct insiders = stronger).
    lean = (buy_w - sell_w) / total_w if total_w > 0 else 0.0
    conviction = min(1.0, len(per) / 3.0)
    score = round(min(100.0, max(0.0, NEUTRAL_SCORE + 45.0 * lean * conviction)), 1)

    min_val = _min_cluster_value()
    if len(buyers) >= 2 and buy_w > sell_w and buy_value >= min_val:
        cluster_type, total_shares, total_value = ACCUMULATION, buy_shares, buy_value
    elif len(sellers) >= 2 and sell_w > buy_w and sell_value >= min_val:
        cluster_type, total_shares, total_value = DISTRIBUTION, sell_shares, sell_value
    else:
        cluster_type, total_shares, total_value = NONE, buy_shares + sell_shares, buy_value + sell_value

    participants = sorted(({"name": p["name"], "title": p["title"], "weight": p["weight"],
                            "direction": "BUY" if p["buy_shares"] > p["sell_shares"]
                            else "SELL" if p["sell_shares"] > p["buy_shares"] else "MIXED"}
                           for p in per.values()), key=lambda x: -x["weight"])
    return {"time_window": f"{window_days}d", "cluster_type": cluster_type, "score": score,
            "insider_count": len(per), "buy_count": buy_count, "sell_count": sell_count,
            "total_shares": round(total_shares, 0), "total_value": round(total_value, 0),
            "participants": participants}


def _summary(headline: dict) -> str:
    ct = headline["cluster_type"]
    n = headline["insider_count"]
    if ct == ACCUMULATION:
        return f"Insider accumulation cluster — {n} insiders buying"
    if ct == DISTRIBUTION:
        return f"Insider distribution cluster — {n} insiders selling"
    if headline["sell_count"] and not headline["buy_count"]:
        return f"{headline['sell_count']} insider sell transaction(s) — no cluster"
    if headline["buy_count"] and not headline["sell_count"]:
        return f"{headline['buy_count']} insider buy transaction(s) — no cluster"
    return "No significant insider cluster"


def build_insider_cluster(store, symbol: str, now: datetime | None = None) -> dict:
    """The insider-cluster read-model for a symbol across the supported windows. NO DATA until Form 4
    transactions exist (never a fabricated cluster). Read-only; never a trade."""
    now = now or datetime.now(timezone.utc)
    sym = symbol.upper()
    txns = store.list_insider_transactions(sym, 500)
    windows = {w: detect_cluster(txns, w, now) for w in WINDOWS}
    headline = windows[PRIMARY_WINDOW]
    if not txns or headline["cluster_type"] is None:
        return {"symbol": sym, "status": "NO DATA", "cluster_type": None, "score": None,
                "insider_count": 0, "time_window": f"{PRIMARY_WINDOW}d", "participants": [],
                "buy_count": 0, "sell_count": 0, "total_shares": 0.0, "total_value": 0.0,
                "summary": "No Form 4 data", "windows": windows}
    return {"symbol": sym, "status": "COMPLETE", "cluster_type": headline["cluster_type"],
            "score": headline["score"], "insider_count": headline["insider_count"],
            "time_window": headline["time_window"], "participants": headline["participants"],
            "buy_count": headline["buy_count"], "sell_count": headline["sell_count"],
            "total_shares": headline["total_shares"], "total_value": headline["total_value"],
            "summary": _summary(headline), "windows": windows}
