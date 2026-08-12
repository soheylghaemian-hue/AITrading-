"""Model governance tests (§19): registry gating, decay-driven suspension, the desk actually
ignoring a suspended strategy, and validated model promotion + rollback."""

import math
from datetime import datetime, timedelta, timezone

from atp.brokers.base import Fill
from atp.core.enums import AssetClass, Side
from atp.core.events import Bar, Instrument
from atp.governance import (
    DecayPolicy,
    GovernanceMonitor,
    ModelRegistry,
    ModelVersion,
    PromotionPolicy,
    StrategyRegistry,
    StrategyStatus,
)
from atp.journal import InMemoryJournal, TradeAssembler, TradeContext
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.base import Signal, Strategy
from atp.core.enums import Action, Regime

INST = Instrument("AAPL", AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- registry
def test_unregistered_strategy_defaults_active():
    reg = StrategyRegistry()
    assert reg.is_active("anything") is True  # governance is opt-in


def test_suspend_and_reactivate():
    reg = StrategyRegistry()
    reg.register("momentum")
    assert reg.is_active("momentum")
    reg.suspend("momentum", "decay")
    assert not reg.is_active("momentum")
    assert reg.suspended() == ["momentum"]
    reg.reactivate("momentum", "recovered", version="v2")
    assert reg.is_active("momentum")
    assert reg.get("momentum").version == "v2"


def test_probation_is_still_tradable():
    reg = StrategyRegistry()
    reg.register("m")
    reg.set_probation("m", "watch")
    assert reg.get("m").status is StrategyStatus.PROBATION
    assert reg.is_active("m")  # probation still trades, just flagged


# --------------------------------------------------------------------------- decay monitor
def _journal_with(strategy, regime, pnls):
    """Build a journal of closed trades with the given per-trade P&Ls via the assembler."""
    journal = InMemoryJournal()
    a = TradeAssembler()
    price = 100.0
    for i, pnl in enumerate(pnls):
        # qty 1: exit = entry + pnl gives exactly `pnl` realized (no commission).
        a.on_fill(Fill(INST, Side.BUY, 1, price, 0.0, T0 + timedelta(minutes=2 * i)),
                  TradeContext(strategy=strategy, regime=regime))
        rec = a.on_fill(Fill(INST, Side.SELL, 1, price + pnl, 0.0, T0 + timedelta(minutes=2 * i + 1)), None)
        journal.record(rec)
    return journal


def test_monitor_suspends_losing_strategy():
    reg = StrategyRegistry()
    reg.register("loser")
    journal = _journal_with("loser", "range", [-5.0] * 15)  # clearly negative expectancy
    monitor = GovernanceMonitor(reg, DecayPolicy(min_trades=10))

    decisions = monitor.evaluate(journal)

    assert any(d.action == "suspend" and d.name == "loser" for d in decisions)
    assert not reg.is_active("loser")


def test_monitor_ignores_small_sample():
    reg = StrategyRegistry()
    reg.register("new")
    journal = _journal_with("new", "range", [-5.0] * 3)  # below evidence gate
    monitor = GovernanceMonitor(reg, DecayPolicy(min_trades=10))

    decisions = monitor.evaluate(journal)

    assert decisions == []
    assert reg.is_active("new")  # not enough evidence to judge


def test_monitor_keeps_profitable_strategy():
    reg = StrategyRegistry()
    reg.register("winner")
    journal = _journal_with("winner", "trending_up", [10.0, -2.0] * 8)  # net positive
    monitor = GovernanceMonitor(reg, DecayPolicy(min_trades=10))

    monitor.evaluate(journal)

    assert reg.is_active("winner")


def test_probation_first_then_suspend():
    reg = StrategyRegistry()
    reg.register("m")
    policy = DecayPolicy(min_trades=10, probation_first=True)
    monitor = GovernanceMonitor(reg, policy)
    journal = _journal_with("m", "range", [-1.0] * 12)

    monitor.evaluate(journal)
    assert reg.get("m").status is StrategyStatus.PROBATION  # first breach

    monitor.evaluate(journal)
    assert reg.get("m").status is StrategyStatus.SUSPENDED   # escalates on the next breach


def test_auto_reactivate_on_recovery():
    reg = StrategyRegistry()
    reg.suspend("m", "prior decay")
    monitor = GovernanceMonitor(reg, DecayPolicy(min_trades=5, auto_reactivate=True))
    journal = _journal_with("m", "trending_up", [8.0] * 10)  # now profitable

    decisions = monitor.evaluate(journal)

    assert any(d.action == "reactivate" for d in decisions)
    assert reg.is_active("m")


# --------------------------------------------------------------------------- desk integration
class _AlwaysBuy(Strategy):
    """Deterministic strategy that always wants to be long — for testing the gate."""

    def __init__(self, name="always_buy"):
        self._name = name

    @property
    def name(self):
        return self._name

    def generate(self, fs, regime):
        return Signal(
            instrument=fs.instrument, action=Action.BUY, confidence=1.0,
            expected_return=0.01, stop_distance=max(fs.close_std, fs.price * 0.01),
            strategy=self._name, regime=regime, ts=fs.ts, rationale="test",
        )


async def test_desk_ignores_suspended_strategy():
    from atp.backtest import Backtester

    bars = []
    for i in range(60):
        p = 100 + 0.1 * i
        bars.append(Bar(INST, p, p * 1.001, p * 0.999, p, 1000, T0 + timedelta(minutes=i)))

    # Suspended before the run -> the desk must place zero orders from it.
    reg = StrategyRegistry()
    reg.suspend("always_buy", "test")
    bt = Backtester(policy=TradingPolicy(capital=100_000.0), strategies=[_AlwaysBuy()],
                    regime=RegimeClassifier(), registry=reg)
    res = await bt.run(bars)
    assert res.n_executed == 0

    # Active -> it trades.
    reg2 = StrategyRegistry()  # unregistered => active
    bt2 = Backtester(policy=TradingPolicy(capital=100_000.0), strategies=[_AlwaysBuy()],
                     regime=RegimeClassifier(), registry=reg2)
    res2 = await bt2.run(bars)
    assert res2.n_executed > 0


# --------------------------------------------------------------------------- versioning
def _mv(strategy, version, sharpe):
    return ModelVersion(strategy, version, params={"lookback": 10}, metrics={"sharpe": sharpe})


def test_baseline_then_validated_promotion():
    mr = ModelRegistry(PromotionPolicy(primary_metric="sharpe", min_improvement=0.1))
    r0 = mr.promote(_mv("m", "v1", sharpe=1.0))
    assert r0.promoted and mr.current("m").version == "v1"  # first => baseline

    # Not enough improvement -> rejected, incumbent stands.
    r1 = mr.promote(_mv("m", "v2", sharpe=1.05))
    assert not r1.promoted and mr.current("m").version == "v1"

    # Clear improvement -> promoted.
    r2 = mr.promote(_mv("m", "v3", sharpe=1.4))
    assert r2.promoted and mr.current("m").version == "v3"


def test_promotion_requires_positive_metric():
    mr = ModelRegistry(PromotionPolicy(require_positive=True))
    r = mr.promote(_mv("m", "v1", sharpe=-0.2))
    assert not r.promoted


def test_rollback_restores_previous_version():
    mr = ModelRegistry(PromotionPolicy(min_improvement=0.0))
    mr.promote(_mv("m", "v1", sharpe=1.0))
    mr.promote(_mv("m", "v2", sharpe=1.5))
    assert mr.current("m").version == "v2"

    restored = mr.rollback("m")
    assert restored.version == "v1"
    assert mr.current("m").version == "v1"


def test_rollback_without_prior_is_safe():
    mr = ModelRegistry()
    mr.promote(_mv("m", "v1", sharpe=1.0))
    assert mr.rollback("m") is None  # nothing earlier to roll back to
    assert mr.current("m").version == "v1"


# --------------------------------------------------------------------------- model lifecycle (§9)
def test_model_lifecycle_forward_transitions():
    from atp.governance import ModelRegistry, ModelStatus
    mr = ModelRegistry()
    v = _mv("m", "v1", sharpe=1.2)
    mr.set_baseline(v)
    assert v.status is ModelStatus.RESEARCH
    for nxt in (ModelStatus.TESTING, ModelStatus.PAPER, ModelStatus.APPROVED, ModelStatus.LIVE):
        mr.transition(v, nxt)
    assert v.status is ModelStatus.LIVE
    assert v.deployment_date is not None       # deployment date stamped on going LIVE


def test_model_illegal_transition_rejected():
    import pytest
    from atp.governance import ModelRegistry, ModelStatus
    mr = ModelRegistry()
    v = _mv("m", "v1", sharpe=1.0)
    with pytest.raises(ValueError, match="illegal model transition"):
        mr.transition(v, ModelStatus.LIVE)      # RESEARCH -> LIVE not allowed


def test_model_suspend_and_retire():
    from atp.governance import ModelRegistry, ModelStatus
    mr = ModelRegistry()
    v = _mv("m", "v1", sharpe=1.0)
    for nxt in (ModelStatus.TESTING, ModelStatus.PAPER, ModelStatus.APPROVED, ModelStatus.LIVE,
                ModelStatus.SUSPENDED):
        mr.transition(v, nxt)
    assert v.status is ModelStatus.SUSPENDED
    mr.transition(v, ModelStatus.LIVE)          # can be reinstated
    mr.transition(v, ModelStatus.RETIRED)
    assert v.status is ModelStatus.RETIRED
    import pytest
    with pytest.raises(ValueError):
        mr.transition(v, ModelStatus.LIVE)      # retired is terminal


def test_model_results_attach_and_query_by_status():
    from atp.governance import ModelRegistry, ModelStatus
    mr = ModelRegistry()
    v = _mv("m", "v1", sharpe=1.5)
    mr.set_baseline(v)
    mr.attach_results(v, "oos", {"sharpe": 1.5, "profit_factor": 1.4})
    mr.attach_results(v, "paper", {"sharpe": 1.3})
    assert v.oos_results["profit_factor"] == 1.4 and v.paper_results["sharpe"] == 1.3
    assert mr.by_status(ModelStatus.RESEARCH) == [v]
