"""Insider signal engine (§ Phase R1.3) — deterministic, PURE.

Turns recent open-market insider transactions (Form 4 BUY/SELL) into a sentiment: multiple / large
buys are bullish; heavy selling is bearish. Share-weighted so a big purchase outweighs a token one,
with a small nudge for a cluster of distinct buyers. No transactions → NO DATA (never fabricated).
Read-only intelligence; never a trade.
"""
from __future__ import annotations

BULLISH_MIN = 60.0
BEARISH_MAX = 40.0


def _shares(t) -> float:
    v = t.get("shares") if isinstance(t, dict) else getattr(t, "shares", None)
    return float(v) if v is not None else 0.0


def _type(t) -> str | None:
    return t.get("transaction_type") if isinstance(t, dict) else getattr(t, "transaction_type", None)


def _name(t) -> str | None:
    return t.get("insider_name") if isinstance(t, dict) else getattr(t, "insider_name", None)


def insider_sentiment(transactions: list) -> dict:
    """{sentiment, score, buy_count, sell_count, buy_shares, sell_shares, distinct_buyers}. Score is the
    share-weighted BUY share of activity (0-100); >=60 BULLISH, <40 BEARISH, else NEUTRAL."""
    buys = [t for t in transactions if _type(t) == "BUY"]
    sells = [t for t in transactions if _type(t) == "SELL"]
    if not buys and not sells:
        return {"sentiment": None, "score": None, "buy_count": 0, "sell_count": 0,
                "buy_shares": 0.0, "sell_shares": 0.0, "distinct_buyers": 0}
    buy_sh = sum(_shares(t) for t in buys)
    sell_sh = sum(_shares(t) for t in sells)
    total = buy_sh + sell_sh
    base = round(100.0 * buy_sh / total, 1) if total > 0 else (100.0 if buys else 0.0)
    distinct_buyers = len({_name(t) for t in buys if _name(t)})
    # A cluster of distinct buyers is a stronger bullish tell — small, bounded nudge.
    score = min(100.0, base + (5.0 if distinct_buyers >= 2 else 0.0)) if buys else base
    sentiment = "BULLISH" if score >= BULLISH_MIN else "BEARISH" if score < BEARISH_MAX else "NEUTRAL"
    return {"sentiment": sentiment, "score": round(score, 1), "buy_count": len(buys),
            "sell_count": len(sells), "buy_shares": round(buy_sh, 0), "sell_shares": round(sell_sh, 0),
            "distinct_buyers": distinct_buyers}
