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

from ..aigov.engine import build_governance_feed, evaluate_governance
from ..completeness.engine import compute_completeness
from ..consensus.engine import build_ai_consensus
from ..dashboard.readmodel import build_dashboard_read_model
from ..evaluation.metrics import build_ai_history, compute_outcomes_summary, compute_performance
from ..fundamentals.readmodel import build_fundamentals
from ..macrodata.readmodel import build_macro, build_macro_context
from ..news.analysis import sentiment_label
from ..optflow.diagnostics import audit_options_provider
from ..optflow.provider import resolve_provider as resolve_options_provider
from ..optflow.readmodel import build_options
from ..traders.diagnostics import audit_trader_providers
from ..persistence.state import RedisStateStore
from ..traders.readmodel import build_symbol_consensus, build_trader_profile
from ..runtime.lifecycle import LifecycleManager, RuntimeStatus
from ..store import open_store
from .base import build_dsn, redis_url
from .recovery import age_seconds, build_recovery_checks

SERVICE = "control"
HEARTBEAT_INTERVAL = 5.0
HEALTH_STALE_S = float(os.environ.get("ATP_HEALTH_STALE_S", "20"))
BROKER_STALE_S = float(os.environ.get("ATP_BROKER_STALE_S", "20"))   # broker heartbeat expiry -> STALE


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


@app.get("/market/{symbol}/ohlc")
def market_ohlc(symbol: str, interval: str = "1m", limit: int = 500) -> dict:
    """Read-only OHLC bars (§ Phase G1) for the Market Intelligence Terminal — the durable, Massive-
    aggregated candles from PostgreSQL. Bar shape matches the frontend OhlcBar. Carries NO secrets.
    Example: /market/NVDA/ohlc?interval=1m&limit=500 . No bars -> empty list (NO DATA), never fabricated."""
    iv = interval if interval in ("1m", "5m", "15m", "1h", "1D") else "1m"
    try:
        n = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        n = 500
    with ctx.lock:
        rows = ctx.store.list_ohlc_bars(symbol.upper(), iv, n)
    bars = [{"timestamp": r.ts, "open": float(r.open), "high": float(r.high), "low": float(r.low),
             "close": float(r.close), "volume": float(r.volume)} for r in rows]
    return {"symbol": symbol.upper(), "interval": iv, "count": len(bars), "bars": bars,
            "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/market/{symbol}/news")
def market_news(symbol: str, limit: int = 30) -> dict:
    """Read-only market news (§ Phase G2.1) for a symbol — real headlines collected by the
    news-intelligence service into PostgreSQL, each with a deterministic sentiment score/label and
    impact level. Carries NO secrets or provider keys. No items -> empty list (NO DATA), never
    fabricated. Public read-model like /market and /market/{symbol}/ohlc."""
    try:
        n = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        n = 30
    with ctx.lock:
        rows = ctx.store.list_news(symbol.upper(), n)
    items = [{
        "id": r.id, "symbol": r.symbol, "title": r.title, "source": r.source, "url": r.url,
        "published_at": r.published_at, "summary": r.content_summary,
        "sentiment_score": r.sentiment_score, "sentiment": sentiment_label(r.sentiment_score),
        "impact": r.impact_level,
    } for r in rows]
    return {"symbol": symbol.upper(), "count": len(items), "items": items,
            "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/ai/outcomes")
def ai_outcomes() -> dict:
    """Read-only Outcome Lifecycle status (§ Phase G3.2): prediction / evaluated / pending counts, overall
    directional accuracy, and the confusion matrix (TRUE/FALSE POSITIVE/NEGATIVE) at the 5-day horizon —
    from the immutable history. Pending = not yet measured against real OHLC (never fabricated). No
    secrets, no execution."""
    with ctx.lock:
        return compute_outcomes_summary(ctx.store)


