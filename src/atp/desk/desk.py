"""Autonomous Trading Desk — the orchestrator (§3/§6/§25 core loop).

This is the "virtual quantitative trading organization" from the concept: it runs the whole
reference workflow (§7) each step —

    features → regime → specialist signals → opportunity ranking →
    portfolio target → sizing → RISK VETO → execution → broker

— for every instrument that received fresh data. The Risk Engine is consulted on every
order and can veto it; nothing here can bypass it (§14). Cash is a valid outcome: if no
opportunity clears, the desk simply does nothing that step (§9).

The exact same object is driven by the live loop and by the backtester, so backtest and live
behavior cannot silently diverge (§13/§24 Phase 14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..brokers.base import Broker, Order
from ..core.enums import Action, OrderType, Side
from ..core.events import Bar, Instrument, QuoteEvent
from ..cross_asset.engine import CrossAssetEngine
from ..execution.engine import ExecutionEngine, ExecutionResult
from ..execution.scheduler import ExecutionScheduler
from ..features.engine import FeatureEngine
from ..governance.registry import StrategyRegistry
from ..journal import TradeAssembler, TradeContext, TradeJournal
from ..logging_config import get_logger
from ..opportunity.engine import OpportunityEngine
from ..opportunity.sizing import PositionSizer
from ..policy import TradingPolicy
from ..regime.classifier import RegimeClassifier
from ..risk.engine import RiskEngine
from ..strategy.base import Strategy

log = get_logger("desk")


@dataclass(slots=True)
class StepReport:
    ts: datetime
    executed: list[ExecutionResult] = field(default_factory=list)
    blocked: list[ExecutionResult] = field(default_factory=list)
    n_signals: int = 0
    skipped_reason: str = ""


class AutonomousTradingDesk:
    def __init__(
        self,
        *,
        policy: TradingPolicy,
        broker: Broker,
        risk: RiskEngine,
        execution: ExecutionEngine,
        strategies: list[Strategy],
        feature_engine: FeatureEngine,
        regime: RegimeClassifier,
        opportunity: OpportunityEngine,
        sizer: PositionSizer,
        min_trade_units: float = 1.0,
        assembler: TradeAssembler | None = None,
        journal: TradeJournal | None = None,
        registry: StrategyRegistry | None = None,
        cross_asset: CrossAssetEngine | None = None,
        observers: list | None = None,
        scheduler: ExecutionScheduler | None = None,
        data_quality=None,
        portfolio_manager=None,
        calendar=None,
        model_version: str = "v0",
    ) -> None:
        self._policy = policy
        self._broker = broker
        self._risk = risk
        self._execution = execution
        self._strategies = strategies
        self._features = feature_engine
        self._regime = regime
        self._opportunity = opportunity
        self._sizer = sizer
        self._min_trade_units = min_trade_units
        # Optional experience capture (§11). If a journal is given without an assembler, one is
        # created so the desk can turn fills into completed trade records on its own.
        self._journal = journal
        self._assembler = assembler or (TradeAssembler() if journal is not None else None)
        # Governance gate (§19): if present, a suspended strategy's signals are ignored live.
        self._registry = registry
        # Cross-asset intelligence (§6): updated with every bar; strategies read shared state.
        self._cross_asset = cross_asset
        # Generic per-bar observers (e.g. StatArb pairs engine): each gets update_bar(bar).
        self._observers = list(observers or [])
        # Optional time-scheduled execution (§16 VWAP/TWAP): entries are worked over bars.
        self._scheduler = scheduler
        # Optional Data Quality Engine (§10): validates market data before the desk acts on it.
        self._data_quality = data_quality
        # Optional Master Portfolio Manager (§9): portfolio-level allocation across opportunities.
        self._portfolio_manager = portfolio_manager
        # Optional MarketCalendar (§3): gates trading by real session hours + holidays.
        self._calendar = calendar
        self._model_version = model_version

        self._quotes: dict[str, QuoteEvent] = {}
        self._instruments: dict[str, Instrument] = {}
        self._last_volume: dict[str, float] = {}   # liquidity proxy for market impact (§16)
        self._pending: set[str] = set()

    # ------------------------------------------------------------- ingest
    def on_quote(self, quote: QuoteEvent) -> None:
        self._quotes[quote.instrument.key] = quote
        self._instruments[quote.instrument.key] = quote.instrument

    def on_bar(self, bar: Bar) -> None:
        self._features.update(bar)
        if self._cross_asset is not None:
            self._cross_asset.update_bar(bar)  # feed the Global Market Brain (§6)
        for obs in self._observers:
            obs.update_bar(bar)                # feed StatArb / other per-bar engines (§8)
        self._instruments[bar.instrument.key] = bar.instrument
        self._last_volume[bar.instrument.key] = bar.volume
        self._pending.add(bar.instrument.key)
        # Feed the path (MFE/MAE, bars held) of any open trade episode (§11).
        if self._assembler is not None:
            self._assembler.on_mark(bar.instrument.key, bar.close, bar.ts)

    # ------------------------------------------------------------- step
    async def step(self, *, now: datetime) -> StepReport:
        report = StepReport(ts=now)
        pending = self._pending
        self._pending = set()
        if not pending:
            return report

        account = await self._broker.get_account()
        self._risk.mark_equity(account.equity)

        # Release the next slice of any in-flight time-scheduled order (§16 VWAP/TWAP).
        if self._scheduler is not None and self._scheduler.has_work():
            account = await self._work_scheduler(account, now, report)

        # Protective flatten: once the Risk Engine has halted (daily-loss / drawdown breach),
        # actively close open positions rather than passively riding them. Reducing orders are
        # always approved (§14), and this overrides the trading-hours gate — cutting risk is
        # never off-hours (§18 disaster/robustness).
        if self._risk.state.halted:
            account = await self._flatten(account, now, report)
            report.skipped_reason = f"halted: {self._risk.state.halt_reason}"
            return report

        if not self._market_open(now):
            report.skipped_reason = "market closed (hours/holiday)"
            return report

        # 1) Gather opportunities across every instrument with fresh, quality data.
        scored: list[tuple] = []
        for key in pending:
            fs = self._features.latest(self._instruments[key])
            if fs is None or not fs.ready:
                continue
            if not self._data_ok(key, now):
                continue
            regime = self._regime.classify(fs)
            for strat in self._strategies:
                # §19: skip strategies governance has suspended (decay/rollback).
                if self._registry is not None and not self._registry.is_active(strat.name):
                    continue
                sig = strat.generate(fs, regime)
                if sig is not None:
                    scored.append((sig, fs.price))

        report.n_signals = len(scored)
        opportunities = self._opportunity.rank(scored)

        # 1b) Master Portfolio Manager decides which opportunities to fund given the whole book
        #     (portfolio budget, position count, diversification). Cash is a valid outcome (§9).
        if self._portfolio_manager is not None:
            decisions = self._portfolio_manager.allocate(opportunities, account)
            opportunities = [d.opportunity for d in decisions if d.allocate]

        # 2) Convert ranked opportunities into orders, best first, re-checking risk each time.
        for opp in opportunities:
            key = opp.instrument.key
            price = self._tradable_price(key, opp.price)
            equity = account.equity
            current_qty = account.positions[key].quantity if key in account.positions else 0.0

            target_qty = self._target_qty(opp, price, equity, current_qty)
            reduce_only = abs(target_qty) < abs(current_qty)

            # Count any in-flight scheduled quantity so we don't re-submit what's being worked;
            # when unwinding, cancel the working entry and size from the actual position.
            working = self._scheduler.working_qty(key) if self._scheduler is not None else 0.0
            if reduce_only and self._scheduler is not None:
                self._scheduler.cancel(key)
                working = 0.0
            delta = target_qty - current_qty - working
            if abs(delta) < self._min_trade_units:
                continue

            order = Order(
                instrument=opp.instrument,
                side=Side.BUY if delta > 0 else Side.SELL,
                quantity=abs(delta),
                order_type=OrderType.MARKET,
                reduce_only=reduce_only,
            )

            # Entries route through the time-scheduler (worked over bars); exits go immediately.
            if self._scheduler is not None and not reduce_only:
                decision = self._risk.check_order(
                    order, account, price=price, current_qty=current_qty,
                    stop_distance=opp.signal.stop_distance,
                )
                if not decision.approved:
                    report.blocked.append(ExecutionResult(order, decision))
                else:
                    self._scheduler.submit_parent(
                        order, price=price, stop_distance=opp.signal.stop_distance,
                        adv=self._liquidity(key), context=self._context_for(opp),
                    )
                continue

            exec_result = await self._execution.submit(
                order,
                account,
                price=price,
                current_qty=current_qty,
                stop_distance=opp.signal.stop_distance,
                adv=self._liquidity(key),
                urgency="high" if order.reduce_only else "normal",
            )
            if exec_result.filled:
                report.executed.append(exec_result)
                self._journal_fill(exec_result, self._context_for(opp))
                # Refresh so subsequent orders this step see updated exposure/equity.
                account = await self._broker.get_account()
            else:
                report.blocked.append(exec_result)

        return report

    async def _work_scheduler(self, account, now: datetime, report: StepReport):
        """Release due slices of in-flight scheduled orders and journal their fills (§16)."""
        results = await self._scheduler.tick(
            account, price_fn=lambda k: self._tradable_price(k, 0.0) or None, now=now
        )
        for exec_result, context in results:
            if exec_result.filled:
                report.executed.append(exec_result)
                self._journal_fill(exec_result, context)
                account = await self._broker.get_account()
            else:
                report.blocked.append(exec_result)
        return account

    async def _flatten(self, account, now: datetime, report: StepReport):
        """Submit reduce-only closing orders for every open position (§14 protective halt)."""
        for key, pos in list(account.positions.items()):
            if pos.quantity == 0:
                continue
            price = self._tradable_price(key, pos.market_price or pos.avg_price)
            order = Order(
                instrument=pos.instrument,
                side=Side.SELL if pos.quantity > 0 else Side.BUY,
                quantity=abs(pos.quantity),
                order_type=OrderType.MARKET,
                reduce_only=True,
            )
            exec_result = await self._execution.submit(
                order, account, price=price, current_qty=pos.quantity,
                adv=self._liquidity(key), urgency="high",
            )
            if exec_result.filled:
                report.executed.append(exec_result)
                # Exits are reductions; the assembler folds them into the open episode's record
                # (entry attribution was captured when the episode opened).
                self._journal_fill(exec_result, None)
                account = await self._broker.get_account()
            else:
                report.blocked.append(exec_result)
        return account

    # ------------------------------------------------------------- read model
    def latest_market(self) -> dict[str, dict]:
        """Current per-instrument view (regime + key features) for monitoring/dashboards (§22).

        Read-only; classifies each instrument's latest features on demand. Suspended-strategy
        gating is not applied here — this is observation, not a trading decision."""
        out: dict[str, dict] = {}
        for key, fs in self._features.all_latest().items():
            regime = self._regime.classify(fs)
            out[key] = {
                "regime": regime.value,
                "price": fs.price,
                "trend": fs.trend,
                "realized_vol": fs.realized_vol,
                "vol_percentile": fs.vol_percentile,
                "ready": fs.ready,
            }
        return out

    # ------------------------------------------------------------- journaling
    def _context_for(self, opp) -> TradeContext:
        sig = opp.signal
        return TradeContext(
            strategy=sig.strategy,
            regime=sig.regime.value,
            confidence=sig.confidence,
            expected_return=sig.expected_return,
            model_version=self._model_version,
            decision_price=opp.price,
            rationale=sig.rationale,
            features={"opportunity_score": opp.score, "reward_risk": opp.reward_risk},
            # full learning attribution (§1)
            agent=sig.strategy,                      # the specialist that opened it
            signal_action=sig.action.value,
            signal_strength=sig.confidence,
            expected_risk=sig.stop_distance,         # price-unit stop distance
            strategy_version=self._model_version,
        )

    def _journal_fill(self, exec_result: ExecutionResult, context: TradeContext | None) -> None:
        if self._assembler is None or exec_result.result is None or exec_result.result.fill is None:
            return
        record = self._assembler.on_fill(exec_result.result.fill, context)
        if record is not None and self._journal is not None:
            self._journal.record(record)

    # ------------------------------------------------------------- helpers
    def _data_ok(self, key: str, now: datetime) -> bool:
        """Data-quality gate (§10/§18): NO TRADE on bad data."""
        quote = self._quotes.get(key)
        if quote is None:
            return False
        # Full data-quality engine if configured; otherwise a basic staleness check.
        if self._data_quality is not None:
            result = self._data_quality.check_quote(quote, now)
            if not result.ok:
                log.warning("data-quality reject %s — %s", key, result.reason)
                return False
            return True
        age = (now - quote.ts).total_seconds()
        if age > self._policy.max_quote_age_seconds:
            log.warning("stale quote for %s (%.1fs) — skipping", key, age)
            return False
        return True

    def _market_open(self, now: datetime) -> bool:
        """Trading-session gate (§3): a MarketCalendar (hours + holidays) if configured, else
        the policy's trading-hours window."""
        if self._calendar is not None:
            return self._calendar.is_open(now)
        return self._policy.within_trading_hours(now)

    def _tradable_price(self, key: str, fallback: float) -> float:
        quote = self._quotes.get(key)
        return quote.mid if quote else fallback

    def _liquidity(self, key: str) -> float | None:
        """Latest volume as the impact model's liquidity reference; also pushed to the broker
        so paper fills can model size-dependent impact (§16). No-op without volume data."""
        adv = self._last_volume.get(key)
        if adv and hasattr(self._broker, "set_liquidity"):
            self._broker.set_liquidity(key, adv)  # type: ignore[attr-defined]
        return adv

    def _target_qty(self, opp, price: float, equity: float, current_qty: float) -> float:
        action = opp.signal.action
        if action is Action.CLOSE:
            return 0.0
        units = self._sizer.target_units(
            price=price,
            stop_distance=opp.signal.stop_distance,
            equity=equity,
            policy=self._policy,
            multiplier=opp.instrument.multiplier,
            sizing=opp.signal.sizing,
            hedge_factor=opp.signal.hedge_factor,
            ref_price=opp.signal.ref_price,
        )
        return opp.signal.direction * units
