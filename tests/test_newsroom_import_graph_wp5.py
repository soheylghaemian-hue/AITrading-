"""§ WP5 import-graph / safety guard — STRUCTURAL proof the news/filings subsystem reaches no live-trading
capability and defines no network fetch or HTTP write path at import. Importing the WP5 modules must NOT
pull the legacy backtester, brokers, the execution engine, autonomous/live paths, runtime order/position
paths, any IBKR SDK, or an HTTP client. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
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

# the news subsystem must not perform network I/O — no HTTP client is imported at load
NETWORK = ("urllib.request", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets")

WP5_MODULES = [
    "atp.newsroom.model",
    "atp.newsroom.provider",
    "atp.newsroom.ingest",
    "atp.newsroom.registry",
    "atp.newsroom.readmodel",
]


def _targeted(name: str) -> bool:
    # purge atp.* AND the network modules, so the loaded-set reflects what WP5's import ITSELF pulls
    # (network modules are pre-loaded by other tests, so a bare presence check would be a false positive)
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


def test_wp5_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP5_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP5 news import graph reached prohibited modules: {sorted(set(hits))}"


def test_wp5_imports_no_network_client():
    loaded = _fresh_import(WP5_MODULES)
    hits = [n for n in NETWORK if n in loaded]
    assert not hits, f"WP5 news import graph pulled a network client: {sorted(hits)}"


def test_provider_defines_no_trading_and_newsroom_defines_no_http_write():
    import pathlib

    import atp.newsroom as pkg
    from atp.newsroom.provider import NewsProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account"}
    assert forbidden.isdisjoint(set(dir(NewsProvider)))
    # the whole newsroom package is a library — it defines no HTTP route / write endpoint / app
    pkg_dir = pathlib.Path(pkg.__file__).parent
    http_tokens = ("@app.post", "@app.put", "@app.delete", "@app.patch",
                   "@router.post", "@router.put", "@router.delete", "@router.patch",
                   "FastAPI(", "APIRouter(", "Flask(", "Blueprint(", "web.post", "web.put", "web.delete")
    for py in pkg_dir.glob("*.py"):
        text = py.read_text()
        assert not any(tok in text for tok in http_tokens), f"{py.name} defines an HTTP write path"
