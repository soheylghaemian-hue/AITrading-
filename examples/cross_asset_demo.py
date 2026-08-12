"""Demo: Cross-Asset Intelligence — the Global Market Brain (§6).

    PYTHONPATH=src python3 examples/cross_asset_demo.py

Two related instruments (a leader OIL and a follower XLE energy ETF, expected to move together).
The follower oscillates around the leader-implied path; the CrossAssetEngine measures their
correlation and divergence, and the cross-asset specialist trades the divergence back toward
convergence — only while the relationship still holds. Numbers come from the recorded run.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.backtest import Backtester
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.cross_asset import CrossAssetEngine, Relationship
from atp.journal import InMemoryJournal, TradeAnalytics
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.cross_asset import CrossAssetStrategy

OIL = Instrument("OIL", AssetClass.COMMODITY)
XLE = Instrument("XLE", AssetClass.ETF)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday


def interleaved_bars(n: int = 260) -> list[Bar]:
    """Leader + follower in lockstep; follower = fair value × an oscillating divergence."""
    bars: list[Bar] = []
    for i in range(n):
        fair = 100.0 + 0.05 * i + 4.0 * math.sin(i / 15.0)   # shared path (the relationship)
        lead = fair
        foll = fair * (1 + 0.04 * math.sin(i / 8.0))          # follower decouples & reconverges
        ts = T0 + timedelta(minutes=i)
        bars.append(Bar(OIL, lead, lead * 1.001, lead * 0.999, lead, 1000, ts))
        bars.append(Bar(XLE, foll, foll * 1.001, foll * 0.999, foll, 1000, ts + timedelta(seconds=30)))
    return bars


async def main() -> None:
    engine = CrossAssetEngine([Relationship(OIL.key, XLE.key, expected_sign=+1)],
                              window=30, min_window=15)
    journal = InMemoryJournal()
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[CrossAssetStrategy(engine, entry_z=1.0)],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal,
        cross_asset=engine,
    )
    res = await bt.run(interleaved_bars(260))

    view = engine.assessment(XLE)
    print("=" * 68)
    print("  Cross-Asset Intelligence — OIL → XLE (§6)")
    print("=" * 68)
    if view is not None:
        print(f"  realized correlation : {view.correlation:+.2f}  over {view.n} returns")
        print(f"  leader move (cum)    : {view.leader_cum:+.2%}")
        print(f"  follower move (cum)  : {view.follower_cum:+.2%}   implied: {view.implied:+.2%}")
        print(f"  divergence (z)       : {view.divergence_z:+.2f}   "
              f"{'CONFIRMING' if view.confirming else 'DIVERGING'}")
    print("-" * 68)
    print(f"  orders executed      : {res.n_executed}   blocked: {res.n_blocked}")
    for g in TradeAnalytics.from_journal(journal).by_strategy():
        print(f"  {g.label:<14} n={g.n_trades:>3}  win={g.win_rate:>4.0%}  "
              f"expectancy={g.expectancy:>+8.2f}  total={g.total_pnl:>+9.2f}")
    print("=" * 68)
    print("  The desk reads relationships between markets, not just single instruments.")


if __name__ == "__main__":
    asyncio.run(main())
