"""Convenience assembly of a paper-trading stack (§14/§24 Phase 14).

Wires a `PaperBroker` + `RiskEngine` + `ExecutionEngine` + `AutonomousTradingDesk` with the
optional journal/governance hooks, so callers (the live runner, tests, demos) don't repeat the
boilerplate. The live path swaps `PaperBroker`/`ReplayFeed` for `IBKRBroker`/`IBKRMarketFeed`;
nothing else changes (§3).
"""

from __future__ import annotations

import math

from ..brokers.paper import PaperBroker
from ..cross_asset.engine import CrossAssetEngine
from ..desk.desk import AutonomousTradingDesk
from ..execution.algo import ExecutionAlgo, ImmediateAlgo, SlicingAlgo
from ..execution.engine import ExecutionEngine
from ..execution.impact import MarketImpactModel
from ..features.engine import FeatureEngine
from ..governance.registry import StrategyRegistry
from ..journal.store import TradeJournal
from ..opportunity.engine import OpportunityEngine
from ..opportunity.sizing import PositionSizer
from ..policy import TradingPolicy
from ..regime.classifier import RegimeClassifier
from ..risk.engine import RiskEngine, RiskState
from ..strategy.base import Strategy


async def build_paper_stack(
    *,
    policy: TradingPolicy,
    strategies: list[Strategy],
    regime: RegimeClassifier | None = None,
    opportunity: OpportunityEngine | None = None,
    sizer: PositionSizer | None = None,
    journal: TradeJournal | None = None,
    registry: StrategyRegistry | None = None,
    cross_asset: CrossAssetEngine | None = None,
    observers: list | None = None,
    impact_model: MarketImpactModel | None = None,
    execution_algo: ExecutionAlgo | None = None,
    execution_slices: int | None = None,
    vwap_profile: list[float] | None = None,
    calendar=None,
    commission_per_unit: float = 0.005,
    min_commission: float = 1.0,
    slippage_bps: float = 1.0,
    autonomous: bool = True,
    risk: RiskEngine | None = None,
) -> tuple[AutonomousTradingDesk, PaperBroker, RiskEngine]:
    """Return a connected (desk, broker, risk) ready to hand to a LiveRunner."""
    broker = PaperBroker(
        policy.capital,
        commission_per_unit=commission_per_unit,
        min_commission=min_commission,
        slippage_bps=slippage_bps,
        impact_model=impact_model,
    )
    await broker.connect()
    if execution_algo is not None and type(execution_algo) not in (ImmediateAlgo, SlicingAlgo):
        raise ValueError("paper execution algorithm must be an exact vetted built-in")
    if risk is None:
        risk = RiskEngine(
            limits=policy.to_risk_limits(),
            state=RiskState(day_start_equity=policy.capital, peak_equity=policy.capital),
        )
    else:
        if type(risk) is not RiskEngine:
            raise ValueError("injected paper risk must be exact RiskEngine")
        limits = risk.limits
        expected = policy.to_risk_limits()
        bound_names = (
            "max_capital",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "max_position_pct",
            "max_gross_leverage",
            "max_open_positions",
            "max_trade_risk_pct",
            "max_portfolio_risk_pct",
        )
        invalid = any(
            (
                type(getattr(limits, name, None)) is not int
                or getattr(limits, name) <= 0
            )
            if name == "max_open_positions"
            else (
                isinstance(getattr(limits, name, None), bool)
                or not isinstance(getattr(limits, name, None), (int, float))
                or not math.isfinite(getattr(limits, name))
                or getattr(limits, name) <= 0
            )
            for name in bound_names
        )
        if invalid or any(
            getattr(limits, name, None) != getattr(expected, name) for name in bound_names
        ):
            raise ValueError("injected RiskEngine is not bound to the paper policy")
    execution = ExecutionEngine(broker, risk, autonomous=autonomous, algo=execution_algo)
    scheduler = None
    if execution_slices or vwap_profile:
        from ..execution.scheduler import ExecutionScheduler
        scheduler = ExecutionScheduler(
            execution,
            slices=execution_slices or (len(vwap_profile) if vwap_profile else 4),
            volume_profile=vwap_profile,
        )
    desk = AutonomousTradingDesk(
        policy=policy,
        broker=broker,
        risk=risk,
        execution=execution,
        strategies=strategies,
        feature_engine=FeatureEngine(),
        regime=regime or RegimeClassifier(),
        opportunity=opportunity or OpportunityEngine(min_score=0.0),
        sizer=sizer or PositionSizer(),
        journal=journal,
        registry=registry,
        cross_asset=cross_asset,
        observers=observers,
        scheduler=scheduler,
        calendar=calendar,
    )
    return desk, broker, risk
