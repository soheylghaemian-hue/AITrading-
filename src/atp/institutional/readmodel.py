"""Institutional flow read-model (§ Phase R1.3) — PURE (reads persisted history only).

`build_institutional_flow` assembles the "smart money" picture for a symbol: the 13F quarter-over-quarter
position changes + an accumulation score, and the recent insider BUY/SELL activity + insider sentiment.
No data → NO DATA (never fabricated). Read-only intelligence input; never a trade, order, or broker
action.
"""
from __future__ import annotations

from .changes import accumulation_score, net_share_change_pct
from .insider import insider_sentiment


def build_institutional_flow(store, symbol: str, limit: int = 50) -> dict:
    sym = symbol.upper()
    changes = store.list_institutional_changes(sym, limit)
    insiders = store.list_insider_transactions(sym, limit)

    change_items = [{
        "institution": c.institution, "symbol": c.symbol, "previous_shares": c.previous_shares,
        "current_shares": c.current_shares, "share_change": c.share_change,
        "percentage_change": c.percentage_change, "direction": c.direction,
        "filing_period": c.filing_period} for c in changes]
    acc_score = accumulation_score(changes)
    net_pct = net_share_change_pct(changes)
    # Overall institutional direction from the accumulation score.
    inst_direction = (None if acc_score is None else
                      "ACCUMULATION" if acc_score >= 60 else "REDUCTION" if acc_score < 40 else "MIXED")

    insider_items = [{
        "insider_name": t.insider_name, "title": t.title, "transaction_type": t.transaction_type,
        "shares": t.shares, "price": t.price, "transaction_date": t.transaction_date} for t in insiders]
    isent = insider_sentiment(insiders)

    status = "COMPLETE" if (change_items or insider_items) else "NO DATA"
    return {
        "symbol": sym, "status": status,
        "institutional_changes": change_items,
        "institutional_direction": inst_direction,
        "accumulation_score": acc_score,
        "net_share_change_pct": net_pct,
        "insider_activity": insider_items,
        "insider_sentiment": isent["sentiment"],
        "insider_score": isent["score"],
        "insider_summary": {k: isent[k] for k in
                            ("buy_count", "sell_count", "buy_shares", "sell_shares", "distinct_buyers")},
    }
