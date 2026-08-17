"""§ R3.0 — read-models: store rows → JSON-safe API dicts (RESEARCH ONLY, no secrets)."""
from __future__ import annotations

import json


def _f(v):
    return None if v is None else float(v)


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def run_summary(r) -> dict:
    return {
        "run_id": r.run_id, "owner": r.owner, "strategy_id": r.strategy_id,
        "strategy_version": r.strategy_version, "engine_version": r.engine_version, "interval": r.interval,
        "symbols": _loads(r.symbol_universe_json, []), "start": r.start_ts, "end": r.end_ts,
        "asset_class": r.asset_class, "status": r.status, "failure_code": r.failure_code,
        "failure_reason": r.failure_reason, "result_checksum": r.result_checksum,
        "created_at": r.created_at, "started_at": r.started_at, "ended_at": r.ended_at,
    }


def run_detail(r) -> dict:
    d = run_summary(r)
    d.update({
        "strategy_config": _loads(r.strategy_config_json, {}), "strategy_checksum": r.strategy_checksum,
        "timestamp_policy_id": r.timestamp_policy_id, "timestamp_policy_version": r.timestamp_policy_version,
        "exchange_calendar_id": r.exchange_calendar_id, "exchange_calendar_version": r.exchange_calendar_version,
        "exchange_tz": r.exchange_tz, "session_calendar": r.session_calendar, "data_source": r.data_source,
        "config_snapshot": _loads(r.config_snapshot_json, {}),
        "risk_config_snapshot": _loads(r.risk_config_snapshot_json, {}),
        "warnings": _loads(r.warnings_json, []), "missing_data": _loads(r.missing_data_json, None),
        "commit_ref": r.commit_ref, "updated_at": r.updated_at,
        "safety": {"research_only": True, "autonomous": "DISABLED", "execution": "DISABLED", "ibkr_orders": 0},
    })
    return d


def metrics_view(m) -> dict:
    if m is None:
        return {"status": "NO DATA", "metrics": None}
    return {"status": "COMPLETE", "computed_at": m.computed_at, "metrics": _loads(m.metrics_json, {})}


def decisions_view(rows) -> list[dict]:
    return [{"id": d.id, "seq": d.seq, "ts": d.ts, "symbol": d.symbol, "action": d.action,
             "confidence": d.confidence, "reason": d.reason, "missing_inputs": _loads(d.missing_inputs_json, []),
             "evidence": _loads(d.evidence_json, {}), "checksum": d.decision_checksum} for d in rows]


def trades_view(rows) -> list[dict]:
    return [{"id": t.id, "symbol": t.symbol, "side": t.side, "entry_ts": t.entry_ts,
             "entry_fill_ts": t.entry_fill_ts, "entry_price": _f(t.entry_price),
             "initial_stop_price": _f(t.initial_stop_price), "exit_ts": t.exit_ts, "exit_price": _f(t.exit_price),
             "quantity": _f(t.quantity), "gross_pnl": _f(t.gross_pnl), "commission": _f(t.commission),
             "slippage": _f(t.slippage), "net_pnl": _f(t.net_pnl),
             "return_pct": (None if t.return_pct is None else float(t.return_pct)), "bars_held": t.bars_held,
             "exit_reason": t.exit_reason, "ambiguous": bool(t.ambiguous),
             "expected_risk_per_share": _f(t.expected_risk_per_share),
             "actual_risk_per_share": _f(t.actual_risk_per_share)} for t in rows]


def equity_view(rows) -> list[dict]:
    return [{"seq": e.seq, "ts": e.ts, "cash": _f(e.cash), "equity": _f(e.equity),
             "realized_pnl": _f(e.realized_pnl), "unrealized_pnl": _f(e.unrealized_pnl),
             "daily_pnl": _f(e.daily_pnl),
             "gross_exposure_pct": (None if e.gross_exposure_pct is None else float(e.gross_exposure_pct)),
             "drawdown_pct": (None if e.drawdown_pct is None else float(e.drawdown_pct))} for e in rows]


def events_view(rows) -> list[dict]:
    return [{"id": e.id, "seq": e.seq, "ts": e.ts, "event_type": e.event_type, "severity": e.severity,
             "symbol": e.symbol, "details": _loads(e.details_json, {})} for e in rows]
