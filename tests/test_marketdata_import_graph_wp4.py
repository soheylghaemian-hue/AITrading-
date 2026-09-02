"""§ WP4 import-graph guard — STRUCTURAL proof the persistent market-data foundation reaches no live-trading
capability. Importing the WP4 market-data modules must NOT pull the legacy backtester, brokers, the
execution engine, autonomous/live paths, runtime order/position paths, or any IBKR SDK into sys.modules.
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

# the market-data foundation must not perform network I/O at import — the real Massive/Polygon provider
# defers its network library behind runtime calls (and is disabled without MASSIVE_API_KEY), so importing
# the pipeline (incl. ingest) loads NO HTTP/WS client.
NETWORK = ("urllib.request", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets")

WP4_MODULES = [
    "atp.marketdata.model",
    "atp.marketdata.provider_base",
    "atp.marketdata.ingest",
]


def _targeted(name: str) -> bool:
    # purge atp.* AND the network modules, so the loaded-set reflects what WP4's import ITSELF pulls
    # (network modules may be pre-loaded by other tests, so a bare presence check would be a false positive)
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


def test_wp4_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP4_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP4 market-data import graph reached prohibited modules: {sorted(set(hits))}"


def test_wp4_imports_no_network_client():
    # the real Massive/Polygon provider's network library is deferred inside runtime methods, so importing
    # the WP4 market-data pipeline (incl. ingest) must pull NO HTTP/WS client at load.
    loaded = _fresh_import(WP4_MODULES)
    hits = [n for n in NETWORK if n in loaded]
    assert not hits, f"WP4 market-data import graph pulled a network client: {sorted(hits)}"


def test_provider_base_defines_no_trading_methods():
    from atp.marketdata.provider_base import MarketDataProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "create_order", "buy", "sell",
                 "positions", "account", "withdraw", "deposit", "reqMktData"}
    assert forbidden.isdisjoint(set(dir(MarketDataProvider)))
