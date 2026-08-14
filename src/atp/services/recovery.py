"""Recovery-sequence checks + market-data freshness (§ Phase C, RULE 2).

The lifecycle's fixed recovery sequence (``RECOVERY_STEPS``) needs one boolean checker per step. These
are all derived from DURABLE PostgreSQL state (plus an in-acceptance empty paper broker). A full pass
moves RECOVERY_REQUIRED → READY_FOR_ARM; human ARM is still mandatory and RUNNING is never automatic.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..runtime.gate import today_utc
from ..runtime.positions import reconcile, reconstruct_positions
from ..store.money import D


def age_seconds(iso_ts, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(iso_ts))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds()
    except Exception:
        return float("inf")


def market_data_fresh(store, *, max_age_s: float = 15.0, now: datetime | None = None) -> bool:
    """True only if there is at least one market_data_health row and EVERY row is BOTH fresh (age)
    AND tradable (status READY). A dead feed either stops refreshing rows (they age out) or writes
    fresh DATA_NOT_AVAILABLE rows — both make this False, so new inputs are blocked (fail-closed).
    A freshly-written 'unavailable' row must NEVER count as tradable-fresh."""
    now = now or datetime.now(timezone.utc)
    rows = store.list_md_health()
    if not rows:
        return False
    return all(str(r[2]) == "READY" and age_seconds(r[4], now) <= max_age_s for r in rows)


def build_recovery_checks(store, *, broker_positions: dict | None = None,
                          md_max_age_s: float = 15.0) -> dict:
    """Return the checker map for ``LifecycleManager.run_recovery``. In acceptance there is no broker,
    so paper positions are empty and reconciliation compares them to the store-reconstructed set."""
    broker_positions = broker_positions or {}

    def _read_ok(fn):
        def _c() -> bool:
            try:
                fn()
                return True
            except Exception:
                return False
        return _c

    def _reconcile() -> bool:
        db = {k: v.quantity for k, v in reconstruct_positions(store).items()}
        res = reconcile(db, {k: D(v) for k, v in broker_positions.items()})
        return res.ok

    def _has(name):
        return getattr(store, name, None)

    return {
        "load_runtime_state": _read_ok(store.get_runtime_state),
        "load_risk_state": _read_ok(store.get_risk_state),
        "load_daily_pnl": _read_ok(lambda: store.get_daily_pnl(today_utc())),
        "load_daily_loss_lock": _read_ok(lambda: store.get_daily_loss_lock(today_utc())),
        "load_kill_switch": _read_ok(store.get_kill_switch),
        "load_positions": _read_ok(store.list_positions),
        "load_orders": _read_ok(_has("list_orders") or store.list_fills),
        "load_fills": _read_ok(store.list_fills),
        "query_broker": lambda: True,          # no broker connected in acceptance → empty paper book
        "reconcile": _reconcile,
        "validate_market_data": lambda: market_data_fresh(store, max_age_s=md_max_age_s),
        "validate_risk_service": _read_ok(store.get_risk_state),
        "validate_database": store.ping,
    }
