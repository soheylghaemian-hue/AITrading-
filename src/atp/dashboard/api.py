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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..brokers.base import Broker
from ..desk.desk import AutonomousTradingDesk
from ..governance.registry import StrategyRegistry
from ..journal.store import TradeJournal
from ..risk.engine import RiskEngine
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
        )
        return snap.as_dict()

    def emergency_stop(self, reason: str = "manual emergency stop") -> dict:
        """Trip the Risk Engine kill switch (§13). Stops ALL new orders; does not auto-flatten."""
        self.risk.kill_switch(reason)
        if self.notifications is not None:
            self.notifications.push(Kind.EMERGENCY_STOP, f"TRADING HALTED — {reason}",
                                    severity=Severity.CRITICAL)
        return {"status": "halted", "reason": reason}

    def resume(self, reason: str = "manual resume") -> dict:
        self.risk.reset_kill()
        if self.notifications is not None:
            self.notifications.push(Kind.SYSTEM_ERROR, f"trading resumed — {reason}",
                                    severity=Severity.WARNING)
        return {"status": "resumed", "reason": reason}


def create_app(context: DashboardContext) -> Any:
    """Build the FastAPI app. Lazy-imports FastAPI so the module loads without it.

    The emergency-stop / resume mutations require a bearer token from the ATP_DASHBOARD_TOKEN
    env var (never a hard-coded secret). If unset, mutations are disabled (read-only server)."""
    from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: PLC0415
    from fastapi.responses import FileResponse, JSONResponse  # noqa: PLC0415

    app = FastAPI(title="atp command center", docs_url="/api/docs")
    token = os.environ.get("ATP_DASHBOARD_TOKEN")

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

    @app.get("/")
    async def index() -> Any:
        page = _STATIC / "index.html"
        if page.exists():
            return FileResponse(str(page))
        return JSONResponse({"detail": "dashboard page not found"}, status_code=404)

    return app
