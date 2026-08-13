"""TRADING RISK config — the 3 user parameters, really connected to the Risk Engine.

Proves: monetary limits are derived correctly; the config updates the authoritative Risk Engine
limits; over-risk orders are vetoed; the daily-loss limit blocks all new trades once reached; the
snapshot surfaces the panel; and the dashboard control applies the config.
"""

from types import SimpleNamespace

import pytest

from atp.brokers.base import Account, Order
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Instrument
from atp.dashboard.api import DashboardContext
from atp.dashboard.snapshot import build_snapshot
from atp.risk.config import TradingRiskConfig, trading_risk_view
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

AAPL = Instrument("AAPL", AssetClass.EQUITY)


def _acct(equity=1_000_000.0, positions=None):
    return Account(cash=equity, equity=equity, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions=positions or {})


def _risk(equity=1_000_000.0):
    return RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=equity, peak_equity=equity))


# --------------------------------------------------------------------------- monetary math
def test_monetary_limits_match_the_example():
    c = TradingRiskConfig(capital=1_000_000.0, risk_per_trade_pct=0.01, max_daily_loss_pct=0.02)
    assert c.max_risk_per_trade_amount == 10_000.0   # 1% of 1,000,000
    assert c.max_daily_loss_amount == 20_000.0       # 2% of 1,000,000


def test_validation_rejects_nonsense():
    with pytest.raises(ValueError):
        TradingRiskConfig(capital=0, risk_per_trade_pct=0.01, max_daily_loss_pct=0.02)
    with pytest.raises(ValueError):
        TradingRiskConfig(capital=1_000, risk_per_trade_pct=0, max_daily_loss_pct=0.02)
    with pytest.raises(ValueError):
        TradingRiskConfig(capital=1_000, risk_per_trade_pct=1.5, max_daily_loss_pct=0.02)
    with pytest.raises(ValueError):  # one trade can't risk more than the whole day
        TradingRiskConfig(capital=1_000, risk_per_trade_pct=0.05, max_daily_loss_pct=0.02)


# --------------------------------------------------------------------------- status view
def test_status_active_and_daily_limit_reached():
    c = TradingRiskConfig(1_000_000.0, 0.01, 0.02)
    active = trading_risk_view(c, daily_pnl=-5_000.0, halted=False)
    assert active["status"] == "ACTIVE"
    assert active["remaining_daily_risk"] == 15_000.0
    reached = trading_risk_view(c, daily_pnl=-20_000.0, halted=False)
    assert reached["status"] == "DAILY LOSS LIMIT REACHED"
    assert reached["remaining_daily_risk"] == 0.0
    halted = trading_risk_view(c, daily_pnl=0.0, halted=True)  # engine latch
    assert halted["status"] == "DAILY LOSS LIMIT REACHED"


# --------------------------------------------------------------------------- engine wiring
def test_update_limits_flows_into_engine():
    r = _risk()
    r.update_limits(max_trade_risk_pct=0.02, max_daily_loss_pct=0.05)
    assert r.limits.max_trade_risk_pct == 0.02
    assert r.limits.max_daily_loss_pct == 0.05
    with pytest.raises(ValueError):
        r.update_limits(not_a_limit=1)


def test_per_trade_risk_is_enforced():
    r = _risk(1_000_000.0)
    r.update_limits(max_trade_risk_pct=0.01)          # 1% of 1M = $10k budget
    # 200 units × $60 stop = $12k risk > $10k → vetoed (notional $20k stays within caps)
    over = Order(AAPL, Side.BUY, 200, OrderType.MARKET)
    assert not r.check_order(over, _acct(), price=100.0, current_qty=0.0, stop_distance=60.0).approved
    # 200 units × $40 stop = $8k risk < $10k → approved
    ok = Order(AAPL, Side.BUY, 200, OrderType.MARKET)
    assert r.check_order(ok, _acct(), price=100.0, current_qty=0.0, stop_distance=40.0).approved


def test_daily_loss_limit_blocks_all_new_trades():
    r = _risk(1_000_000.0)
    r.update_limits(max_daily_loss_pct=0.02)          # 2% daily-loss budget
    r.mark_equity(975_000.0)                           # −2.5% > 2% → latch halt for the day
    assert r.state.halted
    new_trade = Order(AAPL, Side.BUY, 10, OrderType.MARKET)
    assert not r.check_order(new_trade, _acct(975_000.0), price=100.0, current_qty=0.0,
                             stop_distance=1.0).approved


# --------------------------------------------------------------------------- snapshot + control
def test_snapshot_exposes_trading_risk_from_engine_limits():
    r = _risk()
    r.update_limits(max_trade_risk_pct=0.01, max_daily_loss_pct=0.02)
    snap = build_snapshot(account=_acct(), risk=r, risk_capital=1_000_000.0).as_dict()
    tr = snap["trading_risk"]
    assert tr["capital"] == 1_000_000.0
    assert tr["max_risk_per_trade"] == 10_000.0
    assert tr["max_daily_loss"] == 20_000.0
    assert tr["status"] == "ACTIVE"


def test_dashboard_control_applies_config_to_engine():
    r = _risk()
    ctx = DashboardContext(broker=SimpleNamespace(is_connected=lambda: True), risk=r)
    result = ctx.set_risk_config(capital=2_000_000.0, risk_per_trade_pct=0.005, max_daily_loss_pct=0.01)
    assert result["max_risk_per_trade"] == 10_000.0   # 0.5% of 2M
    assert result["max_daily_loss"] == 20_000.0       # 1% of 2M
    assert r.limits.max_trade_risk_pct == 0.005        # engine actually updated
    assert r.limits.max_daily_loss_pct == 0.01
    assert ctx.risk_config is not None and ctx.risk_config.capital == 2_000_000.0


def test_control_hook_updates_sizer_policy():
    r = _risk()
    seen = {}
    ctx = DashboardContext(broker=SimpleNamespace(is_connected=lambda: True), risk=r,
                           on_risk_config_change=lambda cfg: seen.update(cap=cfg.capital))
    ctx.set_risk_config(capital=500_000.0, risk_per_trade_pct=0.01, max_daily_loss_pct=0.02)
    assert seen["cap"] == 500_000.0                    # runner hook fired → sizer/policy can update
