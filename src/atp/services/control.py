"""Control / Observability Service (§ Phase C — service C).

A standalone FastAPI process: dashboard read-model + health/heartbeat aggregation + authenticated
control commands (recover / arm / start / kill / reset) that drive the durable LifecycleManager. It
reads and writes ONLY PostgreSQL — it holds no trading runtime. Therefore a Control/API outage has
ZERO impact on the Trading Core: the Trading Core process keeps running and its state is untouched.
Vercel/browser dashboards are downstream of this API and are never in the execution chain.

Control never trades: it can move the human-gated lifecycle (ARM/START require explicit operator
action + confirmation) but it never auto-starts, and after its own restart it calls recover() too.
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..persistence.state import RedisStateStore
from ..runtime.lifecycle import LifecycleManager, RuntimeStatus
from ..store import open_store
from .base import build_dsn, redis_url
from .recovery import age_seconds, build_recovery_checks

SERVICE = "control"
HEARTBEAT_INTERVAL = 5.0
HEALTH_STALE_S = float(os.environ.get("ATP_HEALTH_STALE_S", "20"))


class _Ctx:
    store = None
    life: LifecycleManager | None = None
    snap = None                      # Redis read-model for live quotes (best-effort; never authoritative)
    lock = threading.Lock()          # psycopg connection is not thread-safe across the uvicorn pool
    ready = False
    hb_task = None


ctx = _Ctx()


def _auth(authorization: str | None) -> None:
    tok = os.environ.get("ATP_CONTROL_TOKEN")
    if not tok:
        raise HTTPException(503, "control token not configured (ATP_CONTROL_TOKEN)")
    if authorization != f"Bearer {tok}":
        raise HTTPException(401, "unauthorized")


def _ping() -> bool:
    try:
        with ctx.lock:
            return ctx.store.ping()
    except Exception:
        return False


async def _heartbeat_loop() -> None:
    while True:
        try:
            with ctx.lock:
                ctx.store.upsert_heartbeat(service=SERVICE,
                                           status="UP" if ctx.ready else "DEGRADED", detail="control api")
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ctx.store = open_store(build_dsn(), migrate=False)
    ctx.life = LifecycleManager(ctx.store)
    try:
        ctx.snap = RedisStateStore(redis_url()) if redis_url() else None
    except Exception:
        ctx.snap = None
    with ctx.lock:
        ctx.life.recover()                       # never auto-RUNNING
    ctx.ready = True
    ctx.hb_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        ctx.ready = False
        if ctx.hb_task is not None:
            ctx.hb_task.cancel()
        try:
            ctx.store.close()
        except Exception:
            pass


app = FastAPI(title="ATP Control / Observability", lifespan=lifespan)


class Confirm(BaseModel):
    confirm: str | None = None


# ---------------------------------------------------------------- health / readiness / status
@app.get("/health")
def health() -> dict:
    db = _ping()
    return {"service": SERVICE, "status": "UP" if (ctx.ready and db) else "DEGRADED",
            "ready": ctx.ready and db, "db": db}


@app.get("/ready")
def ready() -> dict:
    if not (ctx.ready and _ping()):
        raise HTTPException(503, "not ready")
    return {"ready": True}


@app.get("/status")
def status() -> dict:
    now = datetime.now(timezone.utc)
    with ctx.lock:
        rs = ctx.store.get_runtime_state()
        kill = ctx.store.get_kill_switch()
        hbs = ctx.store.list_heartbeats()
        md = ctx.store.list_md_health()
        db = ctx.store.ping()
    services = [{"service": s, "status": st, "detail": d, "age_s": round(age_seconds(u, now), 1),
                 "healthy": (st == "UP" and age_seconds(u, now) <= HEALTH_STALE_S)}
                for (s, st, d, u) in hbs]
    market_data = [{"symbol": m[0], "source": m[1], "status": m[2],
                    "age_s": round(age_seconds(m[4], now), 1),
                    "fresh": age_seconds(m[4], now) <= HEALTH_STALE_S} for m in md]
    return {"runtime_state": rs.status if rs else None, "kill_switch": kill.engaged,
            "db": db, "services": services, "market_data": market_data, "ts": now.isoformat()}


@app.get("/market")
def market() -> dict:
    """Read-only market-data read-model: authoritative status/freshness from Postgres merged with the
    live quote snapshot (bid/ask/last/latency) from the Redis cache. No secret, no WS auth payload."""
    now = datetime.now(timezone.utc)
    with ctx.lock:
        md = ctx.store.list_md_health()
    health = {m[0]: {"source": m[1], "status": m[2], "latency_ms": m[3], "updated_at": str(m[4]),
                     "fresh": age_seconds(m[4], now) <= HEALTH_STALE_S} for m in md}
    snap_syms: dict = {}
    feed = None
    if ctx.snap is not None:
        try:
            snap = ctx.snap.get("md:snapshot") or {}
            feed = snap.get("feed")
            snap_syms = snap.get("symbols", {}) or {}
        except Exception:
            snap_syms = {}
    out = []
    for sym in sorted(set(health) | set(snap_syms)):
        h = health.get(sym, {})
        s = snap_syms.get(sym, {})
        out.append({
            "symbol": sym,
            "source": s.get("source") or h.get("source"),
            "status": h.get("status") or s.get("status"),
            "realtime": s.get("realtime"),
            "bid": s.get("bid"), "ask": s.get("ask"), "last": s.get("last"),
            "bid_size": s.get("bid_size"), "ask_size": s.get("ask_size"), "volume": s.get("volume"),
            "latency_ms": s.get("latency_ms") if s.get("latency_ms") is not None else h.get("latency_ms"),
            "last_update": h.get("updated_at") or s.get("updated_at"),
            "fresh": h.get("fresh"), "error": s.get("error"),
        })
    return {"feed": feed, "market_data": out, "ts": now.isoformat()}


@app.get("/broker")
def broker() -> dict:
    """Read-only broker read-model (Phase F1) from the Broker Connector's Redis snapshot. Reports
    broker/mode/connection/reconciliation/heartbeat/equity/cash/buying-power/counts. Carries NO
    credentials, account secrets, or session tokens."""
    snap: dict = {}
    if ctx.snap is not None:
        try:
            snap = ctx.snap.get("broker:snapshot") or {}
        except Exception:
            snap = {}
    for k in ("password", "token", "session", "username"):     # belt-and-suspenders: never leak secrets
        snap.pop(k, None)
    if not snap:
        return {"broker": "IBKR", "connection": "UNKNOWN", "note": "no broker snapshot yet"}
    return snap


# ---------------------------------------------------------------- control commands (authenticated)
@app.post("/control/recover")
def ctl_recover(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    with ctx.lock:
        ctx.life.recover()
        if ctx.life.status is not RuntimeStatus.RECOVERY_REQUIRED:
            return {"ran": False, "status": ctx.life.status.value,
                    "note": "not in RECOVERY_REQUIRED — no sequence run"}
        ok, results = ctx.life.run_recovery(build_recovery_checks(ctx.store))
    return {"ran": True, "ok": ok, "status": ctx.life.status.value, "results": results}


@app.post("/control/arm")
def ctl_arm(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.lock:
            return {"status": ctx.life.arm(actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


@app.post("/control/start")
def ctl_start(body: Confirm, authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.lock:
            return {"status": ctx.life.start(confirm=body.confirm, actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


@app.post("/control/kill")
def ctl_kill(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    with ctx.lock:
        return {"status": ctx.life.kill(actor="operator", reason="control kill").value}


@app.post("/control/reset")
def ctl_reset(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.lock:
            return {"status": ctx.life.reset_kill(actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


def main() -> None:
    import uvicorn
    port = int(os.environ.get("ATP_CONTROL_PORT", "9103"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
