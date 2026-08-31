"""Control / Observability Service (§ Phase C — service C).

A standalone FastAPI process: dashboard read-model + health/heartbeat aggregation + authenticated
control commands (prepare / recover / arm / start / disable / kill / reset) that drive the durable
LifecycleManager. It holds no trading runtime. Paper Canary status is read directly from PostgreSQL and
its fixed mutation
commands are proxied only to Trading Core's private loopback owner. A Control/API outage has zero
impact on the Trading Core's already durable state.
Vercel/browser dashboards are downstream of this API and are never in the execution chain.

Control never trades: it can move the human-gated lifecycle (ARM/START require explicit operator
action + confirmation) but it never auto-starts, and after its own restart it calls recover() too.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import http.client
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from ..aigov.engine import build_governance_feed, evaluate_governance
from ..completeness.engine import compute_completeness
from ..consensus.engine import build_ai_consensus
from ..dashboard.readmodel import build_dashboard_read_model
from ..evaluation.metrics import build_ai_history, compute_outcomes_summary, compute_performance
from ..fundamentals.readmodel import build_fundamentals
from ..institutional.clusters import build_insider_cluster
from ..institutional.readmodel import build_institutional_flow
from ..macrodata.readmodel import build_macro, build_macro_context
from ..news.analysis import sentiment_label
from ..optflow.diagnostics import audit_options_provider
from ..optflow.provider import resolve_provider as resolve_options_provider
from ..optflow.readmodel import build_options
from ..persistence.state import RedisStateStore
from ..research import backfill as bf
from ..research import readmodel as bt_read
from ..research.intel.commit import CommitVerificationError, resolve_commit_sha
from ..research.intel.legacy_diag import reconcile_legacy
from ..research.runner import OneActiveRunError, run_backtest
from ..research.runner import ValidationError as BtValidationError
from ..research.validation import readmodel as val_read
from ..riskcontrol import build_risk_config_view, build_risk_events, build_risk_status, validate_config
from ..runtime.lifecycle import LifecycleManager, RuntimeStatus
from ..store import PaperCanaryError, open_store
from ..traders.diagnostics import audit_trader_providers
from ..traders.readmodel import build_symbol_consensus, build_trader_profile
from .base import (
    PAPER_CANARY_COMMAND_BODY_LIMIT,
    PAPER_CANARY_INTERNAL_TOKEN_HEADER,
    build_dsn,
    redis_url,
)
from .recovery import age_seconds, build_recovery_checks, verify_paper_stopped

SERVICE = "control"
HEARTBEAT_INTERVAL = 5.0
HEALTH_STALE_S = float(os.environ.get("ATP_HEALTH_STALE_S", "20"))
BROKER_STALE_S = float(os.environ.get("ATP_BROKER_STALE_S", "20"))   # broker heartbeat expiry -> STALE


class _Ctx:
    store = None
    life: LifecycleManager | None = None
    snap = None                      # Redis read-model for live quotes (best-effort; never authoritative)
    lock = threading.Lock()          # psycopg connection is not thread-safe across the uvicorn pool
    paper_lock = threading.RLock()   # serialize Control's full Paper operator command surface
    ready = False
    hb_task = None
    backfill_provider = None         # test-injected MinuteAggregatesProvider; real provider is env-gated


ctx = _Ctx()


def _auth(authorization: str | None) -> None:
    tok = os.environ.get("ATP_CONTROL_TOKEN")
    if not tok:
        raise HTTPException(503, "control token not configured (ATP_CONTROL_TOKEN)")
    if type(authorization) is not str or not authorization.startswith("Bearer "):
        raise HTTPException(401, "unauthorized")
    supplied = authorization[len("Bearer "):]
    if not hmac.compare_digest(tok, supplied):
        raise HTTPException(401, "unauthorized")


_PAPER_OWNER_PATHS = {
    "create": "/internal/paper-canary/create",
    "activate": "/internal/paper-canary/activate",
    "submit": "/internal/paper-canary/submit",
    "recover": "/internal/paper-canary/recover",
    "stop": "/internal/paper-canary/stop",
}
_PAPER_OFFENSIVE = frozenset({"create", "activate", "submit"})
_PAPER_RECOVERY_STATUSES = ("CREATED", "RUNNING", "RECOVERY_REQUIRED", "READY_FOR_ARM")


def _paper_offensive_enabled() -> bool:
    return (
        os.environ.get("ATP_DURABLE_PAPER_CANARY_ENABLED") == "true"
        and os.environ.get("BROKER_EXECUTION_ENABLED") == "false"
    )


def _paper_owner_request(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Proxy one fixed command to the loopback owner. Never accepts a caller-supplied URL/path."""
    path = _PAPER_OWNER_PATHS.get(command)
    if path is None:
        raise HTTPException(404, "unknown Paper Canary command")
    if command in _PAPER_OFFENSIVE and not _paper_offensive_enabled():
        raise HTTPException(404, "durable Paper Canary is disabled")
    token = os.environ.get("ATP_PAPER_CANARY_INTERNAL_TOKEN")
    if not token:
        raise HTTPException(503, "Paper Canary owner token is not configured")
    try:
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Paper Canary request is not canonical JSON") from exc
    if len(body) > PAPER_CANARY_COMMAND_BODY_LIMIT:
        raise HTTPException(413, "Paper Canary request body too large")
    try:
        port = int(os.environ.get("ATP_PAPER_CANARY_OWNER_PORT", "9112"))
        if not 1 <= port <= 65_535:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "Paper Canary owner port is invalid") from exc
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                PAPER_CANARY_INTERNAL_TOKEN_HEADER: token,
            },
        )
        response = connection.getresponse()
        raw = response.read(65_537)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise HTTPException(503, "Paper Canary owner is unavailable") from exc
    finally:
        connection.close()
    if len(raw) > 65_536:
        raise HTTPException(502, "Paper Canary owner response is too large")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "Paper Canary owner returned malformed JSON") from exc
    if type(decoded) is not dict:
        raise HTTPException(502, "Paper Canary owner returned an invalid response")
    if not 200 <= response.status < 300:
        detail = decoded.get("detail")
        raise HTTPException(
            response.status if 400 <= response.status < 600 else 502,
            detail if type(detail) is str else "Paper Canary owner rejected command",
        )
    if decoded.get("ok") is not True or type(decoded.get("result")) is not dict:
        raise HTTPException(502, "Paper Canary owner returned an invalid success response")
    return decoded["result"]


