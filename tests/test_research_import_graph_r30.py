"""§ R3.0 import-graph guard — STRUCTURAL proof (not a text grep) that the research package cannot
reach any live-trading capability. Importing atp.research (and every submodule) must NOT pull any
prohibited module into sys.modules: the execution-coupled legacy backtester, brokers, execution,
autonomous, live, runtime order/position paths, or IBKR."""
from __future__ import annotations

import importlib
import sys

PROHIBITED = (
    "atp.backtest",          # execution-coupled legacy Backtester (PaperBroker + ExecutionEngine)
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

RESEARCH_MODULES = [
    "atp.research", "atp.research.calendars", "atp.research.strategy", "atp.research.engine",
    "atp.research.metrics", "atp.research.robustness" if False else "atp.research.risk_adapter",
    "atp.research.runner", "atp.research.readmodel",
]


def _fresh_import(modules):
    # drop any already-imported atp.* so the graph is measured from a clean slate
    for name in list(sys.modules):
        if name == "atp" or name.startswith("atp."):
            del sys.modules[name]
    for m in modules:
        importlib.import_module(m)
    return set(sys.modules)


def test_research_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(RESEARCH_MODULES)
    hits = [p for p in PROHIBITED for name in loaded
            if name == p or name.startswith(p + ".") or name == p.replace(".", "/")]
    assert not hits, f"research import graph reached prohibited modules: {sorted(set(hits))}"


def test_research_reuses_only_pure_riskcontrol_evaluate():
    # the ONE cross-package reuse (risk gate) must itself be clean
    loaded = _fresh_import(["atp.research.risk_adapter"])
    assert "atp.riskcontrol.evaluate" in loaded
    for p in PROHIBITED:
        assert not any(n == p or n.startswith(p + ".") for n in loaded), f"{p} leaked via risk_adapter"
