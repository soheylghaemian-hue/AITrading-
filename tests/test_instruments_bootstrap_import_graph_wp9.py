"""§ WP9 import-graph / safety guard — STRUCTURAL proof the instrument-bootstrap subsystem reaches no
live-trading capability and pulls no HTTP/WS client at import. Importing the WP9 directory/registry/coverage
modules must NOT pull the legacy backtester, brokers, the execution engine, autonomous/live paths, runtime
order/position paths, any IBKR SDK, or a network client. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED ·
IBKR ORDERS=0.
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

# the directory adapters must not perform network I/O at import — the real download is a separate, disabled
# concern (fail-closed source registry). Parsing is offline; no HTTP/WS client is imported at load.
NETWORK = ("urllib.request", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets")

WP9_MODULES = [
    "atp.instruments.sources",
    "atp.instruments.directories",
    "atp.instruments.coverage",
]


def _targeted(name: str) -> bool:
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


def test_wp9_import_graph_has_no_prohibited_modules():
    loaded = _fresh_import(WP9_MODULES)
    hits = [p for p in PROHIBITED for name in loaded if name == p or name.startswith(p + ".")]
    assert not hits, f"WP9 instrument-bootstrap import graph reached prohibited modules: {sorted(set(hits))}"


def test_wp9_imports_no_network_client():
    loaded = _fresh_import(WP9_MODULES)
    hits = [n for n in NETWORK if n in loaded]
    assert not hits, f"WP9 instrument-bootstrap import graph pulled a network client: {sorted(hits)}"


def test_directory_provider_defines_no_trading_and_no_http_write():
    import pathlib

    import atp.instruments as pkg
    from atp.instruments.directories import DirectoryProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "buy", "sell", "positions", "account",
                 "subscribe", "reqMktData", "download", "fetch_url"}
    assert forbidden.isdisjoint(set(dir(DirectoryProvider)))
    # the WP9 modules define no HTTP route / write endpoint / download
    pkg_dir = pathlib.Path(pkg.__file__).parent
    http_tokens = ("@app.post", "@app.put", "@app.delete", "@app.patch", "FastAPI(", "APIRouter(",
                   "Flask(", "urlopen(", "urlretrieve(", "requests.get(", "httpx.")
    for name in ("sources.py", "directories.py", "coverage.py", "bootstrap.py"):
        text = (pkg_dir / name).read_text()
        assert not any(tok in text for tok in http_tokens), f"{name} defines a network/HTTP-write path"
