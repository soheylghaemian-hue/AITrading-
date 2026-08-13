"""TRADING RISK — persistence, restart survival, remaining-daily-budget veto, daily-loss lock."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from atp.brokers.base import Account, Order
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Instrument
from atp.dashboard.api import DashboardContext
from atp.risk.config import TradingRiskConfig
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.risk.store import RiskConfigStore

AAPL = Instrument("AAPL", AssetClass.EQUITY)


def _acct(equity=1_000_000.0):
    return Account(cash=equity, equity=equity, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions={})


def _risk(equity=1_000_000.0):
    return RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=equity, peak_equity=equity))


# --------------------------------------------------------------------------- persistence
def test_store_roundtrip():
    tmp_path = Path(tempfile.mkdtemp())
    store = RiskConfigStore(str(tmp_path / "risk.json"))
    assert store.load() is None                       # nothing yet
    cfg = TradingRiskConfig(capital=250_000.0, risk_per_trade_pct=0.02, max_daily_loss_pct=0.05)
    store.save(cfg)
    got = store.load()
    assert got is not None
    assert got.capital == 250_000.0 and got.risk_per_trade_pct == 0.02 and got.max_daily_loss_pct == 0.05


def test_settings_survive_restart():
    tmp_path = Path(tempfile.mkdtemp())
    path = str(tmp_path / "risk.json")
    # session 1: user sets config
    ctx1 = DashboardContext(broker=SimpleNamespace(is_connected=lambda: True), risk=_risk(),
                            config_store=RiskConfigStore(path))
    ctx1.set_risk_config(capital=500_000.0, risk_per_trade_pct=0.01, max_daily_loss_pct=0.02)
    # session 2: fresh engine + context (a "restart") loads persisted config and re-applies it
    r2 = _risk()
    ctx2 = DashboardContext(broker=SimpleNamespace(is_connected=lambda: True), risk=r2,
                            config_store=RiskConfigStore(path))
    loaded = ctx2.load_persisted_risk_config()
    assert loaded is not None and loaded.capital == 500_000.0
    assert r2.limits.max_trade_risk_pct == 0.01        # engine limits restored after restart
    assert r2.limits.max_daily_loss_pct == 0.02


def test_load_when_nothing_persisted():
    tmp_path = Path(tempfile.mkdtemp())
    ctx = DashboardContext(broker=SimpleNamespace(is_connected=lambda: True), risk=_risk(),
                           config_store=RiskConfigStore(str(tmp_path / "none.json")))
    assert ctx.load_persisted_risk_config() is None


# --------------------------------------------------------------------------- remaining daily budget
def test_remaining_daily_budget_blocks_oversized_trade():
    r = _risk(1_000_000.0)
    r.update_limits(max_trade_risk_pct=0.05, max_daily_loss_pct=0.02)  # daily budget = $20k
    # already down $15k today → only $5k of daily budget remains
    acct = Account(cash=985_000.0, equity=985_000.0, realized_pnl=-15_000.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions={})
    # a trade risking $8k (within per-trade 5% = $49.25k) but > $5k remaining → vetoed
    over = Order(AAPL, Side.BUY, 200, OrderType.MARKET)
    dec = r.check_order(over, acct, price=100.0, current_qty=0.0, stop_distance=40.0)
    assert not dec.approved and "remaining daily" in dec.reason.lower()
    # a trade risking $4k fits in the remaining $5k → approved
    ok = Order(AAPL, Side.BUY, 100, OrderType.MARKET)
    assert r.check_order(ok, acct, price=100.0, current_qty=0.0, stop_distance=40.0).approved


# --------------------------------------------------------------------------- daily-loss lock lifecycle
def test_daily_loss_lock_holds_until_next_trading_day():
    r = _risk(1_000_000.0)
    r.update_limits(max_daily_loss_pct=0.02)
    r.mark_equity(975_000.0)                    # −2.5% → halt latched
    assert r.state.halted
    blocked = r.check_order(Order(AAPL, Side.BUY, 1, OrderType.MARKET), _acct(975_000.0),
                            price=100.0, current_qty=0.0, stop_distance=1.0)
    assert not blocked.approved                 # locked for the rest of the day
    # a later mark on the same day does NOT clear it
    r.mark_equity(999_000.0)
    assert r.state.halted
    # the new trading day resets the baseline and clears the lock
    r.start_new_day(999_000.0)
    assert not r.state.halted
    assert r.check_order(Order(AAPL, Side.BUY, 1, OrderType.MARKET), _acct(999_000.0),
                         price=100.0, current_qty=0.0, stop_distance=1.0).approved


def test_ai_cannot_override_risk_veto():
    # There is no override path: check_order is the sole gate and returns a hard veto. Even a
    # "high confidence" caller cannot bypass it — the decision object only carries approved/reason.
    r = _risk(1_000_000.0)
    r.update_limits(max_trade_risk_pct=0.01)     # $10k per-trade budget
    huge = Order(AAPL, Side.BUY, 1000, OrderType.MARKET)  # 1000×$50 stop = $50k risk
    dec = r.check_order(huge, _acct(), price=100.0, current_qty=0.0, stop_distance=50.0)
    assert dec.approved is False
    assert not dec  # __bool__ is False → callers cannot accidentally treat it as allowed
