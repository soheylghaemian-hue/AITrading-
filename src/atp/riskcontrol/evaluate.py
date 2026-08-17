"""Risk Control deterministic state evaluation (§ Phase R2.0) — PURE, Decimal, fail-closed.

Turns the combined canonical config + the currently-available portfolio facts into a capital-protection
state: READY / WARNING / BLOCKED / NO DATA, with machine-readable reason codes. It is an OBSERVABILITY
and GOVERNANCE gate only — it never calls the broker/execution/order/RiskEngine paths, never mutates the
kill switch, and can never create or submit an order.

Fail-closed rules:
  * STOPPED kill switch (or risk_state.killed) → BLOCKED (highest precedence).
  * No valid configuration → NO DATA (RISK_CONFIGURATION_MISSING) — never READY.
  * Any missing/stale MANDATORY input (capital, daily P&L, exposure, drawdown) → NO DATA
    (RISK_DATA_MISSING + the missing fields). Missing data is NEVER treated as zero, NEVER as READY.
  * A limit exceeded → BLOCKED. A warning threshold reached → WARNING. Otherwise → READY.
Zero is used only when it is an OBSERVED value (e.g. a flat P&L that was actually sourced).
"""
from __future__ import annotations

from decimal import Decimal

READY, WARNING, BLOCKED, NO_DATA = "READY", "WARNING", "BLOCKED", "NO DATA"
_Z = Decimal(0)
_HUNDRED = Decimal(100)

# Mandatory inputs that must be present (and fresh) before the system can be READY.
MANDATORY = ("capital", "daily_pnl", "exposure", "drawdown")


