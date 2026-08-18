"""Structural safety proof: development and brain research cannot import trading authority."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MODULES = ("atp.autopilot", "atp.autopilot.orchestrator", "atp.autopilot.providers",
           "atp.autopilot.verifier", "atp.brain", "atp.brain.contracts", "atp.brain.sense")
PROHIBITED = ("atp.brokers", "atp.execution", "atp.live", "atp.risk", "atp.runtime", "atp.services")


def test_import_graph_has_no_trading_authority():
    # Isolate the source-graph inspection. Deleting modules inside the pytest process can
    # create duplicate Enum classes in tests collected earlier (identity then differs).
    code = ("import importlib,json,sys; "
            f"[importlib.import_module(m) for m in {MODULES!r}]; "
            "print(json.dumps(sorted(sys.modules)))")
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run((sys.executable, "-c", code), capture_output=True, text=True, check=True, env=env)
    loaded = set(json.loads(proc.stdout))
    assert not [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]


def test_sources_contain_no_order_calls():
    root = Path(__file__).resolve().parents[1] / "src" / "atp"
    forbidden = ("placeOrder(", ".place_order(", "submitOrder(", "ExecutionEngine(", "IBKRBroker(")
    for package in (root / "autopilot", root / "brain"):
        for source in package.rglob("*.py"):
            for token in forbidden:
                assert token not in source.read_text(), f"{source} references {token}"
