"""Notification watchdog — the pure change-detection core (no network, no IBKR)."""

import importlib.util
from pathlib import Path

from atp.dashboard.notifications import Kind, Severity

_PATH = Path(__file__).resolve().parents[1] / "examples" / "notify_watchdog.py"
_spec = importlib.util.spec_from_file_location("notify_watchdog_undertest", _PATH)
_wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wd)
status_events = _wd.status_events


def test_no_change_no_events():
    st = {"gateway": True, "market": {"AAPL": False}}
    assert status_events(st, st) == []


def test_first_cycle_is_silent_baseline():
    # empty prev → no events (we only alert on a *change* from a known previous state)
    assert status_events({}, {"gateway": True, "market": {"AAPL": True}}) == []


def test_gateway_down_is_critical():
    ev = status_events({"gateway": True}, {"gateway": False})
    assert len(ev) == 1
    kind, sev, msg = ev[0]
    assert kind is Kind.BROKER_DISCONNECT and sev is Severity.CRITICAL


def test_gateway_back_up():
    ev = status_events({"gateway": False}, {"gateway": True})
    assert ev and ev[0][0] is Kind.SYSTEM_ERROR


def test_market_data_becomes_available():
    ev = status_events({"gateway": True, "market": {"AAPL": False}},
                       {"gateway": True, "market": {"AAPL": True}})
    assert len(ev) == 1
    kind, sev, msg = ev[0]
    assert kind is Kind.DATA_FEED and "AVAILABLE" in msg and "AAPL" in msg


def test_market_data_lost():
    ev = status_events({"gateway": True, "market": {"EUR.USD": True}},
                       {"gateway": True, "market": {"EUR.USD": False}})
    assert ev and "lost" in ev[0][2].lower()


def test_multiple_changes_reported_together():
    ev = status_events({"gateway": True, "market": {"AAPL": False, "SPY": True}},
                       {"gateway": False, "market": {"AAPL": True, "SPY": True}})
    kinds = {e[0] for e in ev}
    assert Kind.BROKER_DISCONNECT in kinds and Kind.DATA_FEED in kinds
    assert len(ev) == 2  # gateway down + AAPL available; SPY unchanged
