"""Demo: Statistical Arbitrage — a market-neutral pairs trade (§8).

    PYTHONPATH=src python3 examples/statarb_demo.py

Two cointegrated instruments whose spread oscillates. The StatArb engine tracks the hedge
ratio and the spread z-score; the specialist trades both legs — selling the rich one and
buying the cheap one — as the spread stretches, and unwinds as it converges. Numbers come from
the recorded run.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.backtest import Backtester
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.journal import InMemoryJournal, TradeAnalytics
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.stat_arb import Pair, StatArbEngine
from atp.strategy.stat_arb import StatArbStrategy

KO = Instrument("KO", AssetClass.EQUITY)
PEP = Instrument("PEP", AssetClass.EQUITY)   # the classic cointegrated pair
START = datetime(2026, 1, 5, tzinfo=timezone.utc)


def pair_bars(n: int = 320) -> list[Bar]:
    """A cointegrated pair moving ~1:1 (β≈1) on a strong shared factor, plus a mean-reverting
    spread of meaningful size — the tradable edge. With an accurate hedge the shared move
    cancels and the spread reversion is captured."""
    bars: list[Bar] = []
    for i in range(n):
        common = 100 + 0.04 * i + 5.0 * math.sin(i / 6.0)     # dominant shared co-movement
        ko = common
        pep = common + 10 + 3.0 * math.sin(i / 9.0)           # β≈1 + mean-reverting spread
        ts = START + timedelta(minutes=i)
        bars.append(Bar(KO, ko, ko * 1.001, ko * 0.999, ko, 1000, ts))
        bars.append(Bar(PEP, pep, pep * 1.001, pep * 0.999, pep, 1000, ts + timedelta(seconds=30)))
    return bars


async def main() -> None:
    engine = StatArbEngine([Pair(KO.key, PEP.key)], window=40, min_window=20)
    journal = InMemoryJournal()
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[StatArbStrategy(engine, entry_z=1.2, corr_min=0.3)],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        observers=[engine],
        journal=journal,
    )
    res = await bt.run(pair_bars(320))
    view = engine.assessment(PEP.key)

    print("=" * 64)
    print("  Statistical Arbitrage — KO / PEP pairs trade (§8)")
    print("=" * 64)
    if view is not None:
        print(f"  hedge ratio (beta)  : {view.beta:.2f}")
        print(f"  returns correlation : {view.correlation:+.2f}   (relationship intact)")
        print(f"  final spread z      : {view.z:+.2f}")
    print(f"  orders executed     : {res.n_executed}   blocked: {res.n_blocked}")
    for g in TradeAnalytics.from_journal(journal).by_strategy():
        print(f"  {g.label:<10} n={g.n_trades:>3}  win={g.win_rate:>4.0%}  "
              f"expectancy={g.expectancy:>+8.2f}  total={g.total_pnl:>+9.2f}")
    print("=" * 64)
    print("  Two legs, opposite directions, β-weighted so the shared move cancels — the P&L")
    print("  is the captured spread reversion (market-neutral pairs sizing, §8 / ADR-14).")


if __name__ == "__main__":
    asyncio.run(main())
