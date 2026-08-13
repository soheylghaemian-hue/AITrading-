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

from atp.autonomous import PaperAutonomousEngine
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard.api import DashboardContext, create_app
from atp.dashboard.notifications import NotificationCenter
from atp.dashboard.snapshot import build_snapshot
from atp.live import build_paper_stack
from atp.live.marketdata import DEFAULT_UNIVERSE, probe_market_data, subscription_report
from atp.marketdata import GLOBAL_UNIVERSE, MarketDataManager

# Provider-independent GLOBAL market-data grid (§ Phase 10). Classifies whatever the read-only IBKR
# probe returns for the symbols we actually query, against the global universe specs (region/venue).
_GLOBAL_SPECS = {s.symbol: s for s in GLOBAL_UNIVERSE}
_MD_MANAGER = MarketDataManager()


def _global_market_data(md_rows: list[dict]) -> list[dict]:
    specs = [_GLOBAL_SPECS[r["symbol"]] for r in md_rows if r.get("symbol") in _GLOBAL_SPECS]
    if not specs:
        return []
    raw = {r["symbol"]: r for r in md_rows}
    return _MD_MANAGER.dashboard_rows(_MD_MANAGER.classify(raw, specs=specs))
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore


class IBKRWorker(threading.Thread):
    """Owns the read-only IBKR connection in its own asyncio loop and refreshes a shared cache.
    Nothing here can trade — the broker is read-only and only get_account/positions/market data
    are called."""

    def __init__(self, *, host, port, client_id, interval, engine=None):
        super().__init__(daemon=True)
        self._host, self._port, self._cid, self._interval = host, port, client_id, interval
        self._engine = engine   # PaperAutonomousEngine — driven read-only from real data (DISABLED default)
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
                await self._drive_autonomous(md)
            except Exception as exc:  # noqa: BLE001 — surface, never fake, never paper
                self.cache = {"connected": False, "account": None, "buying_power": None,
                              "market_data": [], "subscriptions": [], "error": repr(exc)}
                try:
                    await broker.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self._interval)

    async def _drive_autonomous(self, md: list[dict]) -> None:
        """Feed the PAPER AUTONOMOUS engine one bar per available instrument, built from the REAL
        current mid (not synthetic). A no-op while the engine is DISABLED — nothing runs or trades
        until the user explicitly ARMs + STARTs. Errors here never affect read-only serving."""
        if self._engine is None:
            return
        now = datetime.now(timezone.utc)
        bars = []
        for row in md:
            if row.get("status") != "DATA_AVAILABLE":
                continue
            bid, ask = row.get("bid"), row.get("ask")
            if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))):
                continue
            mid = (float(bid) + float(ask)) / 2.0
            sym, ac = row["symbol"], AssetClass(row["asset_class"])
            inst = (Instrument(sym.split(".")[0], AssetClass.FX, currency=sym.split(".")[1])
                    if ac is AssetClass.FX and "." in sym else Instrument(sym, ac))
            bars.append(Bar(inst, mid, mid, mid, mid, 0.0, now))
        try:
            await self._engine.step(now=now, bars=bars, market_data=md)
        except Exception as exc:  # noqa: BLE001 — never let the paper engine break data serving
            print(f"  autonomous step error: {exc!r}")

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
        eng = self._base.autonomous_engine
        autonomous = (eng.snapshot(account=c["account"], risk_config=self._base.risk_config,
                                   market_data=md) if eng is not None else None)
        snap = build_snapshot(
            account=c["account"], risk=self._base.risk, mode=self._base.mode, connected=connected,
            execution_enabled=self._base.execution_enabled, market_data=md,
            subscriptions=c["subscriptions"], buying_power=c["buying_power"],
            risk_config=self._base.risk_config,
            risk_capital=(self._base.risk_config.capital if self._base.risk_config else None),
            notifications=notes, data_ok=data_ok, autonomous=autonomous,
            global_market_data=_global_market_data(md),
        )
        return snap.as_dict()

    def autonomous(self, action, payload=None):
        eng = self._base.autonomous_engine
        if eng is None:
            return {"detail": "autonomous engine not available"}
        if action == "start":
            c = self._worker.cache
            return eng.start(confirm=(payload or {}).get("confirm"), connected=bool(c["connected"]),
                             market_data=c["market_data"], risk_config=self._base.risk_config)
        return self._base.autonomous(action, payload)

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
    capital = loaded.capital if loaded is not None else float(os.environ.get("DASHBOARD_CAPITAL", "1000000"))
    risk.state.day_start_equity = capital
    risk.state.peak_equity = capital

    # PAPER AUTONOMOUS engine — SHARES the authoritative Risk Engine; default DISABLED (nothing
    # runs or trades until the user explicitly ARMs + STARTs via the token-gated endpoints).
    from atp.journal import InMemoryJournal  # noqa: PLC0415
    from atp.policy import TradingPolicy  # noqa: PLC0415
    from atp.strategy import BreakoutStrategy, MeanReversionStrategy, MomentumStrategy  # noqa: PLC0415
    journal = InMemoryJournal()
    desk, paper_broker, _ = asyncio.run(build_paper_stack(
        policy=TradingPolicy(capital=capital),
        strategies=[MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()],
        journal=journal, risk=risk))       # shared Risk Engine
    base.autonomous_engine = PaperAutonomousEngine(desk=desk, broker=paper_broker, risk=risk, journal=journal)

    worker = IBKRWorker(host=os.environ.get("IBKR_HOST", "127.0.0.1"),
                        port=int(os.environ.get("IBKR_PORT", "4002")),
                        client_id=int(os.environ.get("IBKR_CLIENT_ID", "20")),
                        interval=float(os.environ.get("DASHBOARD_MD_INTERVAL", "30")),
                        engine=base.autonomous_engine)
    worker.start()

    app = create_app(IBKRContext(base, worker))
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")  # localhost only
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    print(f"IBKR read-only dashboard backend on http://{host}:{port}  (paper · read-only · no execution)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
