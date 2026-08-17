"""§ R3.0A import-graph guard — STRUCTURAL proof (not a text grep) that the historical-backfill package
cannot reach any live-trading / order-submission capability. Importing atp.research.backfill (and every
submodule) must NOT pull any prohibited module into sys.modules: the execution-coupled legacy backtester,
brokers (incl. the paper broker = F2 durable paper execution), the execution engine, autonomous, live,
runtime order/position paths, or any IBKR client. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED ·
IBKR ORDERS=0."""
from __future__ import annotations

import importlib
import sys

PROHIBITED = (
    "atp.backtest",          # execution-coupled legacy Backtester (PaperBroker + ExecutionEngine)
    "atp.brokers",           # incl. atp.brokers.paper (F2 durable paper execution) and atp.brokers.ibkr
    "atp.execution",         # order execution engine / algos / scheduler
    "atp.autonomous",
    "atp.live",
    "atp.runtime.orders",
    "atp.runtime.positions",
    "ib_insync",
    "ibapi",
    "ib_async",
)

BACKFILL_MODULES = [
    "atp.research.backfill",
    "atp.research.backfill.normalize",
    "atp.research.backfill.provider",
    "atp.research.backfill.dataset",
    "atp.research.backfill.validate",
    "atp.research.backfill.runner",
    "atp.research.backfill.readmodel",
    "atp.research.backfill.select",
]


def _fresh_import(modules):
    for name in list(sys.modules):
        if name == "atp" or name.startswith("atp."):
            del sys.modules[name]
    for m in modules:
        importlib.import_module(m)
    return set(sys.modules)


def test_backfill_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(BACKFILL_MODULES)
    hits = [p for p in PROHIBITED for name in loaded
            if name == p or name.startswith(p + ".")]
    assert not hits, f"backfill import graph reached prohibited modules: {sorted(set(hits))}"


def test_backfill_provider_uses_only_stdlib_http():
    """The real provider client must use stdlib urllib (auditable, no hidden SDK) and reach nothing
    execution-related when imported on its own."""
    loaded = _fresh_import(["atp.research.backfill.provider"])
    assert "urllib.request" in loaded
    for p in PROHIBITED:
        assert not any(n == p or n.startswith(p + ".") for n in loaded), f"{p} leaked via provider"
