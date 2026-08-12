"""Demo: model governance closing the learning loop (§19).

    PYTHONPATH=src python3 examples/governance_demo.py

Two parts:
  A. Decay -> suspension. From a journal of recorded experience, the monitor evaluates each
     strategy and takes the failing one offline automatically (the desk would then ignore it).
  B. Versioning. A new model version is adopted only after a *validated* improvement, and a
     decayed model is rolled back to its previous version.

Everything is driven by recorded numbers — no invented edge (§25).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from atp.brokers.base import Fill
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument
from atp.governance import (
    DecayPolicy,
    GovernanceMonitor,
    ModelRegistry,
    ModelVersion,
    PromotionPolicy,
    StrategyRegistry,
)
from atp.journal import InMemoryJournal, TradeAnalytics, TradeAssembler, TradeContext

INST = Instrument("DEMO", AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_journal() -> InMemoryJournal:
    """Synthetic experience: 'momentum' earns, 'fade' bleeds — recorded like real trades."""
    journal = InMemoryJournal()
    a = TradeAssembler()

    def trade(strategy, regime, pnl, i):
        a.on_fill(Fill(INST, Side.BUY, 1, 100.0, 0.0, T0 + timedelta(minutes=2 * i)),
                  TradeContext(strategy=strategy, regime=regime))
        rec = a.on_fill(Fill(INST, Side.SELL, 1, 100.0 + pnl, 0.0, T0 + timedelta(minutes=2 * i + 1)), None)
        journal.record(rec)

    for i, pnl in enumerate([12, -4, 9, -3, 15, 8, -2, 11, 7, -5, 10, 6, 9, -3]):
        trade("momentum", "trending_up", pnl, i)
    for i, pnl in enumerate([-6, 2, -8, -3, -5, 1, -7, -4, -6, -2, -5, -9, -3, -4], start=100):
        trade("fade", "trending_up", pnl, i)
    return journal


def part_a() -> None:
    print("=" * 72)
    print("  A. Decay monitor — observe → evaluate → act (§19)")
    print("=" * 72)
    journal = build_journal()
    for g in TradeAnalytics.from_journal(journal).by_strategy():
        print(f"  {g.label:<10} n={g.n_trades:>2}  win={g.win_rate:>4.0%}  "
              f"expectancy={g.expectancy:>+6.2f}  total_pnl={g.total_pnl:>+7.2f}")

    registry = StrategyRegistry()
    registry.register("momentum")
    registry.register("fade")
    monitor = GovernanceMonitor(registry, DecayPolicy(min_trades=10, min_expectancy=0.0))

    print("  --- running governance ---")
    for d in monitor.evaluate(journal):
        print(f"  {d.action.upper():<10} {d.name}: {d.reason}")

    print("  registry after governance:")
    for s in registry.states():
        print(f"    {s.name:<10} -> {s.status.value}")
    print(f"  the desk will now trade: {[s.name for s in registry.states() if s.tradable]}")


def part_b() -> None:
    print("\n" + "=" * 72)
    print("  B. Model versioning — promote only on validated improvement (§19)")
    print("=" * 72)
    mr = ModelRegistry(PromotionPolicy(primary_metric="sharpe", min_improvement=0.10))

    def show(res):
        arrow = "✓ PROMOTED" if res.promoted else "✗ rejected"
        print(f"  {arrow}: {res.reason}  |  active={mr.current('momentum').version}")

    show(mr.promote(ModelVersion("momentum", "v1", metrics={"sharpe": 1.00})))  # baseline
    show(mr.promote(ModelVersion("momentum", "v2", metrics={"sharpe": 1.06})))  # too small
    show(mr.promote(ModelVersion("momentum", "v3", metrics={"sharpe": 1.45})))  # clear win

    prev = mr.rollback("momentum")  # v3 decayed in production -> revert
    print(f"  ROLLBACK after decay -> active={prev.version}")
    print("=" * 72)


if __name__ == "__main__":
    part_a()
    part_b()