def _pct_of(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole is None or whole <= 0 else (part / whole * _HUNDRED)


def evaluate_risk_state(*, config: dict | None, daily_pnl, risk_state, kill_switch,
                        exposure: dict | None = None, equity: Decimal | None = None) -> dict:
    """`config` = the validated combined view (capital, max_daily_loss_pct, max_position_risk_pct,
    max_portfolio_exposure_pct, max_drawdown_pct, warning_threshold_pct) or None. `daily_pnl` =
    DailyPnlRow|None. `risk_state` = RiskStateRow|None. `kill_switch` = KillSwitchRow. `exposure` =
    {gross_pct, net_pct}|None. `equity` = live account equity Decimal|None. Returns a structured result."""
    reasons: list[str] = []
    missing: list[str] = []
    kill_stopped = bool(getattr(kill_switch, "engaged", False)) or bool(getattr(risk_state, "killed", False))

    # ---- derive whatever is available (Decimal), marking absent inputs as missing (never zero) ----
    capital = config.get("capital") if config else None
    # daily P&L: realized + unrealized (an OBSERVED value; a flat/profit day is a real 0, not "missing")
    daily = None
    if daily_pnl is not None:
        daily = Decimal(daily_pnl.realized_pnl) + Decimal(daily_pnl.unrealized_pnl)
    daily_loss = None if daily is None else max(_Z, -daily)
    daily_limit = (capital * config["max_daily_loss_pct"] / _HUNDRED) if (config and capital is not None) else None
    daily_used_pct = _pct_of(daily_loss, daily_limit) if (daily_loss is not None and daily_limit) else None
    daily_remaining = (daily_limit - daily_loss) if (daily_limit is not None and daily_loss is not None) else None

    gross = exposure.get("gross_pct") if exposure else None
    net = exposure.get("net_pct") if exposure else None
    drawdown_pct = None
    if risk_state is not None and equity is not None and Decimal(risk_state.peak_equity) > 0:
        peak = Decimal(risk_state.peak_equity)
        drawdown_pct = max(_Z, (peak - equity) / peak * _HUNDRED)

    # ---- fail-closed precedence ----
    # 1) Active hard stop → BLOCKED regardless of config/data.
    if kill_stopped:
        reasons.append("KILL_SWITCH_TRIGGERED")
        status = BLOCKED
        return _result(status, reasons, missing, config, capital, daily, daily_loss, daily_limit,
                       daily_used_pct, daily_remaining, gross, net, drawdown_pct, kill_switch, daily_pnl)

    # 2) No valid configuration → NO DATA (must not be READY; does NOT force BLOCKED downstream).
    if not config:
        reasons.append("RISK_CONFIGURATION_MISSING")
        return _result(NO_DATA, reasons, list(MANDATORY), config, None, None, None, None, None, None,
                       None, None, None, kill_switch, daily_pnl)

    # 3) Mandatory inputs present?
    if capital is None:
        missing.append("capital")
    if daily is None:
        missing.append("daily_pnl")
    if gross is None:
        missing.append("exposure")
    if drawdown_pct is None:
        missing.append("drawdown")
    if missing:
        reasons.append("RISK_DATA_MISSING")
        return _result(NO_DATA, reasons, missing, config, capital, daily, daily_loss, daily_limit,
                       daily_used_pct, daily_remaining, gross, net, drawdown_pct, kill_switch, daily_pnl)

    # 4) BLOCKING breaches (>= for the daily boundary — an exact hit blocks).
    warn = config["warning_threshold_pct"]
    if daily_loss >= daily_limit:
        reasons.append("DAILY_LOSS_LIMIT_EXCEEDED")
    if gross > config["max_portfolio_exposure_pct"]:
        reasons.append("EXPOSURE_LIMIT_EXCEEDED")
    if drawdown_pct > config["max_drawdown_pct"]:
        reasons.append("DRAWDOWN_LIMIT_EXCEEDED")
    # position risk is only evaluated when observed (not fabricated when flat/unknown)
    pos_risk = exposure.get("position_risk_pct") if exposure else None
    if pos_risk is not None and pos_risk > config["max_position_risk_pct"]:
        reasons.append("POSITION_RISK_EXCEEDED")
    if reasons:
        return _result(BLOCKED, reasons, missing, config, capital, daily, daily_loss, daily_limit,
                       daily_used_pct, daily_remaining, gross, net, drawdown_pct, kill_switch, daily_pnl, pos_risk)

    # 5) WARNINGS (>= threshold fraction of a limit).
    if daily_loss >= (daily_limit * warn / _HUNDRED):
        reasons.append("DAILY_LOSS_WARNING")
    if gross >= (config["max_portfolio_exposure_pct"] * warn / _HUNDRED):
        reasons.append("EXPOSURE_WARNING")
    if drawdown_pct >= (config["max_drawdown_pct"] * warn / _HUNDRED):
        reasons.append("DRAWDOWN_WARNING")
    status = WARNING if reasons else READY
    return _result(status, reasons, missing, config, capital, daily, daily_loss, daily_limit,
                   daily_used_pct, daily_remaining, gross, net, drawdown_pct, kill_switch, daily_pnl, pos_risk)


def _result(status, reasons, missing, config, capital, daily, daily_loss, daily_limit, daily_used_pct,
            daily_remaining, gross, net, drawdown_pct, kill_switch, daily_pnl, pos_risk=None) -> dict:
    return {
        "status": status, "reasons": reasons, "missing": missing,
        "capital": capital, "currency": (config.get("currency") if config else None),
        "daily_pnl": daily, "daily_loss": daily_loss, "daily_limit": daily_limit,
        "daily_used_pct": daily_used_pct, "daily_remaining": daily_remaining,
        "daily_observed_at": (daily_pnl.updated_at if daily_pnl is not None else None),
        "position_risk": pos_risk, "position_risk_limit": (config.get("max_position_risk_pct") if config else None),
        "gross_pct": gross, "net_pct": net,
        "exposure_limit_pct": (config.get("max_portfolio_exposure_pct") if config else None),
        "drawdown_pct": drawdown_pct, "drawdown_limit_pct": (config.get("max_drawdown_pct") if config else None),
        "kill_switch": ("STOPPED" if getattr(kill_switch, "engaged", False) else "ARMED"),
    }
