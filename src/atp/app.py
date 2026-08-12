"""Application assembly (§24): turn a `SystemConfig` into a runnable desk.

The capstone that ties the library into one program. `build_strategies` instantiates the
feature-only specialists named in the config; `run_backtest` assembles a `Backtester` (policy,
strategies, regime, execution) and runs it, capturing trades in a journal. Engine-backed
specialists (cross-asset, stat-arb, volatility, fx-carry, macro, event) need their shared data
engines and are wired programmatically — `build_strategies` says so clearly rather than
silently dropping them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backtest.engine import Backtester, BacktestResult
from .config import SystemConfig
from .core.events import Bar
from .execution.impact import MarketImpactModel
from .journal.store import InMemoryJournal, TradeJournal
from .strategy.base import Strategy
from .strategy.breakout import BreakoutStrategy
from .strategy.mean_reversion import MeanReversionStrategy
from .strategy.momentum import MomentumStrategy

# Specialists that need only features (no shared data engine) — configurable by name.
_SIMPLE_STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "breakout": BreakoutStrategy,
}
# Engine-backed specialists — recognized, but must be constructed with their engine.
_ENGINE_STRATEGIES = {"cross_asset", "stat_arb", "volatility", "fx_carry", "macro", "event"}


def build_strategies(config: SystemConfig) -> list[Strategy]:
    out: list[Strategy] = []
    for name in config.strategies:
        ctor = _SIMPLE_STRATEGIES.get(name)
        if ctor is not None:
            out.append(ctor())
        elif name in _ENGINE_STRATEGIES:
            raise ValueError(
                f"strategy '{name}' is engine-backed (needs its data engine, e.g. RatesTable / "
                f"OptionsEngine / StatArbEngine) — construct it programmatically, not by name"
            )
        else:
            raise ValueError(f"unknown strategy '{name}'")
    return out


@dataclass(slots=True)
class BacktestRun:
    result: BacktestResult
    journal: TradeJournal


def make_backtester(config: SystemConfig, *, journal: TradeJournal | None = None) -> Backtester:
    ex = config.execution
    impact = MarketImpactModel(eta_bps=ex.impact_eta_bps) if ex.impact_eta_bps else None
    return Backtester(
        policy=config.to_policy(),
        strategies=build_strategies(config),
        regime=config.to_regime(),
        spread_bps=ex.spread_bps,
        slippage_bps=ex.slippage_bps,
        commission_per_unit=ex.commission_per_unit,
        min_commission=ex.min_commission,
        impact_model=impact,
        execution_slices=ex.slices,
        vwap_profile=ex.vwap_profile,
        journal=journal,
    )


async def run_backtest(config: SystemConfig, bars: list[Bar],
                       periods_per_year: float = 252.0) -> BacktestRun:
    """Assemble the desk from `config` and replay `bars`, capturing trades in a journal."""
    journal = InMemoryJournal()
    bt = make_backtester(config, journal=journal)
    result = await bt.run(bars, periods_per_year)
    return BacktestRun(result=result, journal=journal)
