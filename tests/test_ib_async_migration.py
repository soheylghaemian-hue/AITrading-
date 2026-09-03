"""§ Repository-wide ib_insync → ib_async migration guard (deterministic, OFFLINE, no broker SDK).

Proves every production IBKR SDK binding is `ib_async` (installed), that no production module can fail on a
missing `ib_insync`, that the contract-discovery path stays read-only, and that importing the migrated
modules pulls no broker SDK. All tests run with neither ib_async nor ib_insync installed.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import types
from types import SimpleNamespace

_PROD_SDK = ("ib_async", "ib_insync", "ibapi")


def _src_root() -> pathlib.Path:
    import atp
    return pathlib.Path(atp.__file__).parent


# --------------------------------------------------------------------- no ib_insync anywhere in production
def test_no_ib_insync_reference_remains_in_production_src():
    hits = []
    for py in _src_root().rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "ib_insync" in line:
                hits.append(f"{py.relative_to(_src_root())}:{i}")
    assert not hits, f"ib_insync still referenced in production src (migrate to ib_async): {hits}"


def test_pyproject_declares_ib_async_not_ib_insync():
    root = _src_root().parent.parent            # …/AITrading_main
    txt = (root / "pyproject.toml").read_text()
    assert "ib-async" in txt and "ib-insync" not in txt


# --------------------------------------------------------------------- migrated modules import SDK-free
def _fresh_import(modules):
    saved = {k: v for k, v in sys.modules.items()
             if k == "atp" or k.startswith("atp.") or k in _PROD_SDK}

    def _purge():
        for name in list(sys.modules):
            if name == "atp" or name.startswith("atp.") or name in _PROD_SDK:
                del sys.modules[name]

    _purge()
    try:
        for m in modules:
            importlib.import_module(m)
        return set(sys.modules)
    finally:
        _purge()
        sys.modules.update(saved)


def test_migrated_modules_pull_no_broker_sdk_at_import():
    loaded = _fresh_import([
        "atp.instruments.ibkr_catalog", "atp.instruments.qualification",
        "atp.brokers.ibkr", "atp.live.feed",
    ])
    hits = [m for m in _PROD_SDK if m in loaded or any(x.startswith(m + ".") for x in loaded)]
    assert not hits, f"a migrated module pulled a broker SDK at import (must be lazy): {sorted(hits)}"


# --------------------------------------------------------------------- ibkr_catalog binds to ib_async
def test_ibkr_catalog_build_contract_uses_ib_async(monkeypatch):
    captured: dict = {}

    class Contract:
        def __init__(self, **kw):
            captured.clear(); captured.update(kw)

    fake = types.ModuleType("ib_async")
    fake.Contract = Contract
    monkeypatch.setitem(sys.modules, "ib_async", fake)

    from atp.instruments.ibkr_catalog import _ib_contract
    _ib_contract(SimpleNamespace(symbol="AAPL", sec_type="STK", exchange="NASDAQ", currency="USD"))
    assert captured["symbol"] == "AAPL" and captured["secType"] == "STK"
    assert captured["exchange"] == "SMART" and captured["primaryExchange"] == "NASDAQ"  # STK routing preserved
    assert captured["currency"] == "USD"
    assert "ib_insync" not in sys.modules

    # ETF collapses to STK (unchanged behaviour), still via ib_async
    _ib_contract(SimpleNamespace(symbol="SPY", sec_type="ETF", exchange="ARCA", currency="USD"))
    assert captured["secType"] == "STK" and captured["exchange"] == "SMART"


# --------------------------------------------------------------------- contract-discovery stays read-only
def test_contract_discovery_paths_are_read_only():
    for mod in ("atp.instruments.ibkr_catalog", "atp.instruments.qualification"):
        src = pathlib.Path(importlib.import_module(mod).__file__).read_text()
        for forbidden in ("reqMktData", "placeOrder", "cancelOrder", "reqScannerSubscription",
                          "reqRealTimeBars", "reqTickByTick", "reqHistoricalData", "reqMktDepth"):
            assert forbidden not in src, f"{mod} must stay read-only — found {forbidden}"
        assert "reqContractDetailsAsync" in src   # the only IBKR call these make
