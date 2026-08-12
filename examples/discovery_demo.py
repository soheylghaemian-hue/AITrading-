"""Demo: Strategy Discovery + the mandatory validation gauntlet (§12/§13).

    PYTHONPATH=src python3 examples/discovery_demo.py

Enumerates candidate rule strategies, runs each through Backtest → Out-of-Sample →
Walk-Forward → Monte-Carlo, and reports which survive. A survivor is handed to the model
registry as a versioned, governed model (§19). The selection-bias caveat is printed, not
hidden (§13).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.discovery import DiscoveryCriteria, SearchSpace, StrategyDiscovery
from atp.governance import ModelRegistry, ModelVersion
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier

INST = Instrument("DEMO", AssetClass.EQUITY)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def trending_bars(n: int = 600) -> list[Bar]:
    """A drifting market with cycles — the kind of tape a trend rule can exploit."""
    bars = []
    for i in range(n):
        price = 100.0 + 0.06 * i + 5.0 * math.sin(i / 13.0) + 1.0 * math.sin(i / 3.0)
        bars.append(Bar(INST, price, price * 1.002, price * 0.998, price,
                        1000 + (i % 40) * 25, START + timedelta(minutes=i)))
    return bars


async def main() -> None:
    disc = StrategyDiscovery(
        policy=TradingPolicy(capital=100_000.0),
        criteria=DiscoveryCriteria(
            min_trades=4, min_oos_sharpe=0.0, min_oos_profit_factor=1.0,
            min_oos_return=0.0, max_mc_prob_loss=0.05, min_wf_win_fraction=0.5,
        ),
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        mc_runs=2000,
    )
    space = SearchSpace(feature_grid={
        "trend": [0.2, 0.3, 0.5],
        "momentum": [0.002, 0.005],
        "zscore": [1.0, 1.5],
    })

    result = await disc.discover(trending_bars(600), space)

    print("=" * 92)
    print("  Strategy Discovery — candidates through the validation gauntlet (§12/§13)")
    print("=" * 92)
    header = f"  {'candidate':<26} {'trades':>6} {'oos_ret':>8} {'oos_shrp':>8} {'PF':>5} {'wf_win':>6} {'mc_loss':>7}  verdict"
    print(header)
    print("  " + "-" * 88)
    for r in sorted(result.reports, key=lambda r: (r.passed, r.oos.sharpe), reverse=True):
        pf = "inf" if r.oos.profit_factor == float("inf") else f"{r.oos.profit_factor:.2f}"
        verdict = "PASS" if r.passed else "fail: " + ", ".join(r.failures[:2])
        print(f"  {r.name:<26} {r.oos.n_trades:>6} {r.oos.total_return:>+7.2%} "
              f"{r.oos.sharpe:>8.2f} {pf:>5} {r.wf_win_fraction:>5.0%} {r.mc_prob_loss:>6.0%}  {verdict}")

    print("  " + "-" * 88)
    print(f"  {result.selection_note}")

    best = result.best
    if best is not None:
        print("-" * 92)
        print(f"  best survivor: {best.name}")
        print(f"    params: {best.params}")
        registry = ModelRegistry()
        registry.set_baseline(ModelVersion(
            strategy=best.name, version="v1", params=best.params,
            metrics={"sharpe": best.oos.sharpe, "profit_factor": best.oos.profit_factor,
                     "total_return": best.oos.total_return},
        ))
        print(f"    -> registered as governed model {registry.current(best.name).version} (§19)")
    else:
        print("  No candidate survived the gauntlet — the honest outcome is often 'nothing yet'.")
    print("=" * 92)


if __name__ == "__main__":
    asyncio.run(main())
