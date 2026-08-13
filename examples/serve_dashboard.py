"""Run the READ-ONLY Dashboard API backend (FastAPI) — for the production frontend to read.

This is the private Trading Backend in the target architecture:

    Browser -> Vercel/Next.js -> authenticated HTTPS API -> THIS backend -> Risk Engine -> IBKR

It binds to localhost only. It uses a deterministic PaperBroker (no IBKR here — the broker seam
is unchanged) so the dashboard + TRADING RISK config chain can be verified end-to-end without
touching the gateway. Execution is disabled; nothing here can place an order.

    ATP_DASHBOARD_TOKEN=... ATP_RISK_CONFIG_PATH=~/.atp/risk_config.json \
    PYTHONPATH=src python3 examples/serve_dashboard.py            # http://127.0.0.1:8000

Endpoints: GET /dashboard/summary, /dashboard/trading-risk, … · POST /dashboard/risk-config
(and /emergency-stop, /resume) require Authorization: Bearer $ATP_DASHBOARD_TOKEN.
"""

from __future__ import annotations

import asyncio
import os

from atp.brokers.paper import PaperBroker
from atp.dashboard.api import DashboardContext, create_app
from atp.dashboard.notifications import NotificationCenter
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore


def build_context() -> DashboardContext:
    capital = float(os.environ.get("DASHBOARD_CAPITAL", "1000000"))
    broker = PaperBroker(starting_cash=capital)
    asyncio.run(broker.connect())
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=capital, peak_equity=capital))
    store = RiskConfigStore()  # persists to ATP_RISK_CONFIG_PATH or ~/.atp/risk_config.json
    ctx = DashboardContext(
        broker=broker, risk=risk, notifications=NotificationCenter.from_env(),
        mode="paper", execution_enabled=False, config_store=store,
    )
    # Restart persistence: re-apply the user's saved 3 parameters to the Risk Engine (§15).
    loaded = ctx.load_persisted_risk_config()
    if loaded is not None:
        print(f"[startup] restored risk config: capital={loaded.capital:,.0f} "
              f"risk/trade={loaded.risk_per_trade_pct:.2%} daily={loaded.max_daily_loss_pct:.2%}")
    else:
        print("[startup] no persisted risk config yet (showing engine defaults)")
    return ctx


def main() -> None:
    import uvicorn  # noqa: PLC0415
    if not os.environ.get("ATP_DASHBOARD_TOKEN"):
        print("⚠ ATP_DASHBOARD_TOKEN not set — mutations (risk-config, emergency-stop) return 503.")
    app = create_app(build_context())
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")   # localhost only — never 0.0.0.0 here
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    print(f"dashboard backend on http://{host}:{port}  (read-only · paper · no execution)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