@app.get("/ai/performance")
def ai_performance(horizon: int = 5) -> dict:
    """Read-only AI performance (§ Phase G3.1): directional accuracy, average forward return, confidence
    calibration, score reliability, error classification + sample size — computed from the immutable
    prediction/outcome history. History is never rewritten. 0 evaluated outcomes -> NO DATA (never
    fabricated). No secrets, no execution."""
    h = horizon if horizon in (1, 3, 5, 20) else 5
    with ctx.lock:
        return compute_performance(ctx.store, h)


@app.get("/market/{symbol}/ai-history")
def market_ai_history(symbol: str, limit: int = 50) -> dict:
    """Read-only AI prediction history (§ Phase G3.1) for a symbol — past AI views with their measured
    outcomes (1/3/5/20-day forward returns). Immutable; missing outcomes -> NO DATA. No secrets."""
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with ctx.lock:
        return build_ai_history(ctx.store, symbol.upper(), n)


@app.get("/market/{symbol}/ai-consensus")
def market_ai_consensus(symbol: str) -> dict:
    """Read-only AI consensus (§ Phase G3): the transparent AI market view — conviction score, direction,
    confidence, per-source components, strengths, risks and surfaced CONFLICTS — computed FRESH from the
    other intelligence layers. This is an INTELLIGENCE SIGNAL, never a trading decision, order, or broker
    action. Missing inputs -> NO DATA / PARTIAL, never fabricated. No secrets. Public read-model."""
    with ctx.lock:
        return build_ai_consensus(ctx.store, symbol.upper())


@app.get("/market/{symbol}/ai-governance")
def market_ai_governance(symbol: str) -> dict:
    """Read-only AI Decision Governance verdict (§ Phase G3.3) for the CURRENT view: APPROVED / PARTIAL /
    CONFLICT / BLOCKED with the score, confidence, data completeness and reason codes it was based on.
    This EVALUATES decision quality/readiness only — it is NOT a trade, order, or broker/IBKR action.
    Missing inputs -> BLOCKED (INSUFFICIENT_DATA), never fabricated. No secrets. Public read-model."""
    with ctx.lock:
        return evaluate_governance(build_ai_consensus(ctx.store, symbol.upper()))


@app.get("/ai/governance")
def ai_governance(limit: int = 50) -> dict:
    """Read-only recent governance decisions (§ Phase G3.3) — the immutable verdict per prediction joined
    to its direction and 5-day outcome (Prediction → Governance → Outcome). Governance history is never
    rewritten. No verdicts yet -> empty (never fabricated). No secrets, no execution."""
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with ctx.lock:
        return build_governance_feed(ctx.store, n)


@app.get("/market/{symbol}/data-completeness")
def market_data_completeness(symbol: str) -> dict:
    """Read-only Data Completeness (§ Phase C1): how complete GIGBAY's information is for a symbol across
    the 7 intelligence domains — a deterministic 0-100 score, a readiness state (READY / PARTIAL /
    INSUFFICIENT) and which sources are available vs missing. This MEASURES information quality only — it
    is NOT a trade, order, or broker action. A missing source scores 0 / NO DATA (never fabricated, and
    the score never rises to cover a gap). No secrets. Public read-model."""
    with ctx.lock:
        return compute_completeness(ctx.store, symbol.upper())


@app.get("/market/{symbol}/options")
def market_options(symbol: str) -> dict:
    """Read-only options intelligence (§ Phase G2.3): a deterministic options score, put/call ratio,
    implied volatility, volume, open interest, unusual-activity flag, sentiment + signals/risks —
    assembled from persisted flow. INTELLIGENCE SIGNAL only, never a trade signal. Missing data ->
    null/empty (NO DATA), never fabricated. No secrets. Public read-model like /market/{symbol}/news."""
    with ctx.lock:
        return build_options(ctx.store, symbol.upper())


