"""Runnable demo: drive the full desk pipeline over synthetic data and print an honest report.

    PYTHONPATH=src python3 examples/run_backtest.py

This is the same `AutonomousTradingDesk` the live loop would use, replayed over bars — the
core loop from the concept: features → regime → signals → opportunity → sizing → RISK → fill.
No fabricated numbers: everything printed is computed from the fed data (§25).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.backtest import Backtester, monte_carlo_trade_order
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy import MeanReversionStrategy, MomentumStrategy

INST = Instrument(symbol="DEMO", asset_class=AssetClass.EQUITY)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def synthetic_bars(n: int = 500) -> list[Bar]:
    """A trending market with cycles and a mild vol regime shift — enough to exercise
    both the momentum and mean-reversion specialists."""
    bars: list[Bar] = []
    for i in range(n):
        trend = 0.04 * i
        cycle = 6.0 * math.sin(i / 11.0)
        wobble = 1.5 * math.sin(i / 2.3)
        price = 100.0 + trend + cycle + wobble
        bars.append(
            Bar(INST, price, price * 1.003, price * 0.997, price, 1000 + (i % 50) * 20,
                START + timedelta(minutes=i))
        )
    return bars


async def main() -> None:
    policy = TradingPolicy(capital=100_000.0, risk_per_trade=0.01, daily_loss_limit=0.03)
    backtester = Backtester(
        policy=policy,
        strategies=[MomentumStrategy(), MeanReversionStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        spread_bps=2.0,
        slippage_bps=1.0,
    )
    ppy = 252 * 390  # minute bars
    result = await backtester.run(synthetic_bars(500), periods_per_year=ppy)
    m = result.metrics(periods_per_year=ppy)

    print("=" * 62)
    print("  Autonomous Multi-Asset Trading — backtest report")
    print("=" * 62)
    print(f"  bars            : {m.n_periods + 1}")
    print(f"  orders executed : {result.n_executed}   blocked by risk: {result.n_blocked}")
    print(f"  closed trades   : {m.n_trades}")
    print(f"  start / end eq  : {result.starting_equity:,.0f}  ->  {result.ending_equity:,.2f}")
    print(f"  total return    : {m.total_return:+.2%}")
    # Annualized figures are meaningless on a sub-year sample; don't print a misleading number.
    span_years = m.n_periods / ppy
    if span_years >= 1.0:
        print(f"  CAGR / vol      : {m.cagr:+.2%} / {m.volatility:.2%}")
        print(f"  Calmar          : {m.calmar:.2f}")
    else:
        print(f"  volatility (ann): {m.volatility:.2%}")
        print(f"  CAGR / Calmar   : n/a (sample is {span_years * 12:.1f} months; too short to annualize)")
    print(f"  Sharpe / Sortino: {m.sharpe:.2f} / {m.sortino:.2f}")
    print(f"  max drawdown    : {m.max_drawdown:.2%}")
    print(f"  win rate / PF   : {m.win_rate:.0%} / {m.profit_factor:.2f}")
    print(f"  expectancy/trade: {m.expectancy:+,.2f}")

    mc = monte_carlo_trade_order(result.trade_pnls, result.starting_equity, n_runs=5000)
    print("-" * 62)
    print("  Monte-Carlo (resampled trade order, 5000 runs):")
    print(f"    final equity  p05/p50/p95 : {mc.final_equity_p05:,.0f} / "
          f"{mc.final_equity_p50:,.0f} / {mc.final_equity_p95:,.0f}")
    print(f"    max drawdown  p50/p95      : {mc.max_drawdown_p50:.2%} / {mc.max_drawdown_p95:.2%}")
    print(f"    prob(end < start)          : {mc.prob_loss:.0%}")
    print("=" * 62)
    print("  NB: synthetic data. Not a performance claim — the machinery, not a promise.")


if __name__ == "__main__":
    asyncio.run(main())
