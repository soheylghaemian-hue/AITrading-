"""The configured paper-trading capital is a hard sizing and risk-budget ceiling."""

import math
from types import SimpleNamespace

import pytest

from atp.brokers.base import Account, Order, Position
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument
from atp.dashboard.api import DashboardContext
from atp.opportunity.sizing import PositionSizer
from atp.policy import TradingPolicy
from atp.risk.config import TradingRiskConfig
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore

AAA = Instrument("AAA", AssetClass.EQUITY)
BBB = Instrument("BBB", AssetClass.EQUITY)
ACCOUNT_EQUITY = 1_000_000.0
CAPITAL_MANDATE = 100_000.0


def _account(*, equity: float = ACCOUNT_EQUITY, gross: float = 0.0, positions=None) -> Account:
    return Account(
        cash=equity,
        equity=equity,
        realized_pnl=equity - ACCOUNT_EQUITY,
        unrealized_pnl=0.0,
        gross_exposure=gross,
        net_exposure=gross,
        positions=positions or {},
    )


def _risk(**overrides) -> RiskEngine:
    limits = RiskLimits(max_capital=CAPITAL_MANDATE, **overrides)
    state = RiskState(day_start_equity=ACCOUNT_EQUITY, peak_equity=ACCOUNT_EQUITY)
    return RiskEngine(limits=limits, state=state)


def test_risk_limits_default_remains_unbounded_and_policy_binds_capital():
    assert math.isinf(RiskLimits().max_capital)
    assert TradingPolicy(capital=CAPITAL_MANDATE).to_risk_limits().max_capital == CAPITAL_MANDATE


@pytest.mark.parametrize("field", ["capital", "risk_per_trade_pct", "max_daily_loss_pct"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_trading_risk_config_rejects_every_nonfinite_input(field, value):
    values = {
        "capital": CAPITAL_MANDATE,
        "risk_per_trade_pct": 0.01,
        "max_daily_loss_pct": 0.02,
    }
    values[field] = value
    with pytest.raises(ValueError, match=field):
        TradingRiskConfig(**values)


@pytest.mark.parametrize(
    ("sizing", "extra", "expected"),
    [
        ("risk", {}, 100.0),
        ("notional", {}, 100.0),
        ("hedged", {"hedge_factor": 2.0, "ref_price": 100.0}, 200.0),
    ],
)
def test_position_sizer_uses_capital_mandate_when_equity_is_larger(sizing, extra, expected):
    policy = TradingPolicy(
        capital=CAPITAL_MANDATE,
        risk_per_trade=0.01,
        max_position_pct=1.0,
    )
    units = PositionSizer(whole_units=False).target_units(
        price=100.0,
        stop_distance=10.0,
        equity=ACCOUNT_EQUITY,
        policy=policy,
        sizing=sizing,
        **extra,
    )
    assert units == expected


def test_position_sizer_still_uses_equity_when_it_is_below_mandate():
    policy = TradingPolicy(capital=ACCOUNT_EQUITY, risk_per_trade=0.01, max_position_pct=1.0)
    units = PositionSizer(whole_units=False).target_units(
        price=100.0,
        stop_distance=10.0,
        equity=50_000.0,
        policy=policy,
    )
    assert units == 50.0


def test_position_and_gross_exposure_caps_use_capital_mandate():
    position_risk = _risk(max_position_pct=0.20, max_gross_leverage=10.0)
    position = position_risk.check_order(
        Order(AAA, Side.BUY, 201), _account(), price=100.0, current_qty=0.0)
    assert not position.approved and "position" in position.reason

    leverage_risk = _risk(max_position_pct=10.0, max_gross_leverage=1.0)
    leverage = leverage_risk.check_order(
        Order(AAA, Side.BUY, 1001), _account(), price=100.0, current_qty=0.0)
    assert not leverage.approved and "leverage" in leverage.reason


def test_trade_risk_and_correlated_exposure_use_capital_mandate():
    trade_risk = _risk(
        max_position_pct=10.0,
        max_gross_leverage=10.0,
        max_trade_risk_pct=0.01,
        max_daily_loss_pct=1.0,
    )
    trade = trade_risk.check_order(
        Order(AAA, Side.BUY, 100),
        _account(),
        price=100.0,
        current_qty=0.0,
        stop_distance=11.0,
    )
    assert not trade.approved and "trade risk" in trade.reason

    bbb = Position(BBB, quantity=250, avg_price=100.0, market_price=100.0)
    correlation_risk = _risk(
        max_position_pct=10.0,
        max_gross_leverage=10.0,
        max_correlated_exposure_pct=0.35,
    )
    correlated = correlation_risk.check_order(
        Order(AAA, Side.BUY, 150),
        _account(gross=25_000.0, positions={BBB.key: bbb}),
        price=100.0,
        current_qty=0.0,
        correlation_fn=lambda _a, _b: 0.9,
    )
    assert not correlated.approved and "correlated" in correlated.reason


def test_daily_halt_and_remaining_budget_use_capital_mandate():
    halt_risk = _risk(max_daily_loss_pct=0.02, max_drawdown_pct=1.0)
    halt_risk.mark_equity(997_999.0)
    assert halt_risk.state.halted and "daily loss" in halt_risk.state.halt_reason

    budget_risk = _risk(
        max_position_pct=10.0,
        max_gross_leverage=10.0,
        max_trade_risk_pct=1.0,
        max_daily_loss_pct=0.02,
    )
    budget = budget_risk.check_order(
        Order(AAA, Side.BUY, 10),
        _account(equity=998_500.0),
        price=100.0,
        current_qty=0.0,
        stop_distance=60.0,
    )
    assert not budget.approved and "remaining daily-loss budget" in budget.reason


def test_drawdown_uses_capital_mandate():
    risk = _risk(max_daily_loss_pct=1.0, max_drawdown_pct=0.15)
    risk.mark_equity(984_999.0)
    assert risk.state.halted and "drawdown" in risk.state.halt_reason


def test_dashboard_set_and_restart_restore_capital_on_risk_engine(tmp_path):
    path = str(tmp_path / "risk.json")
    broker = SimpleNamespace(is_connected=lambda: True)
    first_risk = RiskEngine(
        limits=RiskLimits(),
        state=RiskState(day_start_equity=ACCOUNT_EQUITY, peak_equity=ACCOUNT_EQUITY),
    )
    assert math.isinf(first_risk.limits.max_capital)
    first = DashboardContext(broker=broker, risk=first_risk, config_store=RiskConfigStore(path))
    first.set_risk_config(CAPITAL_MANDATE, 0.01, 0.02)
    assert first_risk.limits.max_capital == CAPITAL_MANDATE

    restarted_risk = RiskEngine(
        limits=RiskLimits(),
        state=RiskState(day_start_equity=ACCOUNT_EQUITY, peak_equity=ACCOUNT_EQUITY),
    )
    restarted = DashboardContext(
        broker=broker,
        risk=restarted_risk,
        config_store=RiskConfigStore(path),
    )
    loaded = restarted.load_persisted_risk_config()
    assert loaded is not None and loaded.capital == CAPITAL_MANDATE
    assert restarted_risk.limits.max_capital == CAPITAL_MANDATE
