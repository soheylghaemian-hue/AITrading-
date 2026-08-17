"""Risk Control configuration validation (§ Phase R2.0) — PURE, deterministic, Decimal.

Validates a proposed Risk Control configuration before it is persisted. It performs NO trading, NO
execution, and NO kill-switch mutation — it only checks that the numbers are sane. Rejects negative
capital, zero/negative limits, out-of-range percentages, inconsistent amount/percentage combinations,
and unknown currencies. `max_position_risk_pct` maps to the canonical `risk_per_trade_pct`.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

ACCEPTED_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD"}
KILL_SWITCH_STATES = {"ARMED", "STOPPED"}      # display-only; the authoritative kill switch is KillSwitchRow

# Example values — for TESTS and UI fixtures ONLY. Never auto-inserted into production as approved limits.
EXAMPLE_CONFIG = {
    "capital": "100000", "currency": "EUR", "max_daily_loss_pct": "1",
    "max_position_risk_pct": "2", "max_portfolio_exposure_pct": "50", "max_drawdown_pct": "10",
    "warning_threshold_pct": "80",
}

# Field → (min_exclusive, max_inclusive) for percentages.
_PCT_BOUNDS = {
    "max_daily_loss_pct": (Decimal(0), Decimal(100)),
    "max_position_risk_pct": (Decimal(0), Decimal(100)),
    "max_portfolio_exposure_pct": (Decimal(0), Decimal(1000)),   # >100 allowed (leverage)
    "max_drawdown_pct": (Decimal(0), Decimal(100)),
    "warning_threshold_pct": (Decimal(0), Decimal(100)),          # a % OF a limit → must be < 100
}
_REQUIRED = ["capital", "currency", "max_daily_loss_pct", "max_position_risk_pct",
             "max_portfolio_exposure_pct", "max_drawdown_pct", "warning_threshold_pct"]


def _dec(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return "INVALID"


def validate_config(payload: dict | None) -> tuple[dict | None, list[str]]:
    """Return (normalized {field: Decimal/str}, errors[]). Non-empty errors → reject (no persistence)."""
    payload = payload or {}
    errors: list[str] = []
    out: dict = {}

    for f in _REQUIRED:
        if payload.get(f) in (None, ""):
            errors.append(f"MISSING_{f.upper()}")

    cap = _dec(payload.get("capital"))
    if cap == "INVALID":
        errors.append("INVALID_CAPITAL")
    elif cap is not None:
        if cap <= 0:
            errors.append("CAPITAL_MUST_BE_POSITIVE")
        out["capital"] = cap

    cur = payload.get("currency")
    if cur is not None:
        if str(cur).upper() not in ACCEPTED_CURRENCIES:
            errors.append("UNKNOWN_CURRENCY")
        else:
            out["currency"] = str(cur).upper()

    for f, (lo, hi) in _PCT_BOUNDS.items():
        v = _dec(payload.get(f))
        if v == "INVALID":
            errors.append(f"INVALID_{f.upper()}")
        elif v is not None:
            if v <= lo:
                errors.append(f"{f.upper()}_MUST_BE_POSITIVE")
            elif v > hi:
                errors.append(f"{f.upper()}_OUT_OF_RANGE")
            elif f == "warning_threshold_pct" and v >= 100:
                errors.append("WARNING_THRESHOLD_MUST_BE_BELOW_100")
            else:
                out[f] = v

    # Optional explicit daily-loss amount must be consistent with capital × pct (else reject — no silent
    # divergence). We otherwise DERIVE the amount, never persist it.
    amt = _dec(payload.get("max_daily_loss_amount"))
    if amt not in (None, "INVALID") and "capital" in out and "max_daily_loss_pct" in out:
        derived = (out["capital"] * out["max_daily_loss_pct"] / Decimal(100))
        if abs(amt - derived) > Decimal("0.01"):
            errors.append("INCONSISTENT_DAILY_LOSS_AMOUNT")

    return (out if not errors else None), errors
