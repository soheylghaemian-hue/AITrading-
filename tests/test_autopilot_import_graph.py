"""Structural safety proof: development and brain research cannot import trading authority."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

MODULES = ("atp.autopilot", "atp.autopilot.orchestrator", "atp.autopilot.providers",
           "atp.autopilot.verifier", "atp.brain", "atp.brain.contracts", "atp.brain.learn",
           "atp.brain.prove", "atp.brain.sense", "atp.brain.think")
PROHIBITED = ("atp.brokers", "atp.execution", "atp.live", "atp.risk", "atp.runtime", "atp.services")
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _is_prohibited(name: str) -> bool:
    """Exact module identity or a genuine submodule, never a substring match."""
    return any(name == prohibited or name.startswith(prohibited + ".") for prohibited in PROHIBITED)


def test_import_graph_has_no_trading_authority():
    # Isolate the source-graph inspection in a subprocess. Deleting modules inside the pytest
    # process can create duplicate Enum classes in tests collected earlier (identity then differs),
    # and restoring sys.modules by hand leaves the ordering of unrelated tests load-bearing.
    code = ("import importlib,json,sys; "
            f"[importlib.import_module(m) for m in {MODULES!r}]; "
            "print(json.dumps(sorted(sys.modules)))")
    env = dict(os.environ)
    source_root = str(SOURCE_ROOT)
    env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run((sys.executable, "-c", code), capture_output=True, text=True, check=True, env=env)
    loaded = set(json.loads(proc.stdout))
    # The transitive graph that is actually loaded, not just the graph that is declared.
    assert not [name for name in loaded if _is_prohibited(name)]


def _module_name(source: Path) -> tuple[str, bool]:
    parts = list(source.relative_to(SOURCE_ROOT).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _declared_targets(source: Path) -> set[str]:
    """Every module a file names, with relative imports resolved to absolute dotted paths.

    Deferred imports inside a function body are walked exactly like top-level ones, and each
    ``from X import a, b`` also contributes ``X.a`` and ``X.b`` so a submodule imported by name is
    not hidden behind its package.
    """
    module_name, is_package = _module_name(source)
    package = module_name if is_package else module_name.rpartition(".")[0]
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                bits = package.split(".")
                if node.level > 1:
                    bits = bits[: -(node.level - 1)]
                base = ".".join(bits)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
            if not base:
                continue
            targets.add(base)
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def test_declared_imports_resolve_and_name_no_trading_authority():
    packages = (SOURCE_ROOT / "atp" / "autopilot", SOURCE_ROOT / "atp" / "brain")
    inspected = 0
    for package in packages:
        for source in sorted(package.rglob("*.py")):
            inspected += 1
            offending = sorted(name for name in _declared_targets(source) if _is_prohibited(name))
            assert not offending, f"{source} declares {offending}"
    assert inspected, "the static import proof inspected no source at all"


def test_brain_sources_use_no_dynamic_import_or_code_execution_escape_hatch():
    forbidden = {"__import__", "eval", "exec"}
    for source in sorted((SOURCE_ROOT / "atp" / "brain").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in forbidden:
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                calls.append("import_module")
        assert not calls, f"{source} uses dynamic authority escape hatches: {calls}"


def test_relative_import_resolution_is_exercised():
    """The resolver must actually turn a relative import into an absolute path.

    Without this, a resolver that collapsed every relative import to "." would silently allow a
    deferred ``from ..runtime import ...`` to pass the declared-import proof.
    """
    targets = _declared_targets(SOURCE_ROOT / "atp" / "brain" / "learn.py")
    assert "atp.brain.sense" in targets
    assert "atp.brain.sense.SenseResult" in targets
    assert not any(name.startswith(".") for name in targets)


def test_sources_contain_no_order_calls():
    root = SOURCE_ROOT / "atp"
    forbidden = ("placeOrder(", ".place_order(", "submitOrder(", "ExecutionEngine(", "IBKRBroker(")
    for package in (root / "autopilot", root / "brain"):
        for source in package.rglob("*.py"):
            for token in forbidden:
                assert token not in source.read_text(), f"{source} references {token}"


def test_learn_results_declare_no_transition_or_execution_capability():
    """Inspect the names each result class *declares*, never inherited object metadata.

    ``dir()`` would report ``__sizeof__`` and match a naive "size" substring scan, so exact declared
    names are compared against exact forbidden names instead.
    """
    from atp.brain import learn

    forbidden = {"place_order", "submit_order", "order", "execute", "execution", "size",
                 "size_position", "allocate", "allocation", "deploy", "promote", "promotion",
                 "retire", "reinstate", "relax_limit", "leverage", "risk_limit"}
    classes = (learn.DriftResult, learn.ComparisonResult, learn.RetirementResult,
               learn.ReinstatementResult, learn.ProofSummary, learn.ModelRecord)
    for cls in classes:
        declared = set(vars(cls))
        overlap = sorted(declared & forbidden)
        assert not overlap, f"{cls.__name__} declares {overlap}"


def test_learn_module_holds_no_origin_authentication_state():
    """Pure Python values use recomputation, never a writable ledger or hidden minting token."""
    from atp.brain import learn

    forbidden = {"_Provenance", "_ADMITTED", "_EVALUATOR_CODES", "_require_issued",
                 "issued_by_evaluate_sense"}
    assert not (set(vars(learn)) & forbidden)
