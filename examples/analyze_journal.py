"""Demo: run the desk with an experience journal, then analyze it (§11).

    PYTHONPATH=src python3 examples/analyze_journal.py

Records every completed trade to a SQLite journal during a backtest, then prints the §11
question — "why do trades work or fail?" — as an expectancy/win-rate breakdown by strategy
and by regime. Every number comes from recorded trades; nothing is invented.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.backtest import Backtester
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.journal import SQLiteJournal, TradeAnalytics
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy import MeanReversionStrategy, MomentumStrategy

INST = Instrument("DEMO", AssetClass.EQUITY)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def synthetic_bars(n: int = 600) -> list[Bar]:
    bars = []
    for i in range(n):
        price = 100.0 + 0.03 * i + 6.0 * math.sin(i / 11.0) + 1.5 * math.sin(i / 2.3)
        bars.append(Bar(INST, price, price * 1.003, price * 0.997, price,
                        1000 + (i % 50) * 20, START + timedelta(minutes=i)))
    return bars


def _row(g) -> str:
    pf = "inf" if g.profit_factor == float("inf") else f"{g.profit_factor:.2f}"
    return (f"  {g.label:<26} n={g.n_trades:>3}  win={g.win_rate:>4.0%}  "
            f"PF={pf:>5}  exp={g.expectancy:>+8.2f}  pnl={g.total_pnl:>+9.2f}  "
            f"calib={g.calibration:>+.4f}")


async def main() -> None:
    journal = SQLiteJournal(":memory:")  # use a file path to persist across runs
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MomentumStrategy(), MeanReversionStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal,
    )
    await bt.run(synthetic_bars(600))

    an = TradeAnalytics.from_journal(journal)
    overall = an.overall()

    print("=" * 78)
    print("  Experience journal — why trades worked or failed (§11)")
    print("=" * 78)
    print(f"  recorded trades : {overall.n_trades}")
    print(f"  overall         : win={overall.win_rate:.0%}  expectancy={overall.expectancy:+.2f}  "
          f"total_pnl={overall.total_pnl:+.2f}")
    print(f"  avg MFE / MAE   : {overall.avg_mfe:+.2%} / {overall.avg_mae:+.2%}  "
          f"avg hold={overall.avg_holding_bars:.1f} bars")
    print(f"  calibration     : realized−expected return = {overall.calibration:+.4f}")
    print("-" * 78)
    print("  by strategy:")
    for g in an.by_strategy():
        print(_row(g))
    print("  by regime:")
    for g in an.by_regime():
        print(_row(g))
    print("=" * 78)
    print("  (calibration < 0 => the signal promised more than it delivered — a decay signal.)")
    journal.close()


if __name__ == "__main__":
    asyncio.run(main())
