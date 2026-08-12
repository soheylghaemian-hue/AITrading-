"""Demo: the live/paper run loop with reconciliation and live governance (§17/§19).

    PYTHONPATH=src python3 examples/live_demo.py

Streams bars through the SAME desk the backtester uses (here off a deterministic ReplayFeed +
PaperBroker; swap in IBKRMarketFeed + IBKRBroker for real paper trading). Every so often it
reconciles its internal book against the broker (§17) and runs the governance monitor over the
journal (§19) — so a strategy that decays is taken offline mid-stream. No invented numbers.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.brokers.reconcile import Reconciler
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.governance import DecayPolicy, GovernanceMonitor, StrategyRegistry
from atp.journal import InMemoryJournal, TradeAnalytics
from atp.live import LiveRunner, ReplayFeed, build_paper_stack
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy import MeanReversionStrategy, MomentumStrategy

INST = Instrument("DEMO", AssetClass.EQUITY)
START = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday


def stream_bars(n: int = 500) -> list[Bar]:
    bars = []
    for i in range(n):
        price = 100.0 + 0.05 * i + 5.0 * math.sin(i / 12.0) + 1.2 * math.sin(i / 2.7)
        bars.append(Bar(INST, price, price * 1.002, price * 0.998, price,
                        1000 + (i % 40) * 25, START + timedelta(minutes=i)))
    return bars


async def main() -> None:
    journal = InMemoryJournal()
    registry = StrategyRegistry()
    registry.register("momentum")
    registry.register("mean_reversion")

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MomentumStrategy(), MeanReversionStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal, registry=registry,
    )

    runner = LiveRunner(
        desk=desk, broker=broker, feed=ReplayFeed(stream_bars(500)),
        reconciler=Reconciler(broker, risk=risk), reconcile_every=50,
        monitor=GovernanceMonitor(registry, DecayPolicy(min_trades=6, min_expectancy=0.0)),
        journal=journal, govern_every=100,
    )
    summary = await runner.run()
    account = await broker.get_account()

    print("=" * 70)
    print("  Live/paper run — same desk as the backtester, off a market feed (§17)")
    print("=" * 70)
    print(f"  bars streamed     : {summary.bars}   quotes: {summary.quotes}")
    print(f"  orders executed   : {summary.executed}   blocked by risk: {summary.blocked}")
    print(f"  ending equity     : {account.equity:,.2f}   realized P&L: {account.realized_pnl:+,.2f}")
    print(f"  reconciliations   : {summary.reconciliations}   breaks: {summary.reconciliation_breaks}")
    print(f"  governance runs   : {summary.governance_runs}   actions: {summary.governance_actions}")
    print(f"  suspended live    : {summary.suspended or '—'}")
    print(f"  internal book     : {summary.internal_book or '—  (flat)'}")
    print("-" * 70)
    print("  strategy status after the run:")
    for s in registry.states():
        print(f"    {s.name:<16} {s.status.value:<10} {s.reason}")
    print("-" * 70)
    print("  journal edge by strategy (§11):")
    for g in TradeAnalytics.from_journal(journal).by_strategy():
        print(f"    {g.label:<16} n={g.n_trades:>3}  win={g.win_rate:>4.0%}  "
              f"expectancy={g.expectancy:>+7.2f}  total={g.total_pnl:>+9.2f}")
    print("=" * 70)
    print("  Swap ReplayFeed->IBKRMarketFeed and PaperBroker->IBKRBroker for real paper (§17).")


if __name__ == "__main__":
    asyncio.run(main())
