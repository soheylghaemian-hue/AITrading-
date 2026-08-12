"""Phase 2A read-only Command Center wiring — offline tests (no live gateway).

Covers: 5-state quote classification, market-data availability in the snapshot, DATA_NOT_AVAILABLE
handling, the PAPER badge, no fabricated values, execution-disabled / zero-orders read-only mode,
account values → dashboard, reconciliation display, and the read-only AI observation pass.
"""

import math
from datetime import datetime, timedelta, timezone

from atp.brokers.base import Account, Position
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard.observe import observe_readonly
from atp.dashboard.snapshot import build_snapshot, classify_quote
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy import MomentumStrategy

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


def _risk(equity=1_000_000.0):
    return RiskEngine(limits=RiskLimits(),
                      state=RiskState(day_start_equity=equity, peak_equity=equity))


def _account(equity=1_000_000.0, cash=1_000_000.0, positions=None):
    return Account(cash=cash, equity=equity, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions=positions or {})


# --------------------------------------------------------------------------- classify_quote
def test_classify_data_available():
    status, _ = classify_quote(bid=1.15275, ask=1.15276, last=None)
    assert status == "DATA_AVAILABLE"


def test_classify_subscription_required_is_not_available():
    status, reason = classify_quote(bid=float("nan"), ask=float("nan"), last=float("nan"),
                                    error_code=10089, error_msg="subscription required")
    assert status == "DATA_NOT_AVAILABLE"
    assert "subscription" in reason.lower()


def test_classify_no_quote_is_not_available():
    status, _ = classify_quote(bid=None, ask=None, last=None)
    assert status == "DATA_NOT_AVAILABLE"


def test_classify_nan_never_shown_as_quote():
    status, _ = classify_quote(bid=float("nan"), ask=float("nan"), last=float("nan"))
    assert status == "DATA_NOT_AVAILABLE"


def test_classify_delayed_and_stale_and_error():
    assert classify_quote(bid=100.0, ask=100.1, last=100.0, delayed=True)[0] == "DELAYED"
    old = NOW - timedelta(seconds=120)
    assert classify_quote(bid=100.0, ask=100.1, last=100.0, ts=old, now=NOW, stale_after=30)[0] == "STALE"
    assert classify_quote(bid=None, ask=None, last=None, error_code=321, error_msg="bad")[0] == "ERROR"


# --------------------------------------------------------------------------- snapshot wiring
def _market_data():
    return [
        {"symbol": "EUR.USD", "asset_class": "fx", "exchange": "IDEALPRO",
         "status": "DATA_AVAILABLE", "bid": 1.15, "ask": 1.1501, "last": None, "reason": "live"},
        {"symbol": "AAPL", "asset_class": "equity", "exchange": "NASDAQ",
         "status": "DATA_NOT_AVAILABLE", "bid": None, "ask": None, "last": None,
         "reason": "IBKR market-data subscription required"},
    ]


def test_snapshot_carries_market_data_and_partial_health():
    snap = build_snapshot(account=_account(), risk=_risk(), mode="paper",
                          market_data=_market_data(), execution_enabled=False, orders=0,
                          connected=True, buying_power=4_000_000.0).as_dict()
    assert len(snap["market_data"]) == 2
    # one available + one not → PARTIAL (degraded)
    assert snap["system_health"]["market_data"] == "degraded"
    # the unavailable instrument is surfaced, not hidden
    aapl = [r for r in snap["market_data"] if r["symbol"] == "AAPL"][0]
    assert aapl["status"] == "DATA_NOT_AVAILABLE" and "subscription" in aapl["reason"].lower()


def test_snapshot_paper_badge_and_readonly_mode():
    snap = build_snapshot(account=_account(), risk=_risk(), mode="paper",
                          execution_enabled=False, orders=0, connected=True).as_dict()
    assert snap["mode"] == "paper"
    assert snap["execution_enabled"] is False
    assert snap["orders"] == 0
    assert snap["system_health"]["execution_engine"] == "disabled"
    assert snap["connected"] is True


def test_snapshot_account_values_reach_dashboard():
    snap = build_snapshot(account=_account(equity=1_000_000.0, cash=999.0), risk=_risk(),
                          buying_power=4_000_000.0, mode="paper").as_dict()
    assert snap["account"]["equity"] == 1_000_000.0
    assert snap["account"]["cash"] == 999.0
    assert snap["account"]["buying_power"] == 4_000_000.0


def test_snapshot_no_fake_buying_power_when_absent():
    snap = build_snapshot(account=_account(), risk=_risk(), mode="paper").as_dict()
    assert snap["account"]["buying_power"] is None   # NO DATA, never invented


def test_snapshot_reconciliation_shows_positions():
    inst = Instrument("AAPL", AssetClass.EQUITY)
    pos = {"AAPL:equity": Position(instrument=inst, quantity=10, avg_price=150.0, market_price=155.0)}
    snap = build_snapshot(account=_account(positions=pos), risk=_risk(), mode="paper").as_dict()
    assert snap["positions"][0]["symbol"] == "AAPL"
    assert snap["positions"][0]["quantity"] == 10


