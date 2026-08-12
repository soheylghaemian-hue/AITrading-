"""Demo: smart execution vs. one-shot, under a market-impact model (§16).

    PYTHONPATH=src python3 examples/execution_demo.py

Runs the identical strategy/data through two backtests that differ only in how orders are
worked: fire each order in one shot (ImmediateAlgo) vs. slice large orders by participation
rate (SlicingAlgo). Because market impact is convex in size, slicing pays less — visible as
higher ending equity. Same signals, same risk — only the execution differs.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.backtest import Backtester
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.execution.algo import ImmediateAlgo, SlicingAlgo
from atp.execution.impact import MarketImpactModel
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("THIN", AssetClass.EQUITY)   # a thinly-traded name => impact matters
START = datetime(2026, 1, 5, tzinfo=timezone.utc)


def bars(n: int = 300) -> list[Bar]:
    return [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                700, START + timedelta(minutes=i)) for i in range(n)]


async def run(algo, impact) -> tuple[int, float]:
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        impact_model=impact, execution_algo=algo,
    )
    res = await bt.run(bars())
    return res.n_executed, res.ending_equity


async def main() -> None:
    impact = MarketImpactModel(eta_bps=200)   # 200 bps at 100% participation
    n_imm, eq_imm = await run(ImmediateAlgo(), impact)
    n_slc, eq_slc = await run(SlicingAlgo(participation_cap=0.05, max_slices=8), impact)

    print("=" * 60)
    print("  Smart execution vs. one-shot, under market impact (§16)")
    print("=" * 60)
    print(f"  immediate : executed={n_imm:>3}   ending equity = {eq_imm:,.2f}")
    print(f"  sliced    : executed={n_slc:>3}   ending equity = {eq_slc:,.2f}")
    print("-" * 60)
    saved = eq_slc - eq_imm
    print(f"  execution cost saved by slicing: {saved:+,.2f}  ({saved / 100_000:+.3%} of capital)")
    print("=" * 60)
    print("  Impact is convex in size; working the order in slices pays less.")


if __name__ == "__main__":
    asyncio.run(main())
