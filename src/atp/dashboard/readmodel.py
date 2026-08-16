"""Dashboard read-model assembly (§ Phase G1.8 — READ-ONLY).

Assembles the account / positions / risk / system / AI view the frontend needs, from AUTHORITATIVE
PostgreSQL state (via the Store) plus the broker read-model dict (for live equity/cash). This is a
PURE, read-only function: it performs SELECTs only and NEVER touches execution, broker orders, the
risk-engine logic, IBKR, or the autonomous flags. Every field is None/empty (NO DATA) when the
underlying state is absent — nothing is fabricated (§33).

Only these keys of the broker read-model are consumed (equity/cash/currency/connection); the broker
dict is already stripped of secrets upstream, and this module copies no other field out of it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _f(v) -> float | None:
    """Decimal/number → float for JSON, or None. Never fabricates a value."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _decode_payload(payload) -> dict:
    """Decode a decision's JSON payload into a dict (action/confidence/entry/… as recorded), or {}."""
    if not payload:
        return {}
    try:
        v = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def build_dashboard_read_model(store, broker: dict | None = None, *, now: datetime | None = None) -> dict:
    """Compose the read-only dashboard snapshot from persisted state + the broker read-model.

    account   equity/cash from a LIVE broker only (fail-closed → None when not CONNECTED); pnl from
              today's persisted daily_pnl.
    positions symbol/quantity/avg_price/realized-pnl from the durable positions table (no market
              price is persisted, so unrealized P&L stays NO DATA).
    risk      capital + the 3 risk parameters, day-start/peak equity, daily P&L, daily-loss fraction
              and drawdown (drawdown needs a live equity → None otherwise).
    system    recovery/runtime state + reason.
    ai        recorded decisions (newest first) — empty when none exist.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    bk = broker or {}

    connected = bk.get("connection") == "CONNECTED"
    equity = _f(bk.get("equity")) if connected else None   # trust equity/cash only from a LIVE broker
    cash = _f(bk.get("cash")) if connected else None

    positions = store.list_positions()
    rc = store.get_risk_config()
    rstate = store.get_risk_state()
    dpnl = store.get_daily_pnl(today)
    rs = store.get_runtime_state()
    decisions = store.list_decisions(50)

    pos_out = [{
        "symbol": p.instrument, "quantity": _f(p.quantity), "avg_price": _f(p.avg_price),
        "pnl": _f(p.realized_pnl), "updated_at": p.updated_at,
    } for p in positions]

    day_start = _f(rstate.day_start_equity) if rstate else (_f(dpnl.day_start_equity) if dpnl else None)
    peak = _f(rstate.peak_equity) if rstate else None
    daily_pnl = (_f(dpnl.realized_pnl) + _f(dpnl.unrealized_pnl)) if dpnl else None

    drawdown = None
    if peak and peak > 0 and equity is not None:
        drawdown = max(0.0, (peak - equity) / peak)

    daily_loss_pct = None
    if daily_pnl is not None and day_start and day_start > 0:
        daily_loss_pct = max(0.0, -daily_pnl / day_start)

    risk_out = None
    if rc is not None or rstate is not None or dpnl is not None:
        risk_out = {
            "capital": _f(rc.capital) if rc else None,
            "risk_per_trade_pct": _f(rc.risk_per_trade_pct) if rc else None,
            "max_daily_loss_pct": _f(rc.max_daily_loss_pct) if rc else None,
            "day_start_equity": day_start,
            "peak_equity": peak,
            "daily_pnl": daily_pnl,
            "daily_loss_pct": daily_loss_pct,
            "drawdown": drawdown,
            "halted": bool(rstate.halted) if rstate else None,
            "killed": bool(rstate.killed) if rstate else None,
        }

    decisions_out = []
    for d in decisions:
        item = _decode_payload(d.payload)
        item.update({
            "decision_id": d.decision_id, "ts": d.ts, "instrument": d.instrument,
            "final_decision": d.final_decision,
        })
        decisions_out.append(item)

    return {
        "account": {"equity": equity, "cash": cash, "pnl": daily_pnl,
                    "currency": bk.get("currency"), "connected": connected},
        "positions": pos_out,
        "risk": risk_out,
        "system": {"recovery_state": rs.status if rs else None,
                   "recovery_reason": rs.reason if rs else None},
        "ai": {"decisions": decisions_out},
        "ts": now.isoformat(),
    }