def test_snapshot_backward_compatible_defaults():
    # Callers that don't pass the new args still get well-formed empties (no crash, no fakes).
    snap = build_snapshot(account=_account(), risk=_risk()).as_dict()
    assert snap["market_data"] == [] and snap["subscriptions"] == [] and snap["ai_analysis"] == []
    assert snap["orders"] == 0


# --------------------------------------------------------------------------- AI observation
def _uptrend_bars(n=80):
    inst = Instrument("EUR.USD", AssetClass.FX, currency="USD")
    out = []
    for i in range(n):
        p = 1.10 + 0.001 * i + 0.0002 * math.sin(i / 3.0)
        out.append(Bar(inst, p, p * 1.0002, p * 0.9998, p, 1000, NOW + timedelta(minutes=5 * i)))
    return out


def test_observe_no_data_when_insufficient_bars():
    inst = Instrument("EUR.USD", AssetClass.FX, currency="USD")
    bars = [Bar(inst, 1.1, 1.1, 1.1, 1.1, 1000, NOW)]
    obs = observe_readonly({inst.key: bars}, [MomentumStrategy()])
    assert obs and all(o["status"] == "NO DATA" for o in obs)


def test_observe_produces_observation_or_signal_when_warm():
    inst = Instrument("EUR.USD", AssetClass.FX, currency="USD")
    obs = observe_readonly({inst.key: _uptrend_bars()}, [MomentumStrategy()])
    assert obs and obs[0]["status"] in ("SIGNAL", "OBSERVATION")
    # never fabricated: an OBSERVATION carries no invented expected_return
    for o in obs:
        if o["status"] == "OBSERVATION":
            assert o["expected_return"] is None


def test_observe_never_executes_returns_plain_dicts():
    inst = Instrument("EUR.USD", AssetClass.FX, currency="USD")
    obs = observe_readonly({inst.key: _uptrend_bars()}, [MomentumStrategy()])
    # output is pure data — no order objects, no broker interaction possible
    assert all(set(o) >= {"agent", "instrument", "status"} for o in obs)


# --------------------------------------------------------------------------- Phase 2B: real data
def _phase2b_market_data():
    """Mirrors the verified live state: EUR.USD realtime; AAPL/NVDA/SPY blocked by IBKR 10089."""
    md = [{"symbol": "EUR.USD", "asset_class": "fx", "exchange": "IDEALPRO",
           "status": "DATA_AVAILABLE", "market_data_type": "REALTIME",
           "bid": 1.15246, "ask": 1.15247, "last": None, "error_code": None, "reason": "live"}]
    for sym, exch in (("AAPL", "NASDAQ"), ("NVDA", "NASDAQ"), ("SPY", "ARCA/NYSE")):
        st, reason = classify_quote(bid=None, ask=None, last=None, error_code=10089,
                                    error_msg="subscription required")
        md.append({"symbol": sym, "asset_class": "equity", "exchange": exch, "status": st,
                   "market_data_type": None, "bid": None, "ask": None, "last": None,
                   "error_code": 10089, "reason": reason})
    return md


def _snap_2b():
    return build_snapshot(account=_account(), risk=_risk(), mode="paper",
                          market_data=_phase2b_market_data(), execution_enabled=False,
                          orders=0, connected=True).as_dict()


def test_eurusd_realtime_appears_in_dashboard():
    row = [r for r in _snap_2b()["market_data"] if r["symbol"] == "EUR.USD"][0]
    assert row["status"] == "DATA_AVAILABLE"
    assert row["market_data_type"] == "REALTIME"
    assert row["bid"] == 1.15246 and row["ask"] == 1.15247


def test_us_equities_10089_are_data_not_available():
    md = {r["symbol"]: r for r in _snap_2b()["market_data"]}
    for sym in ("AAPL", "NVDA", "SPY"):
        assert md[sym]["status"] == "DATA_NOT_AVAILABLE"
        assert md[sym]["error_code"] == 10089
        assert "subscription" in md[sym]["reason"].lower()


def test_no_fake_prices_for_unavailable():
    md = {r["symbol"]: r for r in _snap_2b()["market_data"]}
    for sym in ("AAPL", "NVDA", "SPY"):
        assert md[sym]["bid"] is None and md[sym]["ask"] is None and md[sym]["last"] is None
        assert md[sym]["market_data_type"] is None   # no realtime/delayed label invented


def test_delayed_never_presented_as_realtime():
    # A present quote flagged delayed must classify DELAYED, never DATA_AVAILABLE/REALTIME.
    status, _ = classify_quote(bid=100.0, ask=100.1, last=100.0, delayed=True)
    assert status == "DELAYED"


def test_no_signal_generated_from_unavailable_data():
    # AAPL has no bars (unavailable) → the AI must report NO DATA, never a signal.
    inst = Instrument("AAPL", AssetClass.EQUITY)
    obs = observe_readonly({inst.key: []}, [MomentumStrategy()])
    assert obs and all(o["status"] == "NO DATA" for o in obs)
    assert not any(o["status"] == "SIGNAL" for o in obs)


def test_readonly_snapshot_has_zero_orders_execution_disabled():
    snap = _snap_2b()
    assert snap["orders"] == 0
    assert snap["execution_enabled"] is False
    assert snap["system_health"]["execution_engine"] == "disabled"
