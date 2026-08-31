"""FastAPI backend for the Command Center (§21/§22/§30).

Thin, **read-only** serializer over `build_snapshot` plus one protected control: the emergency
stop. FastAPI is a live/optional dependency, lazy-imported inside `create_app`, so importing
this module never requires it. The dashboard/frontend NEVER makes trading decisions — it reads
state and can only trip the Risk Engine's kill switch (§13). No secrets ever live in the
frontend or in this file.

The read endpoints (§30) all slice the same snapshot so the frontend has one source of truth.
Where there is no real data, fields are null/empty — never fabricated (§33).
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..brokers.base import Broker
from ..desk.desk import AutonomousTradingDesk
from ..governance.registry import StrategyRegistry
from ..journal.store import TradeJournal
from ..risk.config import TradingRiskConfig
from ..risk.engine import RiskEngine
from ..risk.store import RiskConfigStore
from .notifications import Kind, NotificationCenter, Severity
from .snapshot import build_snapshot

_STATIC = Path(__file__).parent / "static"


@dataclass(slots=True)
class DashboardContext:
    """The live objects the dashboard reads from (and the single control it may trip)."""

    broker: Broker
    risk: RiskEngine
    desk: AutonomousTradingDesk | None = None
    journal: TradeJournal | None = None
    registry: StrategyRegistry | None = None
    notifications: NotificationCenter | None = None
    mode: str = "paper"                       # "paper" | "live" — never hidden (§25)
    scan_funnel: dict | None = None           # discovery funnel counts, or None (NO DATA)
    execution_enabled: bool = False           # read-only validation: execution NOT wired (Phase 2A)
    # Optional read-only market snapshots the runner refreshes out-of-band (never fabricated):
    market_data: list[dict] | None = None     # per-instrument quote availability (5 states)
    subscriptions: list[dict] | None = None   # required/missing IBKR data subscriptions
    ai_analysis: list[dict] | None = None      # read-only agent observations/signals
    # TRADING RISK — the 3 user parameters. When set, the section shows the mandate capital;
    # otherwise the account equity is used as the capital reference. Changes are applied to the
    # authoritative Risk Engine (and, via on_risk_config_change, to the Position Sizer/policy).
    risk_config: TradingRiskConfig | None = None
    on_risk_config_change: Callable[[TradingRiskConfig], None] | None = None
    config_store: "RiskConfigStore | None" = None   # persistence (survives restart, §15)
    autonomous_engine: object | None = None         # PAPER AUTONOMOUS engine (default DISABLED)

    def _autonomous_inputs(self):
        try:
            connected = self.broker.is_connected()
        except Exception:  # noqa: BLE001
            connected = False
        return connected, (self.market_data or []), self.risk_config

    def autonomous(self, action: str, payload: dict | None = None) -> dict:
        """Token-gated control of the PAPER AUTONOMOUS engine. Never enables live trading; the
        Risk Engine stays authoritative. Two-step start (ARM then START with confirmation)."""
        eng = self.autonomous_engine
        payload = payload or {}
        if eng is None:
            return {"detail": "autonomous engine not available"}
        if action == "arm":
            return {"status": eng.arm().value}
        if action == "dry_run":
            return {"status": eng.dry_run(duration_minutes=float(payload.get("minutes", 60))).value}
        if action == "disarm":
            return {"status": eng.disarm().value}
        if action == "stop":
            return {"status": eng.stop().value}
        if action == "kill":
            return {"status": eng.kill(reason=payload.get("reason", "manual")).value}
        if action == "reset":
            return {"status": eng.reset_kill().value}
        if action == "start":
            connected, md, cfg = self._autonomous_inputs()
            return eng.start(confirm=payload.get("confirm"), connected=connected,
                             market_data=md, risk_config=cfg)
        return {"detail": f"unknown action: {action}"}

    async def snapshot_dict(self) -> dict:
        account = await self.broker.get_account()
        market = self.desk.latest_market() if self.desk is not None else {}
        data_ok = None
        dq = getattr(self.desk, "_data_quality", None) if self.desk is not None else None
        if dq is not None:
            data_ok = getattr(dq, "_connected", None)
        notes = self.notifications.recent(50) if self.notifications is not None else []
        buying_power = getattr(account, "buying_power", None)
        try:
            connected = self.broker.is_connected()
        except Exception:  # noqa: BLE001 — a broker without the hook is simply unknown
            connected = None
        snap = build_snapshot(
            account=account, risk=self.risk, journal=self.journal, registry=self.registry,
            market=market, mode=self.mode, data_ok=data_ok, scan_funnel=self.scan_funnel,
            notifications=notes, market_data=self.market_data, subscriptions=self.subscriptions,
            ai_analysis=self.ai_analysis, buying_power=buying_power,
            execution_enabled=self.execution_enabled, connected=connected,
            risk_config=self.risk_config,
            risk_capital=(self.risk_config.capital if self.risk_config else None),
            autonomous=(await self.autonomous_engine.snapshot(market_data=self.market_data)
                if self.autonomous_engine is not None else None),
        )
        return snap.as_dict()

    def load_persisted_risk_config(self) -> TradingRiskConfig | None:
        """On startup, re-apply the persisted TRADING RISK config to the authoritative Risk Engine
        so the user's three settings survive a restart (§15). Returns the loaded config or None."""
        if self.config_store is None:
            return None
        cfg = self.config_store.load()
        if cfg is None:
            return None
        eng = self.autonomous_engine
        update_guard = (
            eng.risk_configuration_update(actor="risk-config-load")
            if eng is not None and hasattr(eng, "risk_configuration_update")
            else nullcontext()
        )
        with update_guard:
            self.risk.update_limits(
                max_capital=cfg.capital,
                max_trade_risk_pct=cfg.risk_per_trade_pct,
                max_daily_loss_pct=cfg.max_daily_loss_pct,
            )
            self.risk_config = cfg
            if self.on_risk_config_change is not None:
                self.on_risk_config_change(cfg)
        return cfg

    def set_risk_config(self, capital: float, risk_per_trade_pct: float,
                        max_daily_loss_pct: float) -> dict:
        """Apply the 3-parameter TRADING RISK config to the authoritative Risk Engine (and the
        Position Sizer/policy via the optional hook). The Risk Engine then vetoes any order that
        would exceed risk-per-trade and blocks all new trades once the daily-loss limit is hit.
        This does NOT enable execution or live trading."""
        cfg = TradingRiskConfig(
            capital=float(capital), risk_per_trade_pct=float(risk_per_trade_pct),
            max_daily_loss_pct=float(max_daily_loss_pct),
        )
        eng = self.autonomous_engine
        update_guard = (
            eng.risk_configuration_update(actor="risk-config")
            if eng is not None and hasattr(eng, "risk_configuration_update")
            else nullcontext()
        )
        # Stop/invalidate any old execution epoch while RiskEngine and policy are rebound together.
        with update_guard:
            self.risk.update_limits(
                max_capital=cfg.capital,
                max_trade_risk_pct=cfg.risk_per_trade_pct,
                max_daily_loss_pct=cfg.max_daily_loss_pct,
            )
            self.risk_config = cfg
            if self.on_risk_config_change is not None:
                self.on_risk_config_change(cfg)
        if self.config_store is not None:
            self.config_store.save(cfg)       # persist — survives a restart (§15)
        if self.notifications is not None:
            self.notifications.push(
                Kind.SYSTEM_ERROR,
                f"trading risk updated — capital {cfg.capital:,.0f}, "
                f"risk/trade {cfg.risk_per_trade_pct:.2%}, daily loss {cfg.max_daily_loss_pct:.2%}",
                severity=Severity.INFO,
            )
        return cfg.as_dict()

    def emergency_stop(self, reason: str = "manual emergency stop") -> dict:
        """Trip the Risk Engine kill switch (§13). Stops ALL new orders; does not auto-flatten."""
        eng = self.autonomous_engine
        if eng is not None and hasattr(eng, "kill"):
            eng.kill(reason=reason, actor="dashboard")
        else:
            self.risk.kill_switch(reason)
        if self.notifications is not None:
            self.notifications.push(Kind.EMERGENCY_STOP, f"TRADING HALTED — {reason}",
                                    severity=Severity.CRITICAL)
        return {"status": "halted", "reason": reason}

    def resume(self, reason: str = "manual resume") -> dict:
        eng = self.autonomous_engine
        if eng is not None and hasattr(eng, "reset_kill"):
            eng.reset_kill(actor="dashboard")
        else:
            self.risk.reset_kill()
        if self.notifications is not None:
            self.notifications.push(Kind.SYSTEM_ERROR, f"trading resumed — {reason}",
                                    severity=Severity.WARNING)
        return {"status": "resumed", "reason": reason}