@app.get("/market/{symbol}/options-diagnostics")
def market_options_diagnostics(symbol: str) -> dict:
    """Read-only options-provider entitlement probe (§ Phase R1.1): whether the licensed options data is
    actually AVAILABLE for this symbol — 200 entitled / 401 bad key / 403 NOT_AUTHORIZED — reported
    honestly instead of being swallowed to NO DATA. Never exposes the API key or the raw payload (only
    the HTTP status, Polygon's status word, and the contract count). Diagnostic only: no trade, order,
    broker, IBKR, or execution. No store access (no lock needed)."""
    provider = resolve_options_provider()
    return {"symbol": symbol.upper(), "provider": provider.name, **provider.probe(symbol.upper())}


@app.get("/options/audit")
def options_audit() -> dict:
    """Read-only audit (§ Phase R1.1): is a licensed options data provider AVAILABLE? Probes the
    configured provider for NVDA / AAPL / SPY and returns an AVAILABLE / NOT AVAILABLE verdict with
    recommended providers when unavailable. Exposes no secrets; no trading/broker/IBKR/execution."""
    return audit_options_provider()


@app.get("/traders/audit")
def traders_audit() -> dict:
    """Read-only audit (§ Phase R1.2): which trader-intelligence sources are available (SEC 13F / fund
    holdings / insider / Darwinex / Collective2 / eToro / TradingView), which one is selected (SEC 13F),
    and whether it is active. Data only — no copy-trading, no broker, no IBKR, no execution. No secrets."""
    return audit_trader_providers()


@app.get("/macro/current")
def macro_current() -> dict:
    """Read-only Macro Intelligence (§ Phase R1.2): the current global macro environment — a
    deterministic 0-100 score, the risk regime (RISK_ON / RISK_NEUTRAL / RISK_OFF), signals, risks and
    the raw metrics (rates, inflation, employment, VIX, USD, commodities) with their trend. INTELLIGENCE
    INPUT only — never a trade, order, or broker action. No snapshot -> NO DATA, never fabricated."""
    with ctx.lock:
        return build_macro(ctx.store)


@app.get("/market/{symbol}/macro-context")
def market_macro_context(symbol: str) -> dict:
    """Read-only macro relevance for a symbol (§ Phase R1.2): what the current global regime means for
    this (risk-asset) symbol — TAILWIND / NEUTRAL / HEADWIND — plus the regime, score, signals and risks.
    INTELLIGENCE INPUT only. No snapshot -> NO DATA, never fabricated. No secrets."""
    with ctx.lock:
        return build_macro_context(ctx.store, symbol.upper())


@app.get("/market/{symbol}/fundamentals")
def market_fundamentals(symbol: str) -> dict:
    """Read-only fundamentals intelligence (§ Phase G2.2): company profile, financials, valuation,
    analyst estimates + a deterministic company quality score and strengths/risks — assembled from
    persisted data. INTELLIGENCE SIGNAL only, never a buy/sell decision. Missing data -> null/empty
    (NO DATA), never fabricated. No secrets. Public read-model like /market and /market/{symbol}/news."""
    with ctx.lock:
        return build_fundamentals(ctx.store, symbol.upper())


@app.get("/market/{symbol}/traders")
def market_traders(symbol: str) -> dict:
    """Read-only trader-intelligence consensus (§ Phase G2.5) for a symbol — quality-weighted LONG/
    SHORT/NEUTRAL shares + ranked contributors, computed from persisted trader data. This is an
    INTELLIGENCE SIGNAL, never a trading decision or copy-trade. No positions -> null/empty (NO DATA),
    never fabricated. Carries NO credentials. Public read-model like /market and /market/{symbol}/news."""
    with ctx.lock:
        return build_symbol_consensus(ctx.store, symbol.upper())


