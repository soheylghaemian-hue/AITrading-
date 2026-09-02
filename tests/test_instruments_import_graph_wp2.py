"""§ WP2 import-graph guard — STRUCTURAL proof that the persistent instrument model + importer cannot reach
any live-trading / order-submission capability. Importing the WP2 modules must NOT pull any prohibited
module into sys.modules: the execution-coupled legacy backtester, brokers (incl. the paper broker), the
execution engine, autonomous/live paths, runtime order/position paths, or any IBKR client. This proves the
foundation stays reference-data-only. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
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

WP2_MODULES = [
    "atp.instruments.model",
    "atp.instruments.importer",
]


def _fresh_import(modules):
    """Import ``modules`` into a pristine atp.* module space and return the resulting sys.modules set, then
    RESTORE the original atp.* modules so this structural probe never corrupts module identity for other
    tests (a bare blow-away would leave later monkeypatches of atp.store.schema pointing at a stale copy)."""
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


def test_wp2_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP2_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP2 import graph reached prohibited modules: {sorted(set(hits))}"


def test_importer_does_not_import_ibkr_client_transitively():
    """The importer maps public listing reference data only — it must never pull an IBKR SDK, even though the
    sibling `ibkr_catalog` exists in the same package (that module imports ib_insync lazily)."""
    loaded = _fresh_import(["atp.instruments.importer"])
    for sdk in ("ib_insync", "ibapi", "ib_async"):
        assert not any(n == sdk or n.startswith(sdk + ".") for n in loaded), f"{sdk} leaked via importer"
