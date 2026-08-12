"""Live runtime tests (§17/§19): feed mapping, the run loop over ReplayFeed + PaperBroker,
internal-book reconciliation, and governance taking a strategy offline mid-stream."""

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from atp.brokers.reconcile import Reconciler, diff_positions
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.governance import DecayPolicy, GovernanceMonitor, StrategyRegistry, StrategyStatus
from atp.journal import InMemoryJournal
from atp.live import (
    LiveRunner,
    ReplayFeed,
    bar_from_rt,
    build_paper_stack,
    quote_from_ticker,
)
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("X", AssetClass.EQUITY)
# A Monday, so the default Mon–Fri trading-hours gate is open.
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _bars(n=200, amp=4.0, drift=0.05):
    return [
        Bar(INST, p := 100 + amp * math.sin(i / 6.0) + drift * i,
            p * 1.002, p * 0.998, p, 1000 + i, T0 + timedelta(minutes=i))
        for i in range(n)
    ]


# --------------------------------------------------------------------------- feed mappers
def test_bar_from_rt_maps_fields():
    ts = T0
    rt = SimpleNamespace(time=ts, open_=100.0, high=101.0, low=99.5, close=100.5, volume=1234)
    bar = bar_from_rt(rt, INST)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100.0, 101.0, 99.5, 100.5, 1234.0)
    assert bar.ts == ts and bar.instrument is INST


def test_quote_from_ticker_requires_bid_ask():
    ok = quote_from_ticker(SimpleNamespace(bid=99.0, ask=101.0, time=T0), INST)
    assert ok is not None and ok.mid == 100.0
    assert quote_from_ticker(SimpleNamespace(bid=None, ask=101.0), INST) is None
    assert quote_from_ticker(SimpleNamespace(bid=float("nan"), ask=101.0), INST) is None


async def test_replay_feed_emits_quote_then_bar():
    feed = ReplayFeed(_bars(3))
    seen = [type(ev).__name__ async for ev in feed.stream()]
    assert seen == ["QuoteEvent", "Bar", "QuoteEvent", "Bar", "QuoteEvent", "Bar"]


# --------------------------------------------------------------------------- run loop
async def test_runner_trades_and_books_match_broker():
    journal = InMemoryJournal()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal,
    )
    runner = LiveRunner(
        desk=desk, broker=broker, feed=ReplayFeed(_bars(200)),
        reconciler=Reconciler(broker, risk=risk), reconcile_every=25,
    )
    summary = await runner.run()

    assert summary.bars == 200
    assert summary.executed > 0
    assert len(journal) > 0
    # The runner's internal book must reconcile with the broker's actual positions (§17).
    positions = await broker.get_positions()
    assert diff_positions(summary.internal_book, positions).is_consistent
    assert summary.reconciliation_breaks == 0
    assert summary.reconciliations > 0


async def test_runner_respects_max_bars():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
    )
    runner = LiveRunner(desk=desk, broker=broker, feed=ReplayFeed(_bars(200)), max_bars=50)
    summary = await runner.run()
    assert summary.bars == 50


async def test_governance_takes_strategy_offline_mid_stream():
    """With a strict decay policy the monitor suspends the (only) strategy partway through the
    run; the desk then ignores it, so far fewer trades execute than without governance."""
    bars = _bars(200)

    # Baseline: no governance -> the strategy trades throughout.
    desk_a, broker_a, _ = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
    )
    base = await LiveRunner(desk=desk_a, broker=broker_a, feed=ReplayFeed(bars)).run()

    # Governed: a policy that suspends any strategy with trades (impossible profit-factor bar).
    registry = StrategyRegistry()
    registry.register("momentum")
    journal = InMemoryJournal()
    desk_b, broker_b, _ = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal, registry=registry,
    )
    monitor = GovernanceMonitor(registry, DecayPolicy(min_trades=1, min_profit_factor=1e9))
    governed = await LiveRunner(
        desk=desk_b, broker=broker_b, feed=ReplayFeed(bars),
        monitor=monitor, journal=journal, govern_every=20,
    ).run()

    assert registry.get("momentum").status is StrategyStatus.SUSPENDED
    assert "momentum" in governed.suspended
    assert governed.governance_actions > 0
    # Being taken offline mid-stream means strictly fewer executions than the ungoverned run.
    assert governed.executed < base.executed
