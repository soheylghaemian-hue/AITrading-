"""Options Analytics Engine (§ Phase G2.3) — PURE, deterministic. No randomness, fully testable.

aggregate_chain() folds a real option chain into per-symbol flow (call/put volume, put/call ratio,
volume-weighted IV, total OI, premium $ volume, large-trade count, unusual-activity score, sentiment).
options_score() weights it into a 0-100 Options Intelligence Score:

    Volume Activity      25%   — volume relative to open interest
    Open Interest Change 20%   — needs snapshot history → NO DATA today (table enables it; drops+renorm)
    IV Signal            20%   — implied-volatility level
    Call/Put Balance     20%   — directional balance (bullish tilt scores higher)
    Large Trade Activity 15%   — large-premium concentration

Only components with real data contribute (weights renormalize). Nothing is fabricated — every output
is a function of the real chain. Signals/risks are a transparent labelling of the same flow, never a
buy/sell decision.
"""
from __future__ import annotations

from dataclasses import dataclass

WEIGHTS = {"volume_activity": 0.25, "oi_change": 0.20, "iv_signal": 0.20, "cp_balance": 0.20, "large_trade": 0.15}
_LARGE_PREMIUM = 1_000_000.0     # a single-contract premium above this counts as a "large trade"


@dataclass(slots=True)
class OptionsFlow:
    symbol: str
    call_volume: int | None = None
    put_volume: int | None = None
    call_put_ratio: float | None = None      # put/call ratio (PCR): <0.7 bullish, >1.0 bearish
    implied_volatility: float | None = None
    open_interest: int | None = None
    unusual_activity_score: float | None = None
    large_trade_count: int | None = None
    premium_volume: float | None = None
    sentiment: str | None = None


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def sentiment_from_pcr(pcr: float | None) -> str | None:
    if pcr is None:
        return None
    if pcr < 0.7:
        return "Bullish"
    if pcr > 1.0:
        return "Bearish"
    return "Neutral"


def aggregate_chain(contracts, symbol: str) -> OptionsFlow | None:
    """Fold a real option chain into per-symbol flow. None when there is no activity (NO DATA)."""
    calls = [c for c in contracts if c.option_type == "call"]
    puts = [c for c in contracts if c.option_type == "put"]
    call_vol = sum((c.volume or 0) for c in calls)
    put_vol = sum((c.volume or 0) for c in puts)
    total_oi = sum((c.open_interest or 0) for c in contracts)
    if call_vol == 0 and put_vol == 0 and total_oi == 0:
        return None
    pcr = round(put_vol / call_vol, 4) if call_vol > 0 else None

    ivw = [(c.implied_volatility, (c.volume or 0)) for c in contracts if c.implied_volatility is not None]
    tw = sum(w for _, w in ivw)
    if tw > 0:
        iv = round(sum(v * w for v, w in ivw) / tw, 4)
    elif ivw:
        iv = round(sum(v for v, _ in ivw) / len(ivw), 4)
    else:
        iv = None

    premium = 0.0
    large = 0
    for c in contracts:
        price = c.last if c.last is not None else (
            ((c.bid or 0) + (c.ask or 0)) / 2 if (c.bid is not None or c.ask is not None) else None)
        if price is not None and c.volume:
            p = price * c.volume * 100.0
            premium += p
            if p > _LARGE_PREMIUM:
                large += 1

    total_vol = call_vol + put_vol
    parts = []
    if total_oi > 0:
        parts.append(_clamp(100.0 * total_vol / total_oi))
    parts.append(_clamp(large * 20.0))
    parts.append(_clamp(premium / 500_000.0))               # $50M → 100
    unusual = round(sum(parts) / len(parts), 1) if parts else None

    return OptionsFlow(
        symbol=symbol.upper(), call_volume=call_vol, put_volume=put_vol, call_put_ratio=pcr,
        implied_volatility=iv, open_interest=total_oi, unusual_activity_score=unusual,
        large_trade_count=large, premium_volume=round(premium, 2), sentiment=sentiment_from_pcr(pcr))


def score_components(flow) -> dict[str, float | None]:
    if flow is None:
        return {k: None for k in WEIGHTS}
    total_vol = (flow.call_volume or 0) + (flow.put_volume or 0)
    volume_activity = None
    if flow.open_interest and flow.open_interest > 0:
        volume_activity = round(_clamp(100.0 * total_vol / flow.open_interest * 2.0), 2)
    iv_signal = round(_clamp((flow.implied_volatility or 0) * 160.0), 2) if flow.implied_volatility is not None else None
    cp_balance = None
    if flow.call_put_ratio is not None:
        cp_balance = round(_clamp(100.0 - (flow.call_put_ratio - 0.4) * 80.0), 2)   # bullish (low PCR) → higher
    large_trade = round(_clamp((flow.large_trade_count or 0) * 20.0), 2) if flow.large_trade_count is not None else None
    return {"volume_activity": volume_activity, "oi_change": None, "iv_signal": iv_signal,
            "cp_balance": cp_balance, "large_trade": large_trade}


def options_score(flow) -> float | None:
    """Overall 0-100 Options Intelligence Score — weighted mean over available components. None when no data."""
    if flow is None:
        return None
    subs = score_components(flow)
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = subs.get(k)
        if v is not None:
            num += w * v
            den += w
    return round(num / den, 1) if den > 0 else None


def unusual_activity_label(flow) -> str | None:
    if flow is None:
        return None
    if (flow.large_trade_count or 0) > 0 or (flow.premium_volume or 0) > 10_000_000 \
            or (flow.unusual_activity_score or 0) >= 40:
        return "Detected"
    return "Normal"


def signals_and_risks(flow) -> tuple[list[str], list[str]]:
    """Deterministic ✓ signals / ⚠ risks from the SAME real flow (never fabricated, never a trade signal)."""
    signals: list[str] = []
    risks: list[str] = []
    if flow is None:
        return signals, risks
    cv = flow.call_volume or 0
    pv = flow.put_volume or 0
    if cv > 0 and cv > pv * 1.3:
        signals.append("High call activity")
    if flow.premium_volume is not None and flow.premium_volume > 10_000_000:
        signals.append("Large premium trades detected")
    if flow.call_put_ratio is not None and flow.call_put_ratio < 0.7:
        signals.append("Positive positioning")
    if flow.implied_volatility is not None and flow.implied_volatility > 0.4:
        risks.append("Elevated implied volatility")
    if flow.call_put_ratio is not None and flow.call_put_ratio > 1.2:
        risks.append("Heavy put buying")
    return signals, risks