@app.get("/traders/{trader_id}")
def trader_profile(trader_id: str) -> dict:
    """Read-only single-trader profile (§ Phase G2.5): performance, risk, strategy + a deterministic
    quality score. 404 when the trader is unknown. No secrets, no execution."""
    with ctx.lock:
        prof = build_trader_profile(ctx.store, trader_id)
    if prof is None:
        raise HTTPException(404, "trader not found")
    return prof


@app.get("/broker")
def broker() -> dict:
    """Read-only broker read-model (Phase F1) merged from the Broker Connector's Redis snapshot and its
    PostgreSQL heartbeat. The heartbeat is the authoritative LIVENESS signal (refreshed every ~5s on
    every broker code path); the Redis snapshot only carries the last observed values. If the heartbeat
    expires the connection is reported STALE — never a frozen CONNECTED — so a dead or hung broker can
    never display as connected (fail-closed). Carries NO credentials, account secrets, or tokens.

    connection ∈ {CONNECTED, STALE, DISCONNECTED, UNKNOWN}:
      UNKNOWN      broker never reported (no heartbeat and no snapshot)
      STALE        heartbeat expired -> broker dead/hung; its last snapshot is not trusted
      CONNECTED    broker live AND snapshot reports connected to the Gateway
      DISCONNECTED broker live but not connected to the Gateway
    """
    now = datetime.now(timezone.utc)
    snap: dict = {}
    if ctx.snap is not None:
        try:
            snap = ctx.snap.get("broker:snapshot") or {}
        except Exception:
            snap = {}
    for k in ("password", "token", "session", "username"):     # belt-and-suspenders: never leak secrets
        snap.pop(k, None)
    hb_age = None                                              # seconds since broker's last PG heartbeat
    try:
        with ctx.lock:
            hbs = ctx.store.list_heartbeats()
        for (s, _st, _d, u) in hbs:
            if s == "broker":
                hb_age = age_seconds(u, now)
                break
    except Exception:
        hb_age = None
    raw = snap.get("connection")
    if hb_age is None and not snap:
        state = "UNKNOWN"
    elif hb_age is None:                                       # snapshot but no heartbeat row -> use snap age
        snap_age = age_seconds(snap.get("ts"), now) if snap.get("ts") else float("inf")
        state = "STALE" if snap_age > BROKER_STALE_S else (raw if raw in ("CONNECTED", "DISCONNECTED") else "UNKNOWN")
    elif hb_age > BROKER_STALE_S:                              # heartbeat expired -> broker dead/hung
        state = "STALE"
    else:
        state = raw if raw in ("CONNECTED", "DISCONNECTED") else "UNKNOWN"
    out = dict(snap) if snap else {"broker": "IBKR"}
    out["connection"] = state
    out["connection_raw"] = raw
    out["heartbeat_age"] = round(hb_age, 1) if hb_age is not None else None
    out["stale_threshold_s"] = BROKER_STALE_S
    if state in ("STALE", "UNKNOWN"):                          # never trust values from a non-live broker
        out["reconciliation"] = "UNAVAILABLE"
    if not snap and hb_age is None:
        out["note"] = "no broker snapshot yet"
    return out


# ---------------------------------------------------------------- dashboard read-model (authenticated)
@app.get("/dashboard")
def dashboard(authorization: str | None = Header(default=None)) -> dict:
    """Read-only Dashboard read-model (§ Phase G1.8): account / positions / risk / system / AI,
    assembled from AUTHORITATIVE PostgreSQL state (+ the broker read-model for live equity/cash). It
    reads only — it never touches execution, broker orders, the risk-engine logic, IBKR, or the
    autonomous flags. Missing state renders as null/empty (NO DATA), never fabricated. Carries NO
    credentials or secrets. Authenticated with the control token (richer financial view than the
    public observability endpoints)."""
    _auth(authorization)
    bk = broker()                                  # exact broker read-model (fail-closed liveness, no secrets)
    now = datetime.now(timezone.utc)
    with ctx.lock:
        return build_dashboard_read_model(ctx.store, bk, now=now)


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
