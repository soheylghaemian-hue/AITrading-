"""Risk Control read-models (§ Phase R2.0) — PURE, side-effect-free (GET-safe).

Assembles the /risk/status, /risk/config and /risk/events responses from the CANONICAL risk_config +
the risk_control_policy companion + already-persisted portfolio facts (daily_pnl, risk_state,
kill_switch). No field is duplicated: capital / risk_per_trade_pct(=max_position_risk_pct) /
max_daily_loss_pct come from risk_config; the rest from risk_control_policy; max_daily_loss_amount and
all usage figures are DERIVED (never stored). Missing data → null / NO DATA (never zero, never READY).
Read-only: never trades, never mutates the kill switch, never creates an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..store import risk_config_token
from .evaluate import evaluate_risk_state

_HUNDRED = Decimal(100)


def _f(v):
    return None if v is None else float(v)


def combined_config(store):
    """The canonical combined Risk Control config. Returns (cfg_dict_or_None, rc, pol, version, token,
    complete). `cfg` is populated only when the config is COMPLETE (all canonical + policy fields present)."""
    rc = store.get_risk_config()
    pol = store.get_risk_control_policy()
    complete = (rc is not None and pol is not None
                and rc.capital is not None and rc.risk_per_trade_pct is not None
                and rc.max_daily_loss_pct is not None and pol.currency is not None
                and pol.warning_threshold_pct is not None and pol.max_portfolio_exposure_pct is not None
                and pol.max_drawdown_pct is not None)
    cfg = None
    if complete:
        cfg = {"capital": Decimal(rc.capital), "max_daily_loss_pct": Decimal(rc.max_daily_loss_pct),
               "max_position_risk_pct": Decimal(rc.risk_per_trade_pct),
               "max_portfolio_exposure_pct": Decimal(pol.max_portfolio_exposure_pct),
               "max_drawdown_pct": Decimal(pol.max_drawdown_pct),
               "warning_threshold_pct": Decimal(pol.warning_threshold_pct), "currency": pol.currency}
    token = risk_config_token(
        capital=(Decimal(rc.capital) if rc else None),
        risk_per_trade_pct=(Decimal(rc.risk_per_trade_pct) if rc else None),
        max_daily_loss_pct=(Decimal(rc.max_daily_loss_pct) if rc else None),
        rc_updated_at=(rc.updated_at if rc else None),
        config_version=(pol.config_version if pol else 0), currency=(pol.currency if pol else None),
        warning_threshold_pct=(Decimal(pol.warning_threshold_pct) if pol and pol.warning_threshold_pct is not None else None),
        max_portfolio_exposure_pct=(Decimal(pol.max_portfolio_exposure_pct) if pol and pol.max_portfolio_exposure_pct is not None else None),
        max_drawdown_pct=(Decimal(pol.max_drawdown_pct) if pol and pol.max_drawdown_pct is not None else None))
    return cfg, rc, pol, (pol.config_version if pol else None), token, complete


def build_risk_config_view(store) -> dict:
    """The /risk/config response: the validated combined config WITHOUT secrets, with the derived
    max_daily_loss_amount + its calculation basis + the concurrency token. NO DATA when incomplete."""
    cfg, rc, pol, version, token, complete = combined_config(store)
    kill = "STOPPED" if store.get_kill_switch().engaged else "ARMED"
    if not complete:
        return {"configured": False, "reason": "RISK_CONFIGURATION_MISSING",
                "configuration_version": version, "version_token": token, "kill_switch": kill, "config": None}
    amount = cfg["capital"] * cfg["max_daily_loss_pct"] / _HUNDRED
    return {
        "configured": True, "configuration_version": version, "version_token": token, "kill_switch": kill,
        "config": {
            "capital": _f(cfg["capital"]), "currency": cfg["currency"],
            "max_daily_loss_pct": _f(cfg["max_daily_loss_pct"]),
            "max_daily_loss_amount": _f(amount),
            "max_daily_loss_amount_basis": "capital * max_daily_loss_pct / 100 (derived, Decimal)",
            "max_position_risk_pct": _f(cfg["max_position_risk_pct"]),
            "max_portfolio_exposure_pct": _f(cfg["max_portfolio_exposure_pct"]),
            "max_drawdown_pct": _f(cfg["max_drawdown_pct"]),
            "warning_threshold_pct": _f(cfg["warning_threshold_pct"]),
            "updated_at": (pol.updated_at if pol else None), "updated_by": (pol.updated_by if pol else None),
        },
    }


def build_risk_status(store, now: datetime | None = None, exposure: dict | None = None,
                      equity=None) -> dict:
    """The /risk/status response. Sources ONLY persisted/read-model data; missing inputs stay null/NO
    DATA (never zero). `exposure`/`equity` default to None (no live broker/positions in prod → NO DATA)."""
    now = now or datetime.now(timezone.utc)
    cfg, rc, pol, version, token, _complete = combined_config(store)
    today = now.date().isoformat()
    daily_pnl = store.get_daily_pnl(today)
    risk_state = store.get_risk_state()
    kill_switch = store.get_kill_switch()
    res = evaluate_risk_state(config=cfg, daily_pnl=daily_pnl, risk_state=risk_state,
                              kill_switch=kill_switch, exposure=exposure, equity=equity)
    return {
        "status": res["status"], "reasons": res["reasons"], "missing": res["missing"],
        "capital": {"value": _f(res["capital"]), "currency": res["currency"],
                    "source": ("risk_config" if res["capital"] is not None else None)},
        "daily_pnl": {"value": _f(res["daily_pnl"]), "limit": _f(res["daily_limit"]),
                      "used_pct": _f(res["daily_used_pct"]), "remaining": _f(res["daily_remaining"]),
                      "observed_at": res["daily_observed_at"]},
        "position_risk": {"value": _f(res["position_risk"]), "limit": _f(res["position_risk_limit"])},
        "exposure": {"gross_pct": _f(res["gross_pct"]), "net_pct": _f(res["net_pct"]),
                     "limit_pct": _f(res["exposure_limit_pct"])},
        "drawdown": {"value_pct": _f(res["drawdown_pct"]), "limit_pct": _f(res["drawdown_limit_pct"])},
        "kill_switch": res["kill_switch"], "configuration_version": version,
        "version_token": token, "updated_at": (pol.updated_at if pol else None), "ts": now.isoformat(),
    }


# Existing kill-switch audit actions (already immutable in audit_events) → risk-event view. NOT mirrored.
_KILL_AUDIT_MAP = {"KILL": ("KILL_SWITCH_TRIGGERED", "CRITICAL"), "RESET": ("KILL_SWITCH_ARMED", "INFO")}


def build_risk_events(store, limit: int = 50) -> dict:
    """Merged, read-only risk-event timeline: the immutable risk_events (CONFIGURATION_UPDATED …) PLUS
    the EXISTING authoritative kill-switch audit_events (KILL/RESET) — surfaced, never duplicated."""
    n = max(1, min(500, int(limit)))
    items: list[dict] = []
    for e in store.list_risk_events(n):
        items.append({"id": e.id, "timestamp": e.created_at or e.timestamp, "event_type": e.event_type,
                      "severity": e.severity, "description": e.description, "reason_code": e.reason_code,
                      "observed_value": e.observed_value, "configured_limit": e.configured_limit,
                      "configuration_version": e.configuration_version, "details_json": e.details_json,
                      "source": "risk_events"})
    for a in store.recent_audit(200):
        if a.action in _KILL_AUDIT_MAP:
            etype, sev = _KILL_AUDIT_MAP[a.action]
            items.append({"id": a.event_id, "timestamp": a.ts, "event_type": etype, "severity": sev,
                          "description": a.reason or f"Kill switch {a.action.lower()} by {a.actor}",
                          "reason_code": etype, "observed_value": None, "configured_limit": None,
                          "configuration_version": None, "details_json": None, "source": "kill_switch_audit"})
    items.sort(key=lambda x: (x["timestamp"] or ""), reverse=True)
    return {"count": len(items[:n]), "events": items[:n]}
