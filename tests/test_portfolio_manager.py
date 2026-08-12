"""Master Portfolio Manager tests (§9): portfolio-level allocation, budget, position count,
diversification, and 'cash is valid'."""

from datetime import datetime, timezone

from atp.brokers.base import Account, Position
from atp.core.enums import Action, AssetClass, Regime
from atp.core.events import Instrument
from atp.opportunity.engine import Opportunity
from atp.portfolio import MasterPortfolioManager
from atp.strategy.base import Signal

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _opp(symbol, action=Action.BUY, score=1.0, price=100.0):
    inst = Instrument(symbol, AssetClass.EQUITY)
    sig = Signal(inst, action, 0.8, 0.02, 1.0, "s", Regime.TRENDING_UP, T0)
    return Opportunity(signal=sig, price=price, score=score, reward_risk=2.0, fractional_risk=0.01)


def _account(equity=100_000.0, *, gross=0.0, positions=None):
    return Account(cash=equity, equity=equity, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=gross, net_exposure=gross, positions=positions or {})


def _pos(symbol, qty, price=100.0):
    inst = Instrument(symbol, AssetClass.EQUITY)
    return Position(instrument=inst, quantity=qty, avg_price=price, market_price=price)


def test_funds_within_budget_and_count():
    mpm = MasterPortfolioManager(max_positions=3, max_gross_leverage=1.0, per_position_cap=0.20)
    opps = [_opp("A"), _opp("B"), _opp("C"), _opp("D")]
    decisions = mpm.allocate(opps, _account())
    funded = [d for d in decisions if d.allocate]
    assert len(funded) == 3                      # max_positions caps it at 3
    assert decisions[-1].reason == "max positions reached"


def test_gross_budget_limits_allocations():
    # 1.0x leverage, 20% per name => at most 5 names fit the €100k gross budget.
    mpm = MasterPortfolioManager(max_positions=20, max_gross_leverage=1.0, per_position_cap=0.20)
    opps = [_opp(chr(65 + i)) for i in range(8)]
    funded = mpm.funded(mpm.allocate(opps, _account()))
    assert len(funded) == 5


def test_existing_gross_reduces_budget():
    mpm = MasterPortfolioManager(max_positions=20, max_gross_leverage=1.0, per_position_cap=0.20)
    # Already 80% gross => only €20k headroom => one more 20% name.
    funded = mpm.funded(mpm.allocate([_opp("A"), _opp("B")], _account(gross=80_000.0)))
    assert len(funded) == 1


def test_exits_are_always_allocated():
    mpm = MasterPortfolioManager(max_positions=1, max_gross_leverage=0.0, per_position_cap=0.20)
    # No budget/slots, but a CLOSE (exit) must still pass.
    decisions = mpm.allocate([_opp("A", action=Action.CLOSE)], _account(gross=100_000.0))
    assert decisions[0].allocate and decisions[0].reason == "exit"


def test_cash_is_valid_when_no_budget():
    mpm = MasterPortfolioManager(max_positions=5, max_gross_leverage=1.0, per_position_cap=0.20)
    funded = mpm.funded(mpm.allocate([_opp("A")], _account(gross=100_000.0)))  # fully invested
    assert funded == []                          # allocate nothing => hold cash


def test_diversification_declines_correlated_duplicate():
    mpm = MasterPortfolioManager(max_positions=10, max_gross_leverage=5.0,
                                 per_position_cap=0.20, correlation_threshold=0.6)
    corr = lambda a, b: 0.9  # A and B ~correlated  # noqa: E731
    decisions = mpm.allocate([_opp("A"), _opp("B")], _account(), correlation_fn=corr)
    assert decisions[0].allocate                 # first funded
    assert not decisions[1].allocate and "correlated" in decisions[1].reason


def test_correlated_but_opposite_direction_is_allowed():
    mpm = MasterPortfolioManager(max_gross_leverage=5.0, correlation_threshold=0.6)
    corr = lambda a, b: 0.9  # noqa: E731
    decisions = mpm.allocate([_opp("A", action=Action.BUY), _opp("B", action=Action.SELL)],
                             _account(), correlation_fn=corr)
    assert decisions[0].allocate and decisions[1].allocate   # opposite legs are a hedge, not a duplicate
