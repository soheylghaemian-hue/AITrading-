"""Risk Engine scenario tests (§4) — the veto must be proven, not assumed.

Covers every scenario from the build spec: max position, daily-loss kill switch, leverage /
portfolio exposure, correlation exposure, invalid price, broker disconnect, position mismatch,
and the emergency kill switch. The Risk Engine can never be overridden by the AI model.
"""

from atp.brokers.base import Account, Order, Position
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

AAA = Instrument("AAA", AssetClass.EQUITY)
BBB = Instrument("BBB", AssetClass.EQUITY)


def _account(equity=100_000.0, *, gross=0.0, positions=None):
    return Account(cash=equity, equity=equity, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=gross, net_exposure=gross, positions=positions or {})


def _engine(**limit_overrides):
    limits = RiskLimits(**limit_overrides)
    return RiskEngine(limits=limits, state=RiskState(day_start_equity=100_000.0, peak_equity=100_000.0))


def _pos(inst, qty, price):
    return Position(instrument=inst, quantity=qty, avg_price=price, market_price=price)


# --------------------------------------------------------------------------- 1. max position
def test_max_position_rejects_oversized_order():
    # Want €50,000 (500 @ 100), limit = 20% of €100k = €20,000.
    risk = _engine(max_position_pct=0.20, max_gross_leverage=5.0)
    d = risk.check_order(Order(AAA, Side.BUY, 500), _account(), price=100.0, current_qty=0)
    assert not d.approved and "position" in d.reason


# --------------------------------------------------------------------------- 2. daily loss kill
def test_daily_loss_limit_is_a_kill_switch():
    risk = _engine(max_daily_loss_pct=0.02)          # €2,000 on €100k
    risk.mark_equity(97_999.0)                        # −€2,001 => breach
    assert risk.state.halted
    d = risk.check_order(Order(AAA, Side.BUY, 1), _account(equity=97_999.0), price=100.0, current_qty=0)
    assert not d.approved and "halted" in d.reason


# --------------------------------------------------------------------------- 3. leverage / exposure
def test_max_leverage_rejects():
    risk = _engine(max_position_pct=1.0, max_gross_leverage=1.0)
    # Book already 90% gross; adding €30k pushes to 1.2x.
    d = risk.check_order(Order(AAA, Side.BUY, 300), _account(gross=90_000.0), price=100.0, current_qty=0)
    assert not d.approved and "leverage" in d.reason


def test_portfolio_exposure_rejects():
    risk = _engine(max_position_pct=1.0, max_gross_leverage=1.0)
    d = risk.check_order(Order(BBB, Side.BUY, 200), _account(gross=95_000.0), price=100.0, current_qty=0)
    assert not d.approved   # 95k + 20k = 115k gross > 1.0x


# --------------------------------------------------------------------------- 4. correlation exposure
def test_correlation_exposure_rejects_correlated_cluster():
    risk = _engine(max_position_pct=1.0, max_gross_leverage=5.0, max_correlated_exposure_pct=0.35)
    acct = _account(gross=25_000.0, positions={BBB.key: _pos(BBB, 250, 100.0)})   # €25k in BBB
    corr = lambda a, b: 0.9   # AAA and BBB are ~90% correlated  # noqa: E731
    # New €15k in AAA: cluster = 15k + 0.9*25k = 37.5k > 35k limit.
    d = risk.check_order(Order(AAA, Side.BUY, 150), acct, price=100.0, current_qty=0, correlation_fn=corr)
    assert not d.approved and "correlated" in d.reason


def test_uncorrelated_position_allowed():
    risk = _engine(max_position_pct=1.0, max_gross_leverage=5.0, max_correlated_exposure_pct=0.35)
    acct = _account(gross=25_000.0, positions={BBB.key: _pos(BBB, 250, 100.0)})
    corr = lambda a, b: 0.1   # uncorrelated  # noqa: E731
    d = risk.check_order(Order(AAA, Side.BUY, 150), acct, price=100.0, current_qty=0, correlation_fn=corr)
    assert d.approved


# --------------------------------------------------------------------------- 5. invalid price
def test_invalid_price_no_trade():
    risk = _engine()
    assert not risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=0.0, current_qty=0).approved
    assert not risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=-5.0, current_qty=0).approved
    assert not risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=float("nan"), current_qty=0).approved


# --------------------------------------------------------------------------- 6. broker disconnect
def test_broker_disconnect_blocks_new_orders():
    risk = _engine(max_position_pct=1.0)
    risk.set_broker_connected(False)
    d = risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=100.0, current_qty=0)
    assert not d.approved and "disconnected" in d.reason
    risk.set_broker_connected(True)
    assert risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=100.0, current_qty=0).approved


# --------------------------------------------------------------------------- 7. position mismatch
def test_position_mismatch_halts_trading():
    risk = _engine()
    risk.force_halt("reconciliation break: AAA internal=100 broker=90")  # from the Reconciler
    d = risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=100.0, current_qty=0)
    assert not d.approved and "halted" in d.reason


# --------------------------------------------------------------------------- 8. kill switch
def test_kill_switch_blocks_everything_including_reductions():
    risk = _engine()
    risk.kill_switch("manual emergency stop")
    # A new position is blocked ...
    assert not risk.check_order(Order(AAA, Side.BUY, 1), _account(), price=100.0, current_qty=0).approved
    # ... and even a risk-reducing (closing) order is blocked under a full kill.
    d = risk.check_order(Order(AAA, Side.SELL, 100), _account(), price=100.0, current_qty=100)
    assert not d.approved and "kill" in d.reason
    risk.reset_kill()
    assert risk.check_order(Order(AAA, Side.SELL, 100), _account(), price=100.0, current_qty=100).approved


# --------------------------------------------------------------------------- reductions still allowed when halted
def test_reduction_allowed_when_halted_but_not_killed():
    risk = _engine()
    risk.force_halt("daily loss")
    # Closing an existing long is allowed while halted (you may always cut risk).
    d = risk.check_order(Order(AAA, Side.SELL, 100), _account(), price=100.0, current_qty=100)
    assert d.approved and "reducing" in d.reason
