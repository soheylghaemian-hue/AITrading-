"""Macro regime engine (§ Phase R1.2) — deterministic. PURE.

Turns a macro snapshot (+ the prior snapshot for trend) into a 0-100 macro-environment score, a market
regime (RISK_ON / RISK_NEUTRAL / RISK_OFF) and human-readable signals/risks. Higher score = more
risk-supportive. Only sub-scores whose inputs exist contribute (weights renormalise), so partial data
degrades gracefully and missing data is never fabricated. No trading, no execution.

Accepts any object exposing the metric attributes (fed_rate, treasury_10y, treasury_2y, cpi,
unemployment, vix, dxy, oil, gold) — both MacroMetrics and the store's MacroSnapshotRow qualify.
"""
from __future__ import annotations

# Sub-score weights (renormalised over available components).
WEIGHTS = {"volatility": 0.30, "curve": 0.20, "rates_trend": 0.15, "inflation": 0.20, "usd": 0.15}

RISK_ON_MIN = 65.0
RISK_OFF_MAX = 40.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _get(m, field: str):
    return getattr(m, field, None) if m is not None else None


# ---------------------------------------------------------------- sub-scores (0-100, higher = risk-on)
def _volatility_score(cur, prev):
    vix = _get(cur, "vix")
    if vix is None:
        return None
    # Centred on the long-run average: VIX 20 → 50 (neutral); 12 → 90 (calm/risk-on); 30 → 0 (fear).
    score = _clamp(50 + (20 - vix) * 5)
    pvix = _get(prev, "vix")
    if pvix is not None:
        score = _clamp(score + (8 if vix < pvix - 0.5 else -8 if vix > pvix + 0.5 else 0))
    return score


def _curve_score(cur, prev):
    t10, t2 = _get(cur, "treasury_10y"), _get(cur, "treasury_2y")
    if t10 is None or t2 is None:
        return None
    spread = t10 - t2                                      # inverted (<0) = recession signal = risk-off
    return _clamp(50 + spread * 40)                        # +0.5 → 70, 0 → 50, -0.5 → 30


def _rates_trend_score(cur, prev):
    t10 = _get(cur, "treasury_10y")
    p10 = _get(prev, "treasury_10y")
    if t10 is None:
        return None
    if p10 is None:
        return 50.0                                        # no trend yet → neutral
    d = t10 - p10                                          # rising yields = tightening = risk-off
    return _clamp(50 - d * 100)                            # +0.25 → 25, flat → 50, -0.25 → 75


def _inflation_score(cur, prev):
    cpi = _get(cur, "cpi")
    if cpi is None:
        return None
    score = _clamp(100 - max(0.0, cpi - 2.0) * 15)         # 2% target → 100, 6% → 40
    pcpi = _get(prev, "cpi")
    if pcpi is not None:
        score = _clamp(score + (8 if cpi < pcpi - 0.05 else -8 if cpi > pcpi + 0.05 else 0))
    return score


def _usd_score(cur, prev):
    dxy = _get(cur, "dxy")
    if dxy is None:
        return None
    pdxy = _get(prev, "dxy")
    if pdxy is None:
        return 50.0
    # A strengthening USD tightens global conditions → risk-off; weakening → risk-on.
    chg = (dxy - pdxy) / pdxy * 100 if pdxy else 0.0
    return _clamp(50 - chg * 10)


def macro_score(cur, prev=None) -> float | None:
    """Weighted 0-100 macro-environment score over the available sub-scores. None if nothing to score."""
    subs = {"volatility": _volatility_score(cur, prev), "curve": _curve_score(cur, prev),
            "rates_trend": _rates_trend_score(cur, prev), "inflation": _inflation_score(cur, prev),
            "usd": _usd_score(cur, prev)}
    num = den = 0.0
    for k, w in WEIGHTS.items():
        v = subs[k]
        if v is not None:
            num += w * v
            den += w
    return round(num / den, 1) if den > 0 else None


def classify_regime(cur, prev=None) -> str | None:
    """RISK_ON / RISK_NEUTRAL / RISK_OFF from the macro score. None → the caller shows NO DATA."""
    s = macro_score(cur, prev)
    if s is None:
        return None
    return "RISK_ON" if s >= RISK_ON_MIN else "RISK_OFF" if s < RISK_OFF_MAX else "RISK_NEUTRAL"


def signals_and_risks(cur, prev=None) -> tuple[list[str], list[str]]:
    """Deterministic positive signals + risks from the macro conditions. Never invents a condition."""
    signals: list[str] = []
    risks: list[str] = []
    vix, pvix = _get(cur, "vix"), _get(prev, "vix")
    t10, p10 = _get(cur, "treasury_10y"), _get(prev, "treasury_10y")
    t2 = _get(cur, "treasury_2y")
    cpi, pcpi = _get(cur, "cpi"), _get(prev, "cpi")
    fed = _get(cur, "fed_rate")

    if vix is not None:
        if pvix is not None and vix < pvix - 0.5:
            signals.append("Volatility decreasing")
        if vix < 16:
            signals.append("Low volatility regime")
        if (pvix is not None and vix > pvix + 0.5) or vix > 25:
            risks.append("Rising volatility")
    if cpi is not None:
        if pcpi is not None and cpi < pcpi - 0.05:
            signals.append("Inflation improving")
        elif cpi > 4.0:
            risks.append("Inflation elevated")
    if t10 is not None and t2 is not None and t10 < t2:
        risks.append("Inverted yield curve")
    if t10 is not None and p10 is not None:
        if t10 > p10 + 0.05:
            risks.append("Rising yields")
        elif abs(t10 - p10) <= 0.05:
            signals.append("Stable rates")
    if (fed is not None and fed >= 4.5) or (t10 is not None and t10 >= 4.5):
        risks.append("Rates elevated")

    # de-dupe, preserve order
    def _dedupe(xs):
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    return _dedupe(signals), _dedupe(risks)
