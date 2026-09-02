"""§ WP7 import-graph / safety guard — STRUCTURAL proof the fundamentals subsystem reaches no live-trading
capability and defines no network fetch or HTTP write path at import. Importing the WP7 modules must NOT pull
the legacy backtester, brokers, the execution engine, autonomous/live paths, runtime order/position paths,
any IBKR SDK, or an HTTP client. (Importing the WP5 newsroom library it reuses is expected and allowed.)
Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

import importlib
import sys

PROHIBITED = (
    "atp.backtest",
    "atp.brokers",
    "atp.execution",
    "atp.autonomous",
    "atp.live",
    "atp.runtime.orders",
    "atp.runtime.positions",
    "ib_insync",
    "ibapi",
    "ib_async",
)

NETWORK = ("urllib.request", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets")

WP7_MODULES = [
    "atp.fundseries.model",
    "atp.fundseries.provider",
    "atp.fundseries.ingest",
    "atp.fundseries.registry",
    "atp.fundseries.readmodel",
]


def _targeted(name: str) -> bool:
    return name == "atp" or name.startswith("atp.") or name in NETWORK


def _fresh_import(modules):
    saved = {k: v for k, v in sys.modules.items() if _targeted(k)}

    def _purge():
        for name in list(sys.modules):
            if _targeted(name):
                del sys.modules[name]

    _purge()
    try:
        for m in modules:
            importlib.import_module(m)
        return set(sys.modules)
    finally:
        _purge()
        sys.modules.update(saved)


def test_wp7_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP7_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP7 fundamentals import graph reached prohibited modules: {sorted(set(hits))}"


def test_wp7_imports_no_network_client():
    loaded = _fresh_import(WP7_MODULES)
    hits = [n for n in NETWORK if n in loaded]
    assert not hits, f"WP7 fundamentals import graph pulled a network client: {sorted(hits)}"


def test_wp7_builds_only_on_newsroom_and_stdlib():
    loaded = _fresh_import(WP7_MODULES)
    atp_pkgs = {name.split(".")[1] for name in loaded if name.startswith("atp.") and name.count(".") >= 1}
    assert atp_pkgs <= {"fundseries", "newsroom"}, f"unexpected atp packages reached: {sorted(atp_pkgs)}"


def test_provider_defines_no_trading_and_fundamentals_defines_no_http_write():
    import pathlib

    import atp.fundseries as pkg
    from atp.fundseries.provider import FundamentalProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account"}
    assert forbidden.isdisjoint(set(dir(FundamentalProvider)))
    pkg_dir = pathlib.Path(pkg.__file__).parent
    http_tokens = ("@app.post", "@app.put", "@app.delete", "@app.patch",
                   "@router.post", "@router.put", "@router.delete", "@router.patch",
                   "FastAPI(", "APIRouter(", "Flask(", "Blueprint(", "web.post", "web.put", "web.delete")
    for py in pkg_dir.glob("*.py"):
        text = py.read_text()
        assert not any(tok in text for tok in http_tokens), f"{py.name} defines an HTTP write path"