def create_app(context: DashboardContext) -> Any:
    """Build the FastAPI app. Lazy-imports FastAPI so the module loads without it.

    The emergency-stop / resume mutations require a bearer token from the ATP_DASHBOARD_TOKEN
    env var (never a hard-coded secret). If unset, mutations are disabled (read-only server)."""
    from fastapi import Body, Depends, FastAPI, Header, HTTPException  # noqa: PLC0415
    from fastapi.responses import FileResponse, JSONResponse  # noqa: PLC0415

    app = FastAPI(title="atp command center", docs_url="/api/docs")
    token = os.environ.get("ATP_DASHBOARD_TOKEN")

    # --- security: CORS (locked to the production origin), rate limiting, read-token auth ------
    from collections import deque as _deque  # noqa: PLC0415
    from time import monotonic as _monotonic  # noqa: PLC0415

    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

    origins = [o.strip() for o in os.environ.get(
        "ATP_DASHBOARD_CORS_ORIGINS", "https://www.gigbay.de").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"],
    )

    read_token = os.environ.get("ATP_DASHBOARD_READ_TOKEN")        # required on reads when set
    rate_limit = int(os.environ.get("ATP_DASHBOARD_RATE_LIMIT", "60"))   # requests / 60s / IP
    _hits: dict[str, Any] = {}

    @app.middleware("http")
    async def _guard(request, call_next):
        # per-IP sliding-window rate limit — a read-only dashboard needs no burst traffic.
        ip = request.client.host if request.client else "?"
        now = _monotonic()
        dq = _hits.setdefault(ip, _deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= rate_limit:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        dq.append(now)
        # authenticate public reads (the Vercel proxy sends this token server-side, never the browser)
        if read_token and request.method == "GET" and request.url.path.startswith("/dashboard/"):
            if request.headers.get("authorization") != f"Bearer {read_token}":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    async def _snapshot() -> dict:
        return await context.snapshot_dict()

    def require_token(authorization: str = Header(default="")) -> None:
        if not token:
            raise HTTPException(status_code=503, detail="control disabled (ATP_DASHBOARD_TOKEN unset)")
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    # --- read-only dashboard endpoints (§30) — all slice the one snapshot --------
    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/snapshot")
    @app.get("/dashboard/summary")
    async def summary() -> JSONResponse:
        return JSONResponse(await _snapshot())

    def _slice(name: str):
        async def endpoint() -> JSONResponse:
            snap = await _snapshot()
            return JSONResponse(snap.get(name, {}))
        return endpoint

    for path, key in (
        ("/dashboard/opportunities", "market"),   # top opportunities live in the market view
        ("/dashboard/positions", "positions"),
        ("/dashboard/risk", "risk"),
        ("/dashboard/agents", "agents"),
        ("/dashboard/market-data", "market_data"),
        ("/dashboard/subscriptions", "subscriptions"),
        ("/dashboard/ai-analysis", "ai_analysis"),
        ("/dashboard/trading-risk", "trading_risk"),
        ("/dashboard/performance", "analytics_overall"),
        ("/dashboard/system", "system_health"),
        ("/dashboard/notifications", "notifications"),
        ("/dashboard/governance", "governance"),
    ):
        app.add_api_route(path, _slice(key), methods=["GET"])

    @app.get("/dashboard/reconciliation")
    async def reconciliation() -> JSONResponse:
        # Positions the dashboard sees are the broker's; a full reconciliation is run by the
        # engine (atp.brokers.reconcile) — surfaced here when a break has been recorded.
        snap = await _snapshot()
        return JSONResponse({"positions": snap["positions"], "risk": snap["risk"]})

    # --- protected control: emergency stop / resume (§13) -----------------------
    @app.post("/dashboard/emergency-stop")
    async def emergency_stop(_: None = Depends(require_token)) -> JSONResponse:
        return JSONResponse(context.emergency_stop())

    @app.post("/dashboard/resume")
    async def resume(_: None = Depends(require_token)) -> JSONResponse:
        return JSONResponse(context.resume())

    @app.post("/dashboard/risk-config")
    async def risk_config(payload: dict = Body(...), _: None = Depends(require_token)) -> JSONResponse:
        """Set the 3 TRADING RISK parameters (capital, risk-per-trade %, max-daily-loss %). Token
        protected. Applies to the authoritative Risk Engine; does NOT enable execution."""
        try:
            result = context.set_risk_config(
                capital=payload["capital"],
                risk_per_trade_pct=payload["risk_per_trade_pct"],
                max_daily_loss_pct=payload["max_daily_loss_pct"],
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"missing field: {exc}")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse(result)

    # --- PAPER AUTONOMOUS control (§ Phase 8.5) — token-gated, never enables live trading -------
    def _autonomous_route(action: str):
        async def endpoint(payload: dict = Body(default={}), _: None = Depends(require_token)) -> JSONResponse:
            return JSONResponse(context.autonomous(action, payload))
        return endpoint

    for _act in ("arm", "disarm", "dry_run", "start", "stop", "kill", "reset"):
        app.add_api_route(f"/dashboard/autonomous/{_act}", _autonomous_route(_act), methods=["POST"])

    @app.get("/")
    async def index() -> Any:
        page = _STATIC / "index.html"
        if page.exists():
            return FileResponse(str(page))
        return JSONResponse({"detail": "dashboard page not found"}, status_code=404)

    return app
