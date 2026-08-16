"""13F quarter-over-quarter position-change analysis (§ Phase R1.3) — deterministic, PURE.

Compares an institution's current-quarter 13F holdings against the prior quarter to classify each
watched symbol as ACCUMULATION / REDUCTION / NEW_POSITION / EXIT, and aggregates the "smart money"
accumulation across institutions into a 0-100 score. Read-only intelligence — no trading, no execution,
no copy-trading. Missing history → no changes (NO DATA), never fabricated.
"""
from __future__ import annotations

ACCUMULATION = "ACCUMULATION"
REDUCTION = "REDUCTION"
NEW_POSITION = "NEW_POSITION"
EXIT = "EXIT"


def _long_shares(holdings: list[dict]) -> dict[str, float]:
    """symbol → net long shares from a parsed 13F holdings list (puts are a separate signal)."""
    out: dict[str, float] = {}
    for h in holdings or []:
        out[h["symbol"]] = out.get(h["symbol"], 0.0) + float(h.get("long_shares") or 0)
    return out


def analyze_changes(institution: str, current: list[dict], previous: list[dict] | None,
                    period: str | None) -> list[dict]:
    """Per-symbol position changes for one institution. Only real, non-zero changes are returned."""
    cur = _long_shares(current)
    prev = _long_shares(previous or [])
    changes: list[dict] = []
    for sym in sorted(set(cur) | set(prev)):
        c = cur.get(sym, 0.0)
        p = prev.get(sym, 0.0)
        if c == 0 and p == 0:
            continue
        change = c - p
        if p == 0 and c > 0:
            direction, pct = NEW_POSITION, None
        elif c == 0 and p > 0:
            direction, pct = EXIT, -100.0
        elif change > 0:
            direction, pct = ACCUMULATION, round(change / p * 100.0, 1)
        elif change < 0:
            direction, pct = REDUCTION, round(change / p * 100.0, 1)
        else:
            continue                                        # unchanged → not a signal
        changes.append({"institution": institution, "symbol": sym, "previous_shares": p,
                        "current_shares": c, "share_change": change, "percentage_change": pct,
                        "direction": direction, "filing_period": period})
    return changes


_BULLISH_DIRS = {ACCUMULATION, NEW_POSITION}
_BEARISH_DIRS = {REDUCTION, EXIT}


def accumulation_score(changes: list) -> float | None:
    """0-100 "smart money" accumulation across institutions for a symbol = share of the changing
    institutions that are ADDING (accumulate / new) vs trimming (reduce / exit). None when no changes."""
    bull = sum(1 for c in changes if _dir(c) in _BULLISH_DIRS)
    bear = sum(1 for c in changes if _dir(c) in _BEARISH_DIRS)
    total = bull + bear
    if total == 0:
        return None
    return round(100.0 * bull / total, 1)


def net_share_change_pct(changes: list) -> float | None:
    """Aggregate net share change (%) across institutions for a symbol: Σchange / Σprevious. None when
    there is no prior base to compare against."""
    total_prev = sum((_num(c, "previous_shares") or 0) for c in changes)
    total_change = sum((_num(c, "share_change") or 0) for c in changes)
    if total_prev <= 0:
        return None
    return round(100.0 * total_change / total_prev, 1)


def _dir(c) -> str | None:
    return c["direction"] if isinstance(c, dict) else getattr(c, "direction", None)


def _num(c, field: str):
    return c[field] if isinstance(c, dict) else getattr(c, field, None)
