"""Run the READ-ONLY Dashboard API backed by REAL IBKR data (Phase 7).

Data context = the existing IBKR adapter in READ-ONLY mode (not the PaperBroker). IBKR runs in
its OWN thread + event loop (ib_insync's natural habitat, avoiding the uvicorn-loop import issue
on Python 3.14); FastAPI serves a thread-safe cache. NO orders, NO placeOrder/cancelOrder/
modifyOrder, NO execution. On any IBKR failure it shows **IBKR DATA UNAVAILABLE** with the real
error — it NEVER falls back to the PaperBroker and NEVER fabricates data.

    ATP_DASHBOARD_TOKEN=... ATP_DASHBOARD_READ_TOKEN=... ATP_DASHBOARD_CORS_ORIGINS=https://www.gigbay.de \
    PYTHONPATH=src python3 examples/serve_dashboard_ibkr.py            # http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timezone

from atp.dashboard.api import DashboardContext, create_app
from atp.dashboard.notifications import NotificationCenter
from atp.dashboard.snapshot import build_snapshot
from atp.live.marketdata import DEFAULT_UNIVERSE, probe_market_data, subscription_report
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore


class IBKRWorker(threading.Thread):
    """Owns the read-only IBKR connection in its own asyncio loop and refreshes a shared cache.
    Nothing here can trade — the broker is read-only and only get_account/positions/market data
    are called."""

    def __init__(self, *, host, port, client_id, interval):
        super().__init__(daemon=True)
        self._host, self._port, self._cid, self._interval = host, port, client_id, interval
        self.cache: dict = {"connected": False, "account": None, "buying_power": None,
                            "market_data": [], "subscriptions": [], "error": None}

    def run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        from atp.brokers.ibkr import IBKRBroker, IBKRConfig  # imported inside the running loop
        broker = IBKRBroker(IBKRConfig(host=self._host, port=self._port,
                                       client_id=self._cid, readonly=True))
        while True:
            try:
                if not broker.is_connected():
                    await broker.connect()
                account = await broker.get_account()
                bp = await self._buying_power(broker)
                md = await probe_market_data(broker, DEFAULT_UNIVERSE)
                self.cache = {"connected": True, "account": account, "buying_power": bp,
                              "market_data": md, "subscriptions": subscription_report(md), "error": None}
            except Exception as exc:  # noqa: BLE001 — surface, never fake, never paper
                self.cache = {"connected": False, "account": None, "buying_power": None,
                              "market_data": [], "subscriptions": [], "error": repr(exc)}
                try:
                    await broker.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self._interval)

    @staticmethod
    async def _buying_power(broker):
        try:
            ib = broker._require()  # noqa: SLF001
            for row in ib.accountValues():
                if row.tag == "BuyingPower":
                    return float(row.value)
        except Exception:  # noqa: BLE001
            return None
        return None


def _unavailable_rows(error: str | None) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [{
        "symbol": s, "asset_class": ac.value, "exchange": ex, "status": "ERROR",
        "market_data_type": None, "bid": None, "ask": None, "last": None,
        "bid_size": None, "ask_size": None, "timestamp": now, "error_code": None,
        "error_message": error or "IBKR connection unavailable", "reason": "IBKR DATA UNAVAILABLE",
    } for s, ac, ex in DEFAULT_UNIVERSE]


class IBKRContext:
    """Adapts the IBKR worker's cache to the dashboard control surface. Delegates the risk/config/
    emergency-stop logic to a real DashboardContext; provides its own read-only snapshot."""

    def __init__(self, base: DashboardContext, worker: IBKRWorker):
        self._base, self._worker = base, worker

    async def snapshot_dict(self) -> dict:
        c = self._worker.cache
        connected = bool(c["connected"])
        md = c["market_data"] if (connected and c["market_data"]) else _unavailable_rows(c.get("error"))
        notes = self._base.notifications.recent(50) if self._base.notifications is not None else []
        data_ok = any(r["status"] in ("DATA_AVAILABLE", "DELAYED") for r in md) if md else None
        snap = build_snapshot(
            account=c["account"], risk=self._base.risk, mode=self._base.mode, connected=connected,
            execution_enabled=self._base.execution_enabled, market_data=md,
            subscriptions=c["subscriptions"], buying_power=c["buying_power"],
            risk_config=self._base.risk_config,
            risk_capital=(self._base.risk_config.capital if self._base.risk_config else None),
            notifications=notes, data_ok=data_ok,
        )
        return snap.as_dict()

    # control surface used by create_app — delegate to the real context (RiskEngine authoritative)
    def emergency_stop(self, *a, **k):
        return self._base.emergency_stop(*a, **k)

    def resume(self, *a, **k):
        return self._base.resume(*a, **k)

    def set_risk_config(self, *a, **k):
        return self._base.set_risk_config(*a, **k)


def main() -> None:
    import uvicorn  # noqa: PLC0415
    assert os.environ.get("ATP_DASHBOARD_TOKEN"), "set ATP_DASHBOARD_TOKEN"

    risk = RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=0.0, peak_equity=0.0))
    base = DashboardContext(
        broker=type("_Stub", (), {"is_connected": staticmethod(lambda: False)})(),
        risk=risk, notifications=NotificationCenter.from_env(), mode="paper",
        execution_enabled=False, config_store=RiskConfigStore(),
    )
    loaded = base.load_persisted_risk_config()
    if loaded is not None:
        risk.state.day_start_equity = loaded.capital
        risk.state.peak_equity = loaded.capital

    worker = IBKRWorker(host=os.environ.get("IBKR_HOST", "127.0.0.1"),
                        port=int(os.environ.get("IBKR_PORT", "4002")),
                        client_id=int(os.environ.get("IBKR_CLIENT_ID", "20")),
                        interval=float(os.environ.get("DASHBOARD_MD_INTERVAL", "30")))
    worker.start()

    app = create_app(IBKRContext(base, worker))
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")  # localhost only
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    print(f"IBKR read-only dashboard backend on http://{host}:{port}  (paper · read-only · no execution)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
