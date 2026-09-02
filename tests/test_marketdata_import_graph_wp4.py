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

WP4_MODULES = [
    "atp.marketdata.model",
    "atp.marketdata.provider_base",
    "atp.marketdata.ingest",
]


def _fresh_import(modules):
    saved = {k: v for k, v in sys.modules.items() if k == "atp" or k.startswith("atp.")}

    def _purge():
        for name in list(sys.modules):
            if name == "atp" or name.startswith("atp."):
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


def test_provider_base_defines_no_trading_methods():
    from atp.marketdata.provider_base import MarketDataProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "create_order", "buy", "sell",
                 "positions", "account", "withdraw", "deposit", "reqMktData"}
    assert forbidden.isdisjoint(set(dir(MarketDataProvider)))