def _paper_jsonable(value: object) -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) is datetime:
        return value.astimezone(timezone.utc).isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _paper_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if type(value) in {tuple, list}:
        return [_paper_jsonable(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _paper_jsonable(item) for key, item in value.items()}
    raise HTTPException(500, "Paper Canary database row is malformed")


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


class _PaperBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PaperCanaryCreateBody(_PaperBody):
    run_id: str
    reason: str | None = None


class PaperCanaryActivateBody(_PaperBody):
    run_id: str
    confirm: str
    reason: str | None = None


class PaperCanarySubmitBody(_PaperBody):
    """Deliberately minimal: every instrument, quote, token and execution term is owner-bound."""

    run_id: str
    decision_id: str
    side: Literal["BUY", "SELL"]
    quantity: str


class PaperCanaryRecoveryBody(_PaperBody):
    run_id: str
    reason: str | None = None


class PaperCanaryPrepareBody(_PaperBody):
    expected_commit_sha: str
    expected_config_checksum: str
    expected_risk_version_token: str
    reason: str | None = None


class PaperCanaryDisableBody(_PaperBody):
    run_id: str | None = None
    reason: str | None = None


class RiskConfigUpdate(BaseModel):
    """§ R2.0 Risk Control config update body. `expected_version` is the version_token from GET
    /risk/config (optimistic concurrency). No secrets, no trading fields."""
    expected_version: str | None = None
    capital: float | str | None = None
    currency: str | None = None
    max_daily_loss_pct: float | str | None = None
    max_position_risk_pct: float | str | None = None
    max_portfolio_exposure_pct: float | str | None = None
    max_drawdown_pct: float | str | None = None
    warning_threshold_pct: float | str | None = None
    max_daily_loss_amount: float | str | None = None   # optional; validated for consistency, never stored


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
    Missing inputs -> BLOCKED (INSUFFICIENT_DATA), never fabricated. No secrets. Public read-model.
    § R2.0: the LIVE Risk Control state is a mandatory input — Risk BLOCKED forces BLOCKED, Risk NO DATA
    prevents APPROVED (never a false BLOCKED). This applies to the current verdict only."""
    with ctx.lock:
        consensus = build_ai_consensus(ctx.store, symbol.upper())
        risk_status = build_risk_status(ctx.store).get("status")
        return evaluate_governance(consensus, risk_status=risk_status)


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


@app.get("/risk/status")
def risk_status() -> dict:
    """Read-only Risk Control state (§ Phase R2.0): READY / WARNING / BLOCKED / NO DATA + reason codes,
    capital, daily-loss usage, position/exposure/drawdown vs limits, the authoritative kill switch and
    configuration version. OBSERVABILITY + governance gate only — never a trade/order/broker action, and
    it never enables execution. Missing data → null / NO DATA (never zero, never READY). Side-effect free."""
    with ctx.lock:
        return build_risk_status(ctx.store)


@app.get("/risk/config")
def risk_config() -> dict:
    """Read-only current Risk Control configuration (§ R2.0) — the canonical risk_config values +
    risk_control_policy companion + the DERIVED max_daily_loss_amount, with the concurrency version
    token. NO secrets. NO DATA until a complete validated configuration exists. Side-effect free."""
    with ctx.lock:
        return build_risk_config_view(ctx.store)


@app.get("/risk/events")
def risk_events(limit: int = 50) -> dict:
    """Read-only immutable risk-event history (§ R2.0): CONFIGURATION_UPDATED events + the existing
    authoritative kill-switch audit events (surfaced, not duplicated). Side-effect free. No secrets."""
    try:
        n = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        n = 50
    with ctx.lock:
        return build_risk_events(ctx.store, n)


@app.post("/risk/config")
def risk_config_update(body: RiskConfigUpdate, authorization: str | None = Header(default=None)) -> dict:
    """Authenticated Risk Control configuration update (§ R2.0). Control-token auth + validation +
    optimistic version check (rejects a stale update, incl. an out-of-band canonical change) + one atomic
    transaction that writes the canonical risk_config + the policy + an immutable CONFIGURATION_UPDATED
    event (rolled back together on failure). It NEVER mutates the kill switch and calls NO broker /
    execution / order / RiskEngine code — changing limits here does NOT enable trading."""
    _auth(authorization)
    normalized, errors = validate_config(body.model_dump())
    if errors:
        raise HTTPException(422, {"detail": "validation failed", "errors": errors})
    with ctx.lock:
        r = ctx.store.apply_risk_control_update(
            expected_token=body.expected_version or "", capital=normalized["capital"],
            risk_per_trade_pct=normalized["max_position_risk_pct"],
            max_daily_loss_pct=normalized["max_daily_loss_pct"], currency=normalized["currency"],
            warning_threshold_pct=normalized["warning_threshold_pct"],
            max_portfolio_exposure_pct=normalized["max_portfolio_exposure_pct"],
            max_drawdown_pct=normalized["max_drawdown_pct"], actor="operator")
        if not r.get("ok"):
            raise HTTPException(409, {"detail": "version conflict — reload /risk/config and retry",
                                      "current_version_token": r.get("current_token")})
        view = build_risk_config_view(ctx.store)
    return {"ok": True, "configuration_version": r["version"], **view}


# ---------------------------------------------------------------- § R3.0 backtesting (RESEARCH ONLY)
class BacktestCreate(BaseModel):
    """POST /backtests body. Starts an INTERNAL historical research run — no trading fields, no order,
    no execution. All values are validated + bounded before persistence. R3.0A: `dataset_id` is REQUIRED
    — a run pins an explicit, immutable, checksum-verified research dataset (there is no implicit latest)."""
    strategy_id: str = "OHLC_TREND_BASELINE"
    strategy_version: int = 1
    symbols: list[str] = []
    interval: str = "1D"
    start: str | None = None
    end: str | None = None
    starting_capital: float | str = "100000"
    currency: str = "USD"
    costs: dict | None = None
    risk: dict | None = None
    max_concurrent_positions: int = 3
    dataset_id: str | None = None


@app.post("/backtests")
def create_backtest(body: BacktestCreate, authorization: str | None = Header(default=None)) -> dict:
    """§ R3.0 — start a deterministic, bounded, INTERNAL historical research run. RESEARCH ONLY: it never
    enables trading, never creates or submits a broker/order/execution object, never touches the kill
    switch. Auth + strict validation (≤5 symbols, 1h/1D, bounded range) before any persistence; a data
    problem fails the RUN (persisted FAILED with a code + audit), never crashes. R3.0A: `dataset_id` is
    REQUIRED and the pinned dataset is validated (COMPLETED, symbols, interval, range, calendar,
    adjustment/normalization, checksum) before the run is created."""
    _auth(authorization)
    if not body.dataset_id:
        raise HTTPException(422, {"detail": "validation failed",
                                  "errors": ["dataset_id is required — pin an explicit COMPLETED research "
                                             "dataset (no implicit 'latest')"]})
    try:
        with ctx.lock:
            run_id = run_backtest(ctx.store, owner="operator", req=body.model_dump(),
                                  commit_ref=os.environ.get("ATP_COMMIT_REF"), dataset_id=body.dataset_id)
    except BtValidationError as e:
        raise HTTPException(422, {"detail": "validation failed", "errors": e.errors})
    except OneActiveRunError:
        raise HTTPException(409, {"detail": "ONE_ACTIVE_RUN", "message": "an active research run already exists"})
    with ctx.lock:
        row = ctx.store.bt_get_run(run_id)
    return bt_read.run_detail(row)


# ------------------------------------------------------- § R3.0A research datasets (immutable, versioned)
class DatasetCreate(BaseModel):
    """POST /research/datasets body — request an immutable historical OHLC dataset (US equities, 1D). No
    trading, no order, no execution. Bounded to the approved R3.0A universe/range before any provider call."""
    symbols: list[str] = []
    interval: str = "1D"
    start: str | None = None
    end: str | None = None


@app.post("/research/datasets", status_code=202)
def create_dataset(body: DatasetCreate, response: Response,
                   authorization: str | None = Header(default=None)) -> dict:
    """§ R3.0A.1 — ENQUEUE an immutable research dataset (does NOT execute it). This endpoint performs ZERO
    provider network I/O and NO normalization/backfill inside the request, and never holds `ctx.lock` across
    long-running work: it only authenticates, validates the bounded request, and idempotently creates or
    reuses a PLANNED dataset. The actual chunked backfill runs OUTSIDE atp-control in the durable one-shot
    worker (`python -m atp.research.backfill.worker`). Returns 202 (PLANNED/RUNNING) or 200 (reused COMPLETED)
    with dataset_id, request checksum and status. RESEARCH DATA ONLY: never trades, never touches live
    `ohlc_bars`."""
    _auth(authorization)
    if not body.start or not body.end:
        raise HTTPException(422, {"detail": "dataset request invalid", "error": "start and end are required"})
    try:
        req = bf.build_request(body.symbols, body.interval, body.start, body.end)
    except bf.DatasetRequestError as e:
        raise HTTPException(422, {"detail": "dataset request invalid", "error": str(e)})
    with ctx.lock:   # store-only + fast; NO provider I/O is performed here
        enq = bf.enqueue_backfill(ctx.store, req, owner="operator")
        row = ctx.store.rd_get_dataset(enq["dataset_id"])
        detail = bf.dataset_detail(ctx.store, row)
    response.status_code = 200 if enq["reused"] else 202
    return detail


@app.get("/research/datasets")
def list_datasets(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """Read-only list of research datasets (bounded page)."""
    with ctx.lock:
        rows = ctx.store.rd_list_datasets(owner="operator", status=status, limit=limit, offset=offset)
        return bf.datasets_list(ctx.store, rows)


@app.get("/research/datasets/{dataset_id}")
def get_dataset(dataset_id: str) -> dict:
    with ctx.lock:
        row = ctx.store.rd_get_dataset(dataset_id)
        if row is None:
            raise HTTPException(404, {"detail": "not found"})
        return bf.dataset_detail(ctx.store, row)


@app.get("/research/datasets/{dataset_id}/coverage")
def get_dataset_coverage(dataset_id: str) -> dict:
    with ctx.lock:
        row = ctx.store.rd_get_dataset(dataset_id)
        if row is None:
            raise HTTPException(404, {"detail": "not found"})
        return bf.dataset_coverage(ctx.store, row)


# ------------------------------------------------------ § R3.1A AI-validation (RESEARCH DATA ONLY, GET-only)
# NO POST that runs collection / evaluation / validation — those are external one-shot workers
# (python -m atp.research.intel.worker | atp.research.validation.worker), never HTTP-triggered.
@app.get("/research/validation/coverage")
def research_validation_coverage() -> dict:
    """Collection coverage, effective vs raw sample counts, provenance quality, pilot policy versions, the
    legacy reconciliation diagnostic, and the frozen evidence gate. Read-only; never trades."""
    with ctx.lock:
        return val_read.coverage_view(ctx.store)


@app.get("/research/validation/runs")
def research_validation_runs(limit: int = 50) -> dict:
    with ctx.lock:
        return val_read.runs_view(ctx.store.rv_list_runs(limit=limit))


@app.get("/research/validation/runs/{run_id}")
def research_validation_run(run_id: str) -> dict:
    with ctx.lock:
        row = ctx.store.rv_get_run(run_id)
        if row is None:
            raise HTTPException(404, {"detail": "not found"})
        return val_read.run_detail(ctx.store, row)


@app.get("/research/intel/legacy-reconciliation")
def research_intel_legacy_reconciliation() -> dict:
    """READ-ONLY diagnostic proving the legacy governance/prediction orphan + NVDA discrepancy. Never
    modifies legacy rows."""
    with ctx.lock:
        return reconcile_legacy(ctx.store)


@app.get("/backtests")
def list_backtests(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """Read-only list of research runs (bounded page)."""
    with ctx.lock:
        rows = ctx.store.bt_list_runs(owner="operator", status=status, limit=limit, offset=offset)
    page = min(100, max(1, int(limit)))
    return {"count": len(rows), "runs": [bt_read.run_summary(r) for r in rows],
            "next_offset": (int(offset) + len(rows)) if len(rows) >= page else None}


@app.get("/backtests/{run_id}")
def get_backtest(run_id: str) -> dict:
    with ctx.lock:
        row = ctx.store.bt_get_run(run_id)
    if row is None:
        raise HTTPException(404, {"detail": "not found"})
    return bt_read.run_detail(row)


@app.get("/backtests/{run_id}/metrics")
def get_backtest_metrics(run_id: str) -> dict:
    with ctx.lock:
        row = ctx.store.bt_get_run(run_id)
        if row is None:
            raise HTTPException(404, {"detail": "not found"})
        m = ctx.store.bt_get_metrics(run_id)
    return {"run_id": run_id, "run_status": row.status, **bt_read.metrics_view(m)}


@app.get("/backtests/{run_id}/trades")
def get_backtest_trades(run_id: str, limit: int = 1000, offset: int = 0) -> dict:
    with ctx.lock:
        if ctx.store.bt_get_run(run_id) is None:
            raise HTTPException(404, {"detail": "not found"})
        rows = ctx.store.bt_list_trades(run_id, limit=limit, offset=offset)
    return {"run_id": run_id, "count": len(rows), "trades": bt_read.trades_view(rows)}


@app.get("/backtests/{run_id}/equity")
def get_backtest_equity(run_id: str, limit: int = 50000) -> dict:
    with ctx.lock:
        if ctx.store.bt_get_run(run_id) is None:
            raise HTTPException(404, {"detail": "not found"})
        rows = ctx.store.bt_list_equity(run_id, limit=limit)
    return {"run_id": run_id, "count": len(rows), "equity": bt_read.equity_view(rows)}


@app.get("/backtests/{run_id}/events")
def get_backtest_events(run_id: str, limit: int = 500, offset: int = 0) -> dict:
    with ctx.lock:
        if ctx.store.bt_get_run(run_id) is None:
            raise HTTPException(404, {"detail": "not found"})
        rows = ctx.store.bt_list_events(run_id, limit=limit, offset=offset)
    return {"run_id": run_id, "count": len(rows), "events": bt_read.events_view(rows)}


@app.get("/macro/current")
def macro_current() -> dict:
    """Read-only Macro Intelligence (§ Phase R1.2): the current global macro environment — a
    deterministic 0-100 score, the risk regime (RISK_ON / RISK_NEUTRAL / RISK_OFF), signals, risks and
    the raw metrics (rates, inflation, employment, VIX, USD, commodities) with their trend. INTELLIGENCE
    INPUT only — never a trade, order, or broker action. No snapshot -> NO DATA, never fabricated."""
    with ctx.lock:
        return build_macro(ctx.store)


@app.get("/market/{symbol}/institutional-flow")
def market_institutional_flow(symbol: str) -> dict:
    """Read-only Institutional Intelligence (§ Phase R1.3): 'smart money' flow for a symbol — the 13F
    quarter-over-quarter position changes (ACCUMULATION / REDUCTION / NEW_POSITION / EXIT) + an
    accumulation score, and recent SEC Form 4 insider BUY/SELL activity + insider sentiment. INTELLIGENCE
    INPUT only — never a trade, order, copy-trade, or broker action. No data -> NO DATA, never
    fabricated. No secrets. Public read-model."""
    with ctx.lock:
        return build_institutional_flow(ctx.store, symbol.upper())


@app.get("/market/{symbol}/insider-cluster")
def market_insider_cluster(symbol: str) -> dict:
    """Read-only Insider Cluster Intelligence (§ Phase R1.4): whether a role-weighted CLUSTER of insider
    buying (ACCUMULATION) or selling (DISTRIBUTION) is present for a symbol across the 7/30/90-day
    windows, with a 0-100 score, participant count and a plain summary. INTELLIGENCE ONLY — not a trading
    signal, order, or broker action. No Form 4 data -> NO DATA (never a fabricated cluster). No secrets."""
    with ctx.lock:
        return build_insider_cluster(ctx.store, symbol.upper())


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


# -------------------------------------------------------- durable Paper Canary (single-owner proxy)
@app.get("/paper-canary/status/{run_id}")
def paper_canary_status(run_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Authenticated DB-only status. Control never constructs or calls the runtime owner."""
    _auth(authorization)
    with ctx.lock:
        run = ctx.store.get_paper_run(run_id)
        if run is None:
            raise HTTPException(404, "Paper Canary run not found")
        account = ctx.store.get_paper_account(run_id)
        orders = ctx.store.list_paper_orders(run_id=run_id)
        fills = ctx.store.list_paper_fills(run_id=run_id)
        positions = ctx.store.list_paper_positions(run_id=run_id)
        reconciliations = ctx.store.list_paper_reconciliations(run_id=run_id)
    return _paper_jsonable(
        {
            "run": run,
            "account": account,
            "orders": orders,
            "fills": fills,
            "positions": positions,
            "latest_reconciliation": reconciliations[-1] if reconciliations else None,
        }
    )


@app.post("/control/paper-canary/prepare")
def paper_canary_prepare(
    body: PaperCanaryPrepareBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Validate the complete server-owned PAPER boundary and stop at global READY_FOR_ARM.

    The Store performs the decisive checks, missing risk-baseline initialization, runtime CAS and audit
    write in one transaction.  This endpoint never arms/starts, creates a run, or touches a broker.
    """
    _auth(authorization)
    if not _paper_offensive_enabled():
        raise HTTPException(404, "durable Paper Canary is disabled")
    if re.fullmatch(r"[0-9a-f]{40}", body.expected_commit_sha) is None:
        raise HTTPException(422, "expected_commit_sha must be an exact 40-lowerhex SHA")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", body.expected_config_checksum) is None:
        raise HTTPException(422, "expected_config_checksum must be sha256 plus 64 lowercase hex digits")
    if re.fullmatch(r"[0-9a-f]{20}", body.expected_risk_version_token) is None:
        raise HTTPException(422, "expected_risk_version_token must be 20 lowercase hex digits")
    try:
        deployed_commit = resolve_commit_sha()
    except CommitVerificationError as exc:
        raise HTTPException(409, f"deployed commit verification failed: {exc.code}") from exc
    if deployed_commit != body.expected_commit_sha:
        raise HTTPException(409, "expected commit does not match the deployed commit")
    config_json = os.environ.get("ATP_PAPER_CANARY_CONFIG_JSON")
    if type(config_json) is not str or not config_json:
        raise HTTPException(503, "Paper Canary server config is not configured")
    reason = body.reason or "bounded Paper Canary pre-arm validation passed"
    try:
        with ctx.paper_lock, ctx.lock:
            result = ctx.store.prepare_paper_runtime(
                config_json=config_json,
                commit_sha=deployed_commit,
                expected_config_checksum=body.expected_config_checksum,
                expected_risk_config_checksum=body.expected_risk_version_token,
                actor="operator",
                reason=reason,
            )
    except PaperCanaryError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Paper Canary server configuration is invalid") from exc
    return {
        "ok": True,
        "status": result["status"],
        "commit_sha": result["commit_sha"],
        "config_checksum": result["config_checksum"],
        "risk_version_token": result["risk_config_checksum"],
        "instrument": result["market_data_symbol"],
        "risk_state_initialized": result["risk_state_initialized"],
        "daily_pnl_observed": result["daily_pnl_observed"],
    }


@app.post("/control/paper-canary/create")
def paper_canary_create(
    body: PaperCanaryCreateBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _paper_owner_request("create", body.model_dump(exclude_none=True))


@app.post("/control/paper-canary/activate")
def paper_canary_activate(
    body: PaperCanaryActivateBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _paper_owner_request("activate", body.model_dump(exclude_none=True))


@app.post("/control/paper-canary/submit")
def paper_canary_submit(
    body: PaperCanarySubmitBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _paper_owner_request("submit", body.model_dump())


@app.post("/control/paper-canary/recover")
def paper_canary_recover(
    body: PaperCanaryRecoveryBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _paper_owner_request("recover", body.model_dump(exclude_none=True))


@app.post("/control/paper-canary/stop")
def paper_canary_stop(
    body: PaperCanaryRecoveryBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _paper_owner_request("stop", body.model_dump(exclude_none=True))


def _paper_stopped_proof(run_id: str) -> dict:
    """Recompute and verify the terminal proof required before global disable.

    A persisted PASS is only evidence, not authority by itself: re-run the deterministic ledger
    replay against the current immutable fills/orders and mutable projections, then additionally
    require a flat account and positions.  No supported writer can mutate a terminal STOPPED run.
    """
    try:
        verified = verify_paper_stopped(ctx.store, run_id=run_id)
    except (PaperCanaryError, TypeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not verified.ok:
        raise HTTPException(
            409,
            "Paper Canary is not durably STOPPED, flat, and cleanly reconciled",
        )
    active = [
        item
        for status in _PAPER_RECOVERY_STATUSES
        for item in ctx.store.list_paper_runs(status=status, limit=2)
    ]
    if active:
        raise HTTPException(409, "another active Paper Canary run prevents global disable")
    return {
        "run": _paper_jsonable(verified.run),
        "reconciliation": _paper_jsonable(verified.reconciliation),
    }


def _require_no_active_paper_run() -> None:
    active = [
        item
        for status in _PAPER_RECOVERY_STATUSES
        for item in ctx.store.list_paper_runs(status=status, limit=2)
    ]
    if active:
        raise HTTPException(409, "an active Paper Canary run prevents run-less global disable")


@app.post("/control/paper-canary/disable")
def paper_canary_disable(
    body: PaperCanaryDisableBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Stop/reconcile one run, then risk-reduce the global runtime all the way to DISABLED.

    This operation is retryable after partial progress.  It refuses to reset a kill or bypass recovery,
    and it is available even after the offensive feature flag has been turned off.
    """
    _auth(authorization)
    reason = body.reason or "bounded Paper Canary complete"
    with ctx.paper_lock:
        if body.run_id is None:
            with ctx.lock:
                _require_no_active_paper_run()
            proof = {"run": None, "reconciliation": None}
        else:
            with ctx.lock:
                existing = ctx.store.get_paper_run(body.run_id)
            if existing is None:
                raise HTTPException(404, "Paper Canary run not found")
            if existing.status != "STOPPED":
                result = _paper_owner_request(
                    "stop", {"run_id": body.run_id, "reason": reason},
                )
                if result.get("ok") is not True:
                    raise HTTPException(409, "Paper Canary stop/reconciliation did not pass")
            with ctx.lock:
                proof = _paper_stopped_proof(body.run_id)
        with ctx.lock:
            try:
                disabled = ctx.store.disable_paper_runtime_if_no_active(
                    actor="operator",
                    reason=reason,
                    expected_run_id=body.run_id,
                )
            except (PaperCanaryError, TypeError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "status": disabled["status"], **proof}


# ---------------------------------------------------------------- control commands (authenticated)
@app.post("/control/recover")
def ctl_recover(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    with ctx.paper_lock:
        return _ctl_recover_locked()


def _ctl_recover_locked() -> dict:
    paper_run = None
    with ctx.lock:
        ctx.life.recover()
        if ctx.life.status is not RuntimeStatus.RECOVERY_REQUIRED:
            return {"ran": False, "status": ctx.life.status.value,
                    "note": "not in RECOVERY_REQUIRED — no sequence run"}
        try:
            active = [
                run
                for status in _PAPER_RECOVERY_STATUSES
                for run in ctx.store.list_paper_runs(status=status, limit=2)
            ]
        except Exception:  # noqa: BLE001 - the read-only checker will fail closed on the same Store
            active = []
        if len(active) == 1:
            paper_run = active[0]

    paper_proof = None
    if paper_run is not None:
        try:
            paper_proof = _paper_owner_request(
                "recover",
                {
                    "run_id": paper_run.run_id,
                    "reason": "global recovery: cancel pending paper work and prove durable ledger",
                },
            )
        except HTTPException:
            paper_proof = False

    with ctx.lock:
        if ctx.life.status is not RuntimeStatus.RECOVERY_REQUIRED:
            return {"ran": False, "status": ctx.life.status.value,
                    "note": "recovery state changed before sequence run"}
        broker_positions = (
            {} if os.environ.get("BROKER_EXECUTION_ENABLED") == "false" else None
        )
        ok, results = ctx.life.run_recovery(
            build_recovery_checks(
                ctx.store,
                broker_positions=broker_positions,
                paper_recovery_proof=paper_proof,
            ),
        )
    return {"ran": True, "ok": ok, "status": ctx.life.status.value, "results": results}


@app.post("/control/arm")
def ctl_arm(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.paper_lock, ctx.lock:
            return {"status": ctx.life.arm(actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


@app.post("/control/start")
def ctl_start(body: Confirm, authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.paper_lock, ctx.lock:
            return {"status": ctx.life.start(confirm=body.confirm, actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


@app.post("/control/kill")
def ctl_kill(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    # Emergency KILL must never wait behind a slow owner HTTP command. Commit the durable latch
    # on its own short-lived Store connection. Paper cleanup is deliberately detached: a stuck
    # owner lock or broken primary Control connection cannot delay acknowledgement of the latch.
    status = _emergency_kill()
    threading.Thread(
        target=_best_effort_paper_cleanup_after_kill,
        name="paper-kill-cleanup",
        daemon=True,
    ).start()
    return {"status": status}


def _best_effort_paper_cleanup_after_kill() -> None:
    try:
        with ctx.paper_lock:
            try:
                with ctx.lock:
                    active = [
                        item
                        for run_status in _PAPER_RECOVERY_STATUSES
                        for item in ctx.store.list_paper_runs(status=run_status, limit=2)
                    ]
            except Exception:  # noqa: BLE001 - KILL is already durable; cleanup is best effort only
                return
            # Owner notification is risk-reducing best effort and deliberately outside the
            # Control DB lock / Store transaction.
            for run in active[:1]:
                try:
                    command = (
                        "recover" if run.status in {"RUNNING", "RECOVERY_REQUIRED"} else "stop"
                    )
                    _paper_owner_request(
                        command,
                        {
                            "run_id": run.run_id,
                            "reason": "global kill: reconcile and cancel pending work",
                        },
                    )
                except Exception:  # noqa: BLE001 - never weaken or obscure the committed KILL
                    pass
    except Exception:  # noqa: BLE001 - locks/context may be unavailable during process failure
        return


def _emergency_kill() -> str:
    """Latch KILL through a dedicated DB connection, independent of long Control reads/research."""
    emergency_store = open_store(build_dsn(), migrate=False)
    try:
        return LifecycleManager(emergency_store).kill(
            actor="operator",
            reason="control kill",
        ).value
    finally:
        emergency_store.close()


@app.post("/control/reset")
def ctl_reset(authorization: str | None = Header(default=None)) -> dict:
    _auth(authorization)
    try:
        with ctx.paper_lock, ctx.lock:
            return {"status": ctx.life.reset_kill(actor="operator").value}
    except Exception as e:
        raise HTTPException(409, str(e))


def main() -> None:
    import uvicorn
    port = int(os.environ.get("ATP_CONTROL_PORT", "9103"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
