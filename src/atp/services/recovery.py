"""Recovery-sequence checks + market-data freshness (§ Phase C, RULE 2).

The lifecycle's fixed recovery sequence (``RECOVERY_STEPS``) needs one boolean checker per step. These
are all derived from DURABLE PostgreSQL state plus an explicit broker snapshot. A full pass
moves RECOVERY_REQUIRED → READY_FOR_ARM; human ARM is still mandatory and RUNNING is never automatic.
"""
from __future__ import annotations

from datetime import UTC, datetime

from ..runtime.gate import today_utc
from ..runtime.positions import reconcile, reconstruct_positions
from ..store.money import D


def age_seconds(iso_ts, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    try:
        t = datetime.fromisoformat(str(iso_ts))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return (now - t).total_seconds()
    except Exception:  # noqa: BLE001 - hostile timestamps are stale, never trusted
        return float("inf")


def market_data_fresh(store, *, max_age_s: float = 15.0, now: datetime | None = None) -> bool:
    """True only if there is at least one market_data_health row and EVERY row is BOTH fresh (age)
    AND tradable (status READY). A dead feed either stops refreshing rows (they age out) or writes
    fresh DATA_NOT_AVAILABLE rows — both make this False, so new inputs are blocked (fail-closed).
    A freshly-written 'unavailable' row must NEVER count as tradable-fresh."""
    now = now or datetime.now(UTC)
    rows = store.list_md_health()
    if not rows:
        return False
    return all(str(r[2]) == "READY" and age_seconds(r[4], now) <= max_age_s for r in rows)


def verify_paper_stopped(store, *, run_id: str):
    """Verify a terminal Paper proof without constructing or mutating the owner runtime."""
    from ..runtime.paper_canary import PaperCanaryError, verify_paper_stopped as verify

    try:
        return verify(store, run_id=run_id)
    except PaperCanaryError as exc:
        raise ValueError(str(exc)) from exc


def build_recovery_checks(
    store,
    *,
    broker_positions: dict | None = None,
    paper_canary=None,
    paper_run_id: str | None = None,
    paper_recovery_proof: dict | bool | None = None,
    md_max_age_s: float = 15.0,
) -> dict:
    """Return fail-closed checkers for ``LifecycleManager.run_recovery``.

    A caller-owned Paper Canary may reconcile its own immutable ledger and cancel pending intents.
    Control instead supplies the authenticated Trading Core owner's serialized recovery proof; this
    function validates that proof against current durable rows without constructing or mutating a
    Paper Canary runtime. Without a durable run, callers must supply an explicit broker position
    snapshot. ``None`` means *not queried* and is intentionally distinct from an observed empty map.
    """

    def _read_ok(fn):
        def _c() -> bool:
            try:
                fn()
                return True
            except Exception:  # noqa: BLE001 - each recovery read is a fail-closed boolean check
                return False
        return _c

    def _position_snapshot(raw) -> dict | None:
        if type(raw) is not dict:
            return None
        try:
            snapshot = {}
            for instrument, quantity in raw.items():
                if type(instrument) is not str or not instrument:
                    return None
                exact = D(quantity)
                if not exact.is_finite():
                    return None
                snapshot[instrument] = exact
            return snapshot
        except Exception:  # noqa: BLE001 - malformed broker data is unavailable, never empty
            return None

    external_positions = _position_snapshot(broker_positions)

    durable_cache = {
        "resolved": False,
        "invalid": False,
        "run_id": None,
        "status": None,
        "attempted": False,
        "result": None,
    }

    def _owner_proof_ok(run_id: str, proof: object) -> bool:
        if type(proof) is not dict or proof.get("ok") is not True or proof.get("breaks") != []:
            return False
        proof_run = proof.get("run")
        proof_reconciliation = proof.get("reconciliation")
        if type(proof_run) is not dict or type(proof_reconciliation) is not dict:
            return False
        try:
            from ..runtime.paper_canary import verify_paper_reconciled_ready

            current = verify_paper_reconciled_ready(store, run_id=run_id)
            if current.ok is not True or current.reconciliation is None:
                return False
            run_fields = (
                "run_id", "status", "active_slot", "version", "config_json", "config_checksum",
                "risk_config_checksum", "commit_sha", "reason", "created_at", "started_at",
                "heartbeat_at", "ended_at", "updated_at",
            )
            if any(proof_run.get(field) != getattr(current.run, field) for field in run_fields):
                return False
            reconciliation_fields = (
                "reconciliation_id", "run_id", "status", "fills_checksum",
                "positions_checksum", "account_checksum", "open_order_count", "breaks_json",
                "checked_at",
            )
            return all(
                proof_reconciliation.get(field) == getattr(current.reconciliation, field)
                for field in reconciliation_fields
            )
        except Exception:  # noqa: BLE001 - malformed or unreadable durable proof fails closed
            return False

    def _durable_run_id() -> str | None:
        if durable_cache["resolved"]:
            return durable_cache["run_id"]
        durable_cache["resolved"] = True
        if paper_run_id is not None:
            if type(paper_run_id) is not str or not paper_run_id:
                durable_cache["invalid"] = True
                return None
            run = store.get_paper_run(paper_run_id)
            if run is not None and run.status in {
                "RUNNING", "RECOVERY_REQUIRED", "READY_FOR_ARM",
            }:
                durable_cache["run_id"] = run.run_id
                durable_cache["status"] = run.status
            else:
                durable_cache["invalid"] = True
            return durable_cache["run_id"]
        list_runs = getattr(store, "list_paper_runs", None)
        if not callable(list_runs):
            return None
        try:
            active = []
            for status in ("CREATED", "RUNNING", "RECOVERY_REQUIRED", "READY_FOR_ARM"):
                active.extend(list_runs(status=status, limit=2))
        except Exception:  # noqa: BLE001 - unreadable durable run state must fail closed
            durable_cache["invalid"] = True
            return None
        if len(active) == 1:
            durable_cache["run_id"] = active[0].run_id
            durable_cache["status"] = active[0].status
        elif len(active) > 1:
            durable_cache["invalid"] = True
        return durable_cache["run_id"]

    def _durable_recovery():
        if durable_cache["attempted"]:
            return durable_cache["result"]
        durable_cache["attempted"] = True
        run_id = _durable_run_id()
        if run_id is None:
            return None
        try:
            canary = paper_canary
            if canary is None:
                durable_cache["result"] = paper_recovery_proof
                return durable_cache["result"]

            from ..runtime.paper_canary import DurablePaperCanary

            if (
                type(canary) is not DurablePaperCanary
                or canary._store is not store
            ):
                raise RuntimeError("Paper Canary recovery is not bound to this Store")
            if durable_cache["status"] == "READY_FOR_ARM":
                durable_cache["result"] = canary.prove_reconciled_ready(run_id=run_id)
            else:
                durable_cache["result"] = canary.recover(
                    run_id=run_id,
                    reason="global runtime restart recovery",
                )
        except Exception:  # noqa: BLE001 - recovery checker records only a fail-closed result
            durable_cache["result"] = False
        return durable_cache["result"]

    def _paper_recovery_ok() -> bool:
        run_id = _durable_run_id()
        if run_id is None:
            return durable_cache["invalid"] is False
        result = _durable_recovery()
        if type(result) is dict:
            return _owner_proof_ok(run_id, result)
        return result is not False and result is not None and getattr(result, "ok", False) is True

    def _query_broker() -> bool:
        paper_ok = _paper_recovery_ok()
        return external_positions is not None and paper_ok

    def _legacy_reconcile() -> bool:
        if durable_cache["invalid"] is True or external_positions is None:
            return False
        try:
            db = {k: D(v.quantity) for k, v in reconstruct_positions(store).items()}
            if any(type(key) is not str or not key or not value.is_finite() for key, value in db.items()):
                return False
            return reconcile(db, external_positions).ok
        except Exception:  # noqa: BLE001 - corrupt durable positions fail reconciliation
            return False

    def _reconcile() -> bool:
        paper_ok = _paper_recovery_ok()
        legacy_ok = _legacy_reconcile()
        return legacy_ok and paper_ok

    list_orders = getattr(store, "list_orders", None)

    def _load_orders() -> bool:
        legacy_ok = callable(list_orders) and _read_ok(list_orders)()
        run_id = _durable_run_id()
        if run_id is None:
            return legacy_ok and durable_cache["invalid"] is False
        try:
            store.list_paper_orders(run_id=run_id)
            paper_ok = True
        except Exception:  # noqa: BLE001 - unreadable durable orders fail recovery
            paper_ok = False
        return legacy_ok and paper_ok

    return {
        "load_runtime_state": _read_ok(store.get_runtime_state),
        "load_risk_state": _read_ok(store.get_risk_state),
        "load_daily_pnl": _read_ok(lambda: store.get_daily_pnl(today_utc())),
        "load_daily_loss_lock": _read_ok(lambda: store.get_daily_loss_lock(today_utc())),
        "load_kill_switch": _read_ok(store.get_kill_switch),
        "load_positions": _read_ok(store.list_positions),
        "load_orders": _load_orders,
        "load_fills": _read_ok(store.list_fills),
        "query_broker": _query_broker,
        "reconcile": _reconcile,
        "validate_market_data": lambda: market_data_fresh(store, max_age_s=md_max_age_s),
        "validate_risk_service": _read_ok(store.get_risk_state),
        "validate_database": store.ping,
    }
