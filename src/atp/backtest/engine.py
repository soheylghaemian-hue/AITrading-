"""Event-driven backtester (§11).

Design decision: the backtester drives the SAME `AutonomousTradingDesk` used live, over a
chronological replay of historical bars. Because entry/sizing/risk/execution all run
through the identical code path, a backtest cannot silently diverge from live behavior,
and there is no lookahead — the desk only ever sees bars up to the current timestamp.

Frictions modeled (via PaperBroker + a synthetic quote):
* commission (per-unit + minimum),
* bid/ask spread (configurable bps around each bar's close),
* slippage (bps, applied by the PaperBroker on fills).

Equity is marked-to-market after every bar (so `total_return` includes open positions).
`trade_pnls` collects realized P&L each time the net position is reduced/closed.

Latency and market-impact are intentionally simple here (documented in docs/DECISIONS.md);
they are extension points, not hidden assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..cross_asset.engine import CrossAssetEngine
    from ..execution.algo import ExecutionAlgo
    from ..execution.impact import MarketImpactModel
    from ..governance.registry import StrategyRegistry
    from ..journal import TradeJournal

from ..brokers.paper import PaperBroker
from ..core.events import Bar, QuoteEvent
from ..execution.engine import ExecutionEngine
from ..features.engine import FeatureEngine
from ..logging_config import get_logger
from ..opportunity.engine import OpportunityEngine
from ..opportunity.sizing import PositionSizer
from ..policy import TradingPolicy
from ..regime.classifier import RegimeClassifier
from ..risk.engine import RiskEngine, RiskState
from ..strategy.base import Strategy
from .metrics import BacktestMetrics, compute_metrics

log = get_logger("backtest")


@dataclass(slots=True)
class BacktestResult:
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)
    n_executed: int = 0
    n_blocked: int = 0
    starting_equity: float = 0.0
    ending_equity: float = 0.0

    def metrics(self, periods_per_year: float = 252.0) -> BacktestMetrics:
        return compute_metrics(self.equity_curve, self.trade_pnls, periods_per_year)


class Backtester:
    def __init__(
        self,
        policy: TradingPolicy,
        strategies: list[Strategy],
        *,
        spread_bps: float = 2.0,
        slippage_bps: float = 1.0,
        commission_per_unit: float = 0.005,
        min_commission: float = 1.0,
        respect_trading_hours: bool = False,
        opportunity: OpportunityEngine | None = None,
        sizer: PositionSizer | None = None,
        regime: RegimeClassifier | None = None,
        journal: "TradeJournal | None" = None,
        registry: "StrategyRegistry | None" = None,
        cross_asset: "CrossAssetEngine | None" = None,
        impact_model: "MarketImpactModel | None" = None,
        execution_algo: "ExecutionAlgo | None" = None,
        observers: list | None = None,
        execution_slices: int | None = None,
        vwap_profile: list[float] | None = None,
        calendar=None,
    ) -> None:
        self._policy = policy
        self._strategies = strategies
        self._spread_bps = spread_bps
        self._slippage_bps = slippage_bps
        self._commission_per_unit = commission_per_unit
        self._min_commission = min_commission
        self._respect_hours = respect_trading_hours
        self._opportunity = opportunity or OpportunityEngine(min_score=0.0)
        self._sizer = sizer or PositionSizer()
        self._regime = regime or RegimeClassifier()
        # Optional experience capture (§11): each completed round trip is recorded here.
        self._journal = journal
        # Optional governance gate (§19): suspended strategies are ignored during replay.
        self._registry = registry
        # Optional cross-asset intelligence (§6): fed every bar for cross-asset specialists.
        self._cross_asset = cross_asset
        # Optional smart execution (§16): market-impact model + slicing algo.
        self._impact_model = impact_model
        self._execution_algo = execution_algo
        # Optional per-bar observers (§8 StatArb pairs engine, etc.).
        self._observers = observers
        # Optional time-scheduled execution (§16): work entries as N slices over bars.
        self._execution_slices = execution_slices
        self._vwap_profile = vwap_profile
        # Optional market calendar (§3): session-hours + holiday gating.
        self._calendar = calendar

    def _effective_policy(self) -> TradingPolicy:
        if self._respect_hours:
            return self._policy
        # Replay historical bars without the wall-clock trading-hours gate; risk caps and
        # data-freshness still apply. Live operation always honors the real hours.
        return self._policy.model_copy(
            update={
                "trading_days": [0, 1, 2, 3, 4, 5, 6],
                "trading_start": "00:00",
                "trading_end": "23:59",
                "max_quote_age_seconds": 1e12,
            }
        )

    async def run(self, bars: list[Bar], periods_per_year: float = 252.0) -> BacktestResult:
        """Replay `bars` in the order given (must be chronological)."""
        # Local import avoids a circular import at module load time.
        from ..desk.desk import AutonomousTradingDesk

        # Reset stateful strategies so a reused instance can't leak state between runs
        # (in-sample / OOS / walk-forward windows) — see §13 and Strategy.reset().
        for strat in self._strategies:
            strat.reset()

        policy = self._effective_policy()
        broker = PaperBroker(
            starting_cash=policy.capital,
            commission_per_unit=self._commission_per_unit,
            min_commission=self._min_commission,
            slippage_bps=self._slippage_bps,
            impact_model=self._impact_model,
        )
        await broker.connect()
        risk = RiskEngine(
            limits=policy.to_risk_limits(),
            state=RiskState(day_start_equity=policy.capital, peak_equity=policy.capital),
        )
        execution = ExecutionEngine(broker, risk, autonomous=True, algo=self._execution_algo)
        scheduler = None
        if self._execution_slices or self._vwap_profile:
            from ..execution.scheduler import ExecutionScheduler
            scheduler = ExecutionScheduler(
                execution,
                slices=self._execution_slices or (len(self._vwap_profile) if self._vwap_profile else 4),
                volume_profile=self._vwap_profile,
            )
        desk = AutonomousTradingDesk(
            policy=policy,
            broker=broker,
            risk=risk,
            execution=execution,
            strategies=self._strategies,
            feature_engine=FeatureEngine(),
            regime=self._regime,
            opportunity=self._opportunity,
            sizer=self._sizer,
            journal=self._journal,
            registry=self._registry,
            cross_asset=self._cross_asset,
            observers=self._observers,
            scheduler=scheduler,
            calendar=self._calendar,
        )

        result = BacktestResult(starting_equity=policy.capital)
        prev_realized = 0.0

        for bar in bars:
            # Build a synthetic quote around the bar close, then advance the desk.
            half = bar.close * (self._spread_bps / 2 / 1e4)
            quote = QuoteEvent(
                instrument=bar.instrument,
                bid=bar.close - half,
                ask=bar.close + half,
                ts=bar.ts,
            )
            broker.set_quote(quote)
            desk.on_quote(quote)
            desk.on_bar(bar)

            report = await desk.step(now=bar.ts)
            result.n_executed += len(report.executed)
            result.n_blocked += len(report.blocked)

            # Realized P&L delta since last bar => a closed/reduced trade this step.
            realized = broker.realized_pnl
            if abs(realized - prev_realized) > 1e-12:
                result.trade_pnls.append(realized - prev_realized)
                prev_realized = realized

            account = await broker.get_account()
            result.equity_curve.append(account.equity)
            result.timestamps.append(bar.ts)

        result.ending_equity = result.equity_curve[-1] if result.equity_curve else policy.capital
        log.info(
            "backtest done | bars=%d executed=%d blocked=%d start=%.2f end=%.2f",
            len(bars),
            result.n_executed,
            result.n_blocked,
            result.starting_equity,
            result.ending_equity,
        )
        return result
