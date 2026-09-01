"""§ R3.1A import-graph + source guard — STRUCTURAL proof that the intel-collection and validation packages
cannot reach any live-trading / order / kill-switch capability. Importing every submodule must NOT pull a
prohibited module into sys.modules, and the sources must contain no execution-call identifiers."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

PROHIBITED = (
    "atp.backtest", "atp.brokers", "atp.execution", "atp.autonomous", "atp.live",
    "atp.runtime.orders", "atp.runtime.positions", "ib_insync", "ibapi", "ib_async",
)

MODULES = [
    "atp.research.intel", "atp.research.intel.policy", "atp.research.intel.commit",
    "atp.research.intel.envelope", "atp.research.intel.provenance", "atp.research.intel.collector",
    "atp.research.intel.outcomes", "atp.research.intel.worker", "atp.research.intel.legacy_diag",
    "atp.research.intel.dsn", "atp.research.intel.readmodel",
    "atp.research.validation", "atp.research.validation.metrics", "atp.research.validation.benchmarks",
    "atp.research.validation.calibration", "atp.research.validation.runner",
    "atp.research.validation.readmodel", "atp.research.validation.worker",
]


def _fresh_import(modules):
    for name in list(sys.modules):
        if name == "atp" or name.startswith("atp."):
            del sys.modules[name]
    for m in modules:
        importlib.import_module(m)
    return set(sys.modules)


def test_intel_validation_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"R3.1A import graph reached prohibited modules: {sorted(set(hits))}"


def test_intel_validation_sources_have_no_execution_or_kill_switch_identifiers():
    roots = [Path(__file__).resolve().parents[1] / "src" / "atp" / "research" / "intel",
             Path(__file__).resolve().parents[1] / "src" / "atp" / "research" / "validation"]
    forbidden = ("placeOrder(", "submitOrder(", "createOrder(", "executeTrade(", ".place_order(",
                 "set_kill_switch(", "kill_switch(", "arm(", "PaperBroker(", "ExecutionEngine(",
                 "ib_async", "ibapi", "reqMktData")
    for root in roots:
        for f in root.rglob("*.py"):
            text = f.read_text()
            for tok in forbidden:
                assert tok not in text, f"{f.name} must not reference {tok}"
