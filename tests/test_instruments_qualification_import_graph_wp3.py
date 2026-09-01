"""§ WP3 import-graph guard — STRUCTURAL proof that the read-only IBKR qualification module reaches no
live-trading capability. Importing `atp.instruments.qualification` must NOT pull the legacy backtester,
brokers, the execution engine, autonomous/live paths, runtime order/position paths, or any IBKR SDK into
sys.modules (`ib_insync` is imported lazily, only inside the client adapter's contract-build path).
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

WP3_MODULES = ["atp.instruments.qualification"]


def _fresh_import(modules):
    """Import into a pristine atp.* space, return the sys.modules set, then RESTORE the original atp.*
    modules so this probe never corrupts module identity for other tests."""
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


def test_wp3_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP3_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP3 qualification import graph reached prohibited modules: {sorted(set(hits))}"


def test_no_ibkr_sdk_at_import_time():
    loaded = _fresh_import(WP3_MODULES)
    for sdk in ("ib_insync", "ibapi", "ib_async"):
        assert not any(n == sdk or n.startswith(sdk + ".") for n in loaded), f"{sdk} leaked at import time"
