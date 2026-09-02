"""§ WP6 import-graph / safety guard — STRUCTURAL proof the macro/geopolitical subsystem reaches no
live-trading capability and defines no network fetch or HTTP write path at import. Importing the WP6 modules
must NOT pull the legacy backtester, brokers, the execution engine, autonomous/live paths, runtime
order/position paths, any IBKR SDK, or an HTTP client. (Importing the WP5 newsroom library it builds on is
expected and allowed.) Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
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

# the macro subsystem must not perform network I/O — no HTTP client is imported at load
NETWORK = ("urllib.request", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets")

WP6_MODULES = [
    "atp.macroevents.model",
    "atp.macroevents.provider",
    "atp.macroevents.ingest",
    "atp.macroevents.registry",
    "atp.macroevents.readmodel",
]


def _targeted(name: str) -> bool:
    # purge atp.* AND the network modules, so the loaded-set reflects what WP6's import ITSELF pulls
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


def test_wp6_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP6_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP6 macro import graph reached prohibited modules: {sorted(set(hits))}"


def test_wp6_imports_no_network_client():
    loaded = _fresh_import(WP6_MODULES)
    hits = [n for n in NETWORK if n in loaded]
    assert not hits, f"WP6 macro import graph pulled a network client: {sorted(hits)}"


def test_wp6_builds_only_on_newsroom_and_stdlib():
    # the only atp.* packages WP6 may reach are its own, the WP5 newsroom it overlays, and the store row types
    loaded = _fresh_import(WP6_MODULES)
    atp_pkgs = {name.split(".")[1] for name in loaded if name.startswith("atp.") and name.count(".") >= 1}
    assert atp_pkgs <= {"macroevents", "newsroom"}, f"unexpected atp packages reached: {sorted(atp_pkgs)}"


def test_provider_defines_no_trading_and_macroevents_defines_no_http_write():
    import pathlib

    import atp.macroevents as pkg
    from atp.macroevents.provider import MacroEventProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account"}
    assert forbidden.isdisjoint(set(dir(MacroEventProvider)))
    # the whole macroevents package is a library — it defines no HTTP route / write endpoint / app
    pkg_dir = pathlib.Path(pkg.__file__).parent
    http_tokens = ("@app.post", "@app.put", "@app.delete", "@app.patch",
                   "@router.post", "@router.put", "@router.delete", "@router.patch",
                   "FastAPI(", "APIRouter(", "Flask(", "Blueprint(", "web.post", "web.put", "web.delete")
    for py in pkg_dir.glob("*.py"):
        text = py.read_text()
        assert not any(tok in text for tok in http_tokens), f"{py.name} defines an HTTP write path"
