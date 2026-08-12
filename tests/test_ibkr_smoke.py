"""Safety tests for the READ-ONLY IBKR smoke script (examples/ibkr_smoke.py).

These prove the *standard* smoke path is provably read-only: it issues no order-management
request (`open_orders` → `reqOpenOrders`) and never places/cancels an order, while the session
stays `readonly=True`. Reading open orders is opt-in behind `--check-open-orders`.

The script is loaded by file path (examples/ is not a package) and its `IBKRBroker` symbol is
replaced with a recording fake, so nothing touches a real gateway.
"""

import argparse
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SMOKE_PATH = Path(__file__).resolve().parents[1] / "examples" / "ibkr_smoke.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("ibkr_smoke_undertest", _SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RecordingBroker:
    """Stands in for IBKRBroker; records every method the smoke script calls."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[str] = []
        self.placed: list = []
        self.cancelled: list = []

    async def connect(self):
        self.calls.append("connect")

    def is_connected(self):
        return True

    async def get_account(self):
        self.calls.append("get_account")
        return SimpleNamespace(equity=100000.0, cash=100000.0, realized_pnl=0.0,
                               unrealized_pnl=0.0, gross_exposure=0.0, gross_leverage=0.0)

    async def get_positions(self):
        self.calls.append("get_positions")
        return {}

    async def open_orders(self):
        self.calls.append("open_orders")
        return []

    async def place_order(self, order):
        self.calls.append("place_order")
        self.placed.append(order)
        return SimpleNamespace(status="REJECTED")

    async def cancel_order(self, *a, **k):
        self.calls.append("cancel_order")
        self.cancelled.append((a, k))

    def _require(self):
        # market-data path reaches the raw client; symbols="" keeps the loop empty.
        return SimpleNamespace()

    _factory = SimpleNamespace(contract=lambda inst: SimpleNamespace())

    async def disconnect(self):
        self.calls.append("disconnect")


def _run(check_open_orders: bool):
    mod = _load_smoke()
    captured = {}

    def _fake_broker_ctor(cfg):
        b = RecordingBroker(cfg)
        captured["broker"] = b
        return b

    mod.IBKRBroker = _fake_broker_ctor          # replace the real broker
    mod._preflight = lambda: True               # skip the ib_insync import gate

    args = argparse.Namespace(host="127.0.0.1", port=4002, client_id=7, account=None,
                              symbols="", check_open_orders=check_open_orders)
    rc = asyncio.run(mod.main(args))
    return rc, captured["broker"]


# --------------------------------------------------------------------------- default path
def test_default_smoke_path_never_reads_open_orders():
    rc, broker = _run(check_open_orders=False)
    assert rc == 0
    assert "open_orders" not in broker.calls           # no reqOpenOrders (order-management)
    assert "connect" in broker.calls                   # but it did connect + read
    assert "get_account" in broker.calls
    assert "get_positions" in broker.calls


def test_default_smoke_path_never_places_or_cancels():
    _, broker = _run(check_open_orders=False)
    assert "place_order" not in broker.calls and not broker.placed
    assert "cancel_order" not in broker.calls and not broker.cancelled


def test_default_smoke_session_is_readonly():
    _, broker = _run(check_open_orders=False)
    assert broker.cfg.readonly is True                 # hard read-only session preserved


# --------------------------------------------------------------------------- opt-in path
def test_open_orders_only_when_flag_set():
    _, broker = _run(check_open_orders=True)
    assert "open_orders" in broker.calls               # explicit opt-in reads open orders
    # even opted-in, it still never writes:
    assert "place_order" not in broker.calls and "cancel_order" not in broker.calls


# --------------------------------------------------------------------------- surface guards
def test_smoke_source_has_no_order_write_calls():
    src = _SMOKE_PATH.read_text()
    for forbidden in ("placeOrder", "cancelOrder", "modifyOrder",
                      "reqGlobalCancel", "reqAutoOpenOrders", "place_order", "cancel_order"):
        assert forbidden not in src, f"{forbidden} must not appear in the smoke script"


def test_smoke_source_gates_open_orders_behind_flag():
    src = _SMOKE_PATH.read_text()
    assert "--check-open-orders" in src
    assert "check_open_orders" in src
    assert "readonly=True" in src
