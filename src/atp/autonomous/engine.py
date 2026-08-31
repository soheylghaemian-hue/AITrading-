"""PaperAutonomousEngine — armed/dry-run/running state machine with a data-quality gate,
two-step activation, audit log and an observable decision feed (§ Phase 8.5).

Drives the EXISTING AutonomousTradingDesk. It NEVER sends an order to IBKR: execution is the
internal PaperBroker (realistic bid/ask/spread/slippage/commission). The Risk Engine is the sole
authority; the daily-loss lock holds until the next day; the kill switch is final. There is no
automatic PAPER→LIVE promotion. Default state: DISABLED.

States:
  DISABLED  — nothing runs.
  ARMED     — computes AI/market decisions and logs them, but places NO paper orders.
  DRY_RUN   — same as ARMED (observe only), an explicit "PAPER DRY RUN · NO ORDERS" mode.
  RUNNING   — executes internal paper trades.
  HALTED    — daily-loss lock: no new trades (decisions still logged).
  KILLED    — kill switch: no trades and no re-activation without an explicit reset.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from threading import Condition, RLock

from ..brokers.base import Account
from ..brokers.paper import PaperBroker
from ..core.events import Bar, QuoteEvent
from ..desk.desk import AutonomousTradingDesk
from ..execution.algo import ImmediateAlgo, SlicingAlgo
from ..execution.engine import ExecutionAuthority, ExecutionEngine
from ..execution.scheduler import ExecutionScheduler
from ..policy import TradingPolicy
from ..risk.config import TradingRiskConfig
from ..risk.engine import RiskDecision, RiskEngine, RiskState


class AutonomousStatus(str, Enum):
    DISABLED = "DISABLED"
    ARMED = "ARMED"
    DRY_RUN = "DRY_RUN"
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    KILLED = "KILLED"


_ACTIVE_INTENTS = {AutonomousStatus.ARMED, AutonomousStatus.DRY_RUN, AutonomousStatus.RUNNING}


@dataclass(slots=True)
class Decision:
    ts: str
    instrument: str
    agent: str | None = None
    action: str | None = None
    signal_strength: float | None = None
    confidence: float | None = None
    expected_risk: float | None = None
    suggested_size: float | None = None
    approved_size: float | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_decision: str | None = None
    execution_decision: str = ""
    reason: str = ""
    # § Phase 11 — richer dry-run observability
    source: str | None = None            # data source (MASSIVE / IDEALPRO / …)
    data_status: str | None = None       # market-data status the decision was based on
    regime: str | None = None            # market regime at decision time
    consensus: str | None = None         # AI agent consensus, e.g. "7/9 BUY"
    opportunity_score: float | None = None
    final_decision: str | None = None    # NO_DATA / NO_TRADE / REJECTED_BY_RISK / PAPER_TRADE_WOULD_BE_EXECUTED
    # § Phase 11.5 — explicit monetary risk (never conflate stop distance with dollar risk)
    position_notional: float | None = None
    stop_distance: float | None = None       # per-unit distance in account currency
    monetary_risk: float | None = None       # qty × stop_distance × multiplier
    risk_pct_capital: float | None = None     # monetary_risk / equity
    max_allowed_risk: float | None = None     # risk_per_trade × equity (hard cap)
    remaining_daily_budget: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class AuditEntry:
    actor: str
    ts: str
    prev: str
    new: str
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperAutonomousEngine:
    #: The exact confirmation the client must send to move ARMED → RUNNING (two-step activation).
    CONFIRM_PHRASE = "YES, START PAPER TRADING"

    def __init__(self, *, desk, broker, risk, journal=None, mode: str = "paper",
                 max_decisions: int = 300, max_audit: int = 200):
        if type(mode) is not str or mode != "paper":
            raise ValueError("PaperAutonomousEngine mode must be exactly 'paper'")
        self._desk = desk
        self._broker = broker
        self._risk = risk
        self._journal = journal
        self._mode = mode
        self._active_risk_binding: object | None = None
        self._execution_authority: ExecutionAuthority | None = None
        self._broker_order_guard: object | None = None
        self._canonical_scheduler: ExecutionScheduler | None = None
        self._boundary = Condition(RLock())
        self._boundary_epoch = 0
        self._inflight_orders = 0
        self._desk_cycle_active = False
        self._cycle_epoch: ContextVar[int | None] = ContextVar(
            f"paper_cycle_epoch_{id(self)}", default=None
        )
        self._cycle_task: ContextVar[asyncio.Task | None] = ContextVar(
            f"paper_cycle_task_{id(self)}", default=None
        )
        # Store one stable bound-method object so identity can itself be attested.
        self._order_guard = self._execution_permit
        self._schedule_guard = self._scheduling_permit
        self._intent = AutonomousStatus.DISABLED
        self._decisions: deque[Decision] = deque(maxlen=max_decisions)
        self._audit: deque[AuditEntry] = deque(maxlen=max_audit)
        self._trades_today = 0
        self._day: date | None = None
        self._error: str | None = None
        self._last_start_reasons: list[str] = []
        self._dry_run_until: datetime | None = None   # controlled observation window (auto-stop)
        self._eval_count = 0                           # total evaluation cycles run
        self._obs_count = 0                            # read-only observation cycles (no execution)
        self._observed: set[str] = set()               # instruments the engine has consumed
        self._journal_path: str | None = None          # optional append-only JSONL decision journal live

        reasons = self._paper_wiring_reasons(require_guard=False)
        if reasons:
            raise ValueError("unsafe paper wiring: " + "; ".join(reasons))
        execution = self._desk._execution  # noqa: SLF001
        algo = execution._algo  # noqa: SLF001
        canonical_algo = (
            ImmediateAlgo()
            if type(algo) is ImmediateAlgo
            else SlicingAlgo(
                participation_cap=algo._cap,  # noqa: SLF001
                max_slices=algo._max_slices,  # noqa: SLF001
            )
        )
        authority = ExecutionAuthority(self._broker, self._risk, canonical_algo, True)
        self._execution_authority = execution.bind_order_guard(
            self._order_guard, authority=authority
        )
        self._broker_order_guard = execution._broker_guard  # noqa: SLF001
        self._broker.bind_order_guard(self._broker_order_guard)
        scheduler = self._desk._scheduler  # noqa: SLF001
        self._canonical_scheduler = scheduler
        if type(scheduler) is ExecutionScheduler:
            scheduler.bind_work_guard(self._schedule_guard)
        reasons = self._paper_wiring_reasons()
        if reasons:
            raise ValueError("unsafe paper wiring: " + "; ".join(reasons))

    @property
    def mode(self) -> str:
        """The immutable execution profile. This engine has no live mode."""
        return self._mode

    # ------------------------------------------------------------- paper boundary
    @staticmethod
    def _finite_positive(value: object) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        )

    def _policy_risk_reasons(self, policy: TradingPolicy, risk: RiskEngine) -> list[str]:
        try:
            expected = policy.to_risk_limits()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return ["paper policy cannot produce valid RiskEngine limits"]
        limits = risk.limits
        names = (
            "max_capital",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "max_position_pct",
            "max_gross_leverage",
            "max_open_positions",
            "max_trade_risk_pct",
            "max_portfolio_risk_pct",
        )
        reasons: list[str] = []
        for name in names:
            actual = getattr(limits, name, None)
            wanted = getattr(expected, name, None)
            if name == "max_open_positions":
                if type(actual) is not int or type(wanted) is not int or actual <= 0 or wanted <= 0:
                    reasons.append("policy/RiskEngine max-open-positions is invalid")
                    continue
            elif not self._finite_positive(actual) or not self._finite_positive(wanted):
                reasons.append(f"policy/RiskEngine {name} is not finite positive")
                continue
            if actual != wanted:
                reasons.append(f"policy {name} is not bound to RiskEngine")
        for name in ("max_correlated_exposure_pct", "correlation_threshold"):
            if not self._finite_positive(getattr(limits, name, None)):
                reasons.append(f"RiskEngine {name} is not finite positive")
        if policy.risk_per_trade > policy.daily_loss_limit:
            reasons.append("policy risk-per-trade exceeds daily-loss limit")
        return reasons

    def _paper_wiring_reasons(self, *, require_guard: bool = True) -> list[str]:
        """Verify the complete order-capable object graph, not just the display broker."""
        reasons: list[str] = []
        if type(self._mode) is not str or self._mode != "paper":
            reasons.append("mode is not exact paper")
        if type(self._broker) is not PaperBroker:
            reasons.append("execution broker is not exact PaperBroker")
        else:
            if not self._broker.is_connected():
                reasons.append("PaperBroker is disconnected")
            if require_guard and (
                self._broker._order_guard_bound is not True  # noqa: SLF001
                or self._broker._order_guard is not self._broker_order_guard  # noqa: SLF001
            ):
                reasons.append("PaperBroker order guard is not execution final-send guard")
        if type(self._risk) is not RiskEngine:
            reasons.append("risk authority is not exact RiskEngine")
        else:
            state = self._risk.state
            if type(state) is not RiskState:
                reasons.append("risk state is not exact RiskState")
            else:
                for name in ("day_start_equity", "peak_equity"):
                    if not self._finite_positive(getattr(state, name, None)):
                        reasons.append(f"risk state {name} is not finite positive")
                for name in ("halted", "broker_connected", "killed"):
                    if type(getattr(state, name, None)) is not bool:
                        reasons.append(f"risk state {name} is not exact bool")
                if state.broker_connected is not True:
                    reasons.append("risk state broker connection is not healthy")
                for name in ("halt_reason", "kill_reason"):
                    if type(getattr(state, name, None)) is not str:
                        reasons.append(f"risk state {name} is not exact string")
        if type(self._desk) is not AutonomousTradingDesk:
            reasons.append("desk is not exact AutonomousTradingDesk")
            return reasons

        execution = self._desk._execution  # noqa: SLF001 - boundary attestation
        policy = self._desk._policy  # noqa: SLF001 - boundary attestation
        scheduler = self._desk._scheduler  # noqa: SLF001 - boundary attestation
        if require_guard and scheduler is not self._canonical_scheduler:
            reasons.append("desk scheduler is not the canonical paper scheduler")
        if self._desk._broker is not self._broker:  # noqa: SLF001
            reasons.append("desk broker is not engine PaperBroker")
        if self._desk._risk is not self._risk:  # noqa: SLF001
            reasons.append("desk risk authority is not engine RiskEngine")
        if type(execution) is not ExecutionEngine:
            reasons.append("desk execution is not exact ExecutionEngine")
        else:
            if execution._broker is not self._broker:  # noqa: SLF001
                reasons.append("execution broker is not engine PaperBroker")
            if execution._risk is not self._risk:  # noqa: SLF001
                reasons.append("execution risk authority is not engine RiskEngine")
            if execution._autonomous is not True:  # noqa: SLF001
                reasons.append("desk execution is not autonomous paper execution")
            if require_guard and execution._order_guard is not self._order_guard:  # noqa: SLF001
                reasons.append("execution order guard is not engine boundary guard")
            if require_guard:
                authority = execution._authority  # noqa: SLF001
                if authority is not self._execution_authority:
                    reasons.append("execution canonical authority changed")
                elif type(authority) is not ExecutionAuthority:
                    reasons.append("execution canonical authority is invalid")
                else:
                    if authority.broker is not self._broker:
                        reasons.append("canonical execution broker is not engine PaperBroker")
                    if authority.risk is not self._risk:
                        reasons.append("canonical execution risk is not engine RiskEngine")
                    if authority.autonomous is not True:
                        reasons.append("canonical execution authority is not autonomous")
            algo = execution._algo  # noqa: SLF001
            if type(algo) not in (ImmediateAlgo, SlicingAlgo):
                reasons.append("execution algorithm is not an exact vetted paper algorithm")
            elif type(algo) is SlicingAlgo and (
                not self._finite_positive(algo._cap)  # noqa: SLF001
                or type(algo._max_slices) is not int  # noqa: SLF001
                or algo._max_slices <= 0  # noqa: SLF001
            ):
                reasons.append("paper slicing algorithm configuration is invalid")
            if require_guard and type(execution._authority) is ExecutionAuthority:  # noqa: SLF001
                canonical_algo = execution._authority.algo  # noqa: SLF001
                if type(canonical_algo) is not type(algo):
                    reasons.append("canonical execution algorithm does not match desk execution")
                elif type(algo) is SlicingAlgo and (
                    canonical_algo._cap != algo._cap  # noqa: SLF001
                    or canonical_algo._max_slices != algo._max_slices  # noqa: SLF001
                ):
                    reasons.append("canonical slicing configuration does not match desk execution")
        if scheduler is not None:
            if type(scheduler) is not ExecutionScheduler:
                reasons.append("scheduler is not exact ExecutionScheduler")
            elif scheduler._execution is not execution:  # noqa: SLF001
                reasons.append("scheduler execution is not desk execution")
            elif require_guard and scheduler._work_guard is not self._schedule_guard:  # noqa: SLF001
                reasons.append("scheduler work guard is not engine boundary guard")
            if type(scheduler) is ExecutionScheduler:
                profile = scheduler._profile  # noqa: SLF001
                if type(scheduler._slices) is not int or scheduler._slices <= 0:  # noqa: SLF001
                    reasons.append("scheduler slices are not exact positive integer")
                if profile is not None and (
                    type(profile) is not tuple
                    or not profile
                    or any(not self._finite_positive(weight) for weight in profile)
                ):
                    reasons.append("scheduler volume profile is invalid")

        if type(policy) is not TradingPolicy:
            reasons.append("desk policy is not exact TradingPolicy")
        elif type(self._risk) is RiskEngine:
            reasons.extend(self._policy_risk_reasons(policy, self._risk))
        return reasons

    def _invalidate_epoch_locked(self) -> None:
        self._boundary_epoch += 1
        if type(self._desk) is AutonomousTradingDesk:
            scheduler = self._desk._scheduler  # noqa: SLF001 - attested graph
            if type(scheduler) is ExecutionScheduler:
                scheduler.cancel_all()

    def _wait_for_inflight_locked(self) -> None:
        while self._inflight_orders:
            self._boundary.wait()

    @contextmanager
    def _execution_permit(self):
        """Linearize the final order path against stop, kill, restart, and reconfiguration."""
        expected_epoch = self._cycle_epoch.get()
        expected_task = self._cycle_task.get()
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        rejection = ""
        with self._boundary:
            reasons = self._runtime_boundary_reasons()
            if reasons:
                self._trip_boundary(reasons)
                rejection = "paper boundary violation: " + "; ".join(reasons)
            elif expected_epoch is None:
                rejection = "paper execution has no attested cycle"
            elif expected_task is None or expected_task is not current_task:
                rejection = "paper execution is outside the attested cycle task"
            elif expected_epoch != self._boundary_epoch:
                rejection = "stale paper execution cycle"
            elif self._intent is not AutonomousStatus.RUNNING:
                rejection = "paper execution is not running"
            elif self._execution_authority is None:
                rejection = "paper execution has no canonical authority"
            else:
                self._inflight_orders += 1
        if rejection:
            yield RiskDecision(False, rejection)
            return
        try:
            assert self._execution_authority is not None
            yield self._execution_authority
        finally:
            with self._boundary:
                self._inflight_orders -= 1
                self._boundary.notify_all()

    @contextmanager
    def _scheduling_permit(self):
        """Linearize queued work insertion so an old cycle cannot enqueue after stop/restart."""
        expected_epoch = self._cycle_epoch.get()
        expected_task = self._cycle_task.get()
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        with self._boundary:
            reasons = self._runtime_boundary_reasons()
            if reasons:
                self._trip_boundary(reasons)
                yield RiskDecision(False, "paper boundary violation: " + "; ".join(reasons))
            elif expected_epoch is None:
                yield RiskDecision(False, "paper scheduling has no attested cycle")
            elif expected_task is None or expected_task is not current_task:
                yield RiskDecision(False, "paper scheduling is outside the attested cycle task")
            elif expected_epoch != self._boundary_epoch:
                yield RiskDecision(False, "stale paper scheduling cycle")
            elif self._intent is not AutonomousStatus.RUNNING:
                yield RiskDecision(False, "paper execution is not running")
            else:
                # Keep the boundary lock through submit_parent. A concurrent stop can then only
                # linearize before this insertion (which rejects) or after it (which cancel_all).
                yield None

    def _boundary_signature(self) -> object:
        if type(self._desk) is not AutonomousTradingDesk or type(self._risk) is not RiskEngine:
            return None
        policy = self._desk._policy  # noqa: SLF001 - boundary attestation
        execution = self._desk._execution  # noqa: SLF001
        if type(policy) is not TradingPolicy or type(execution) is not ExecutionEngine:
            return None
        algo = execution._algo  # noqa: SLF001
        algo_signature = (
            type(algo),
            getattr(algo, "_cap", None),
            getattr(algo, "_max_slices", None),
        )
        scheduler = self._desk._scheduler  # noqa: SLF001
        scheduler_signature = (
            None
            if scheduler is None
            else (
                type(scheduler),
                getattr(scheduler, "_slices", None),
                getattr(scheduler, "_profile", None),
            )
        )
        return (
            asdict(policy),
            asdict(self._risk.limits),
            algo_signature,
            scheduler_signature,
        )

    def _active_binding_reasons(self, binding: object) -> list[str]:
        if binding is None:
            return ["risk config is not bound"]
        if binding != self._boundary_signature():
            return ["active paper policy/RiskEngine binding drifted"]
        return []

    @staticmethod
    def _binding(config: TradingRiskConfig) -> tuple[float, float, float]:
        return (config.capital, config.risk_per_trade_pct, config.max_daily_loss_pct)

    def _config_reasons(self, config: object) -> list[str]:
        if type(config) is not TradingRiskConfig:
            return ["risk config is not exact TradingRiskConfig"]
        if type(self._desk) is not AutonomousTradingDesk or type(self._risk) is not RiskEngine:
            return ["risk config cannot be verified against unsafe wiring"]
        capital, risk_per_trade, daily_loss = self._binding(config)
        policy = self._desk._policy  # noqa: SLF001 - boundary attestation
        limits = self._risk.limits
        reasons: list[str] = []
        if capital != policy.capital or capital != getattr(limits, "max_capital", None):
            reasons.append("risk config capital is not bound")
        if risk_per_trade != policy.risk_per_trade or risk_per_trade != limits.max_trade_risk_pct:
            reasons.append("risk config risk-per-trade is not bound")
        if daily_loss != policy.daily_loss_limit or daily_loss != limits.max_daily_loss_pct:
            reasons.append("risk config daily-loss limit is not bound")
        return reasons

    def _runtime_boundary_reasons(self) -> list[str]:
        reasons = self._paper_wiring_reasons()
        if self._intent is AutonomousStatus.RUNNING:
            reasons.extend(self._active_binding_reasons(self._active_risk_binding))
        return reasons

    def _trip_boundary(self, reasons: list[str]) -> None:
        with self._boundary:
            reason = "paper boundary violation: " + "; ".join(reasons)
            previous = self._intent
            self._intent = AutonomousStatus.DISABLED
            self._active_risk_binding = None
            self._invalidate_epoch_locked()
            self._error = reason
            self._audit.append(AuditEntry("system", _now_iso(), previous.value,
                                          AutonomousStatus.DISABLED.value, reason))
            self._record(Decision(_now_iso(), "*", execution_decision="BOUNDARY_REJECTED",
                                  reason=reason))

    def _require_paper_boundary(self) -> None:
        with self._boundary:
            reasons = self._paper_wiring_reasons()
            if reasons:
                self._trip_boundary(reasons)
                raise RuntimeError("; ".join(reasons))

    # ------------------------------------------------------------- status
    @property
    def status(self) -> AutonomousStatus:
        with self._boundary:
            state = self._risk.state if type(self._risk) is RiskEngine else None
            if type(state) is RiskState and state.killed is True:
                return AutonomousStatus.KILLED
            if self._runtime_boundary_reasons():
                return AutonomousStatus.DISABLED
            if self._intent is AutonomousStatus.DISABLED:
                return AutonomousStatus.DISABLED
            if self._risk.state.halted and self._intent in _ACTIVE_INTENTS:
                return AutonomousStatus.HALTED
            return self._intent

    def _log_audit(self, actor: str, new: AutonomousStatus, reason: str) -> None:
        self._audit.append(AuditEntry(actor, _now_iso(), self.status.value, new.value, reason))

    def _record(self, dec: Decision) -> None:
        self._decisions.append(dec)
        if self._journal_path is not None:
            try:
                with open(self._journal_path, "a") as fh:
                    fh.write(json.dumps(dec.as_dict()) + "\n")
            except OSError:
                pass

    def set_decision_journal(self, path: str | None) -> None:
        """Persist every decision as an append-only JSONL line (§ Phase 11 dry-run journal)."""
        self._journal_path = path

    # -- decision enrichment (read-only observability) -------------------------
    @staticmethod
    def _final_from(d: dict) -> str:
        reason = (d.get("reason") or "").lower()
        ex = d.get("execution_decision") or ""
        if "data quality" in reason or (ex == "NO_TRADE"):
            return "NO_DATA" if "data" in reason else "NO_TRADE"
        rd = d.get("risk_decision")
        if rd == "REJECTED":
            return "REJECTED_BY_RISK"
        if rd == "APPROVED":
            return "PAPER_TRADE_WOULD_BE_EXECUTED"
        return "NO_TRADE"

    def _src_status(self, market_data: list[dict], instrument: str) -> tuple[str | None, str | None]:
        row = self._md_row(market_data, instrument)
        if not row:
            return (None, "NO_DATA")
        return (row.get("source"), row.get("status"))

    def _decision_from(self, d: dict, market_data: list[dict], now: datetime, exec_note: str) -> Decision:
        inst = d.get("instrument", "*")
        src, dstatus = self._src_status(market_data, inst)
        return Decision(
            now.isoformat(), inst, agent=d.get("agent"), action=d.get("action"),
            signal_strength=d.get("signal_strength"), confidence=d.get("confidence"),
            expected_risk=d.get("expected_risk"), suggested_size=d.get("suggested_size"),
            approved_size=d.get("approved_size"), entry=d.get("entry"), stop=d.get("stop"),
            target=d.get("target"), risk_decision=d.get("risk_decision"),
            execution_decision=exec_note, reason=d.get("reason", ""),
            source=src, data_status=dstatus, regime=d.get("regime"),
            consensus=d.get("consensus"), opportunity_score=d.get("opportunity_score"),
            final_decision=self._final_from(d),
            position_notional=d.get("position_notional"), stop_distance=d.get("stop_distance"),
            monetary_risk=d.get("monetary_risk"), risk_pct_capital=d.get("risk_pct_capital"),
            max_allowed_risk=d.get("max_allowed_risk"),
            remaining_daily_budget=d.get("remaining_daily_budget"))

    # ------------------------------------------------------------- transitions
    def arm(self, actor: str = "user") -> AutonomousStatus:
        with self._boundary:
            self._require_paper_boundary()
            if self._risk.state.killed:
                raise RuntimeError("kill switch engaged — reset before arming")
            if self._intent is AutonomousStatus.DISABLED:
                self._log_audit(actor, AutonomousStatus.ARMED, "explicit arm")
                self._intent = AutonomousStatus.ARMED
                self._invalidate_epoch_locked()
            return self.status

    def dry_run(self, actor: str = "user", duration_minutes: float = 60.0) -> AutonomousStatus:
        """Enter PAPER DRY RUN — full pipeline on real data, decisions logged, NO orders. Runs for
        a controlled window (default 60 min) then auto-stops back to DISABLED (no infinite process)."""
        with self._boundary:
            self._require_paper_boundary()
            if self._risk.state.killed:
                raise RuntimeError("kill switch engaged — reset first")
            if self._intent in (
                AutonomousStatus.DISABLED,
                AutonomousStatus.ARMED,
                AutonomousStatus.DRY_RUN,
            ):
                self._dry_run_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=max(1.0, duration_minutes))
                )
                self._log_audit(
                    actor,
                    AutonomousStatus.DRY_RUN,
                    f"dry-run (no orders) for {duration_minutes:.0f} min",
                )
                self._intent = AutonomousStatus.DRY_RUN
                self._invalidate_epoch_locked()
            return self.status

    def start(self, *, confirm, actor: str = "user", connected: bool = False,
              market_data: list[dict] | None = None, risk_config=None) -> dict:
        """Two-step activation ARMED → RUNNING. Requires the exact confirmation AND all start-safety
        conditions. On any failure the reason is logged (audit + decision feed) and state is kept."""
        with self._boundary:
            state = self._risk.state if type(self._risk) is RiskEngine else None
            if (
                self._intent is not AutonomousStatus.ARMED
                or type(state) is not RiskState
                or state.killed is not False
            ):
                return self._reject_start(
                    actor, f"START requires state ARMED (current {self.status.value})"
                )
            confirmed = confirm is True or (
                type(confirm) is str and confirm == self.CONFIRM_PHRASE
            )
            if not confirmed:
                return self._reject_start(actor, "confirmation required (two-step activation)")
            ok, reasons = self._start_safety(connected, market_data, risk_config)
            if not ok:
                return self._reject_start(actor, "; ".join(reasons), reasons)
            self._log_audit(actor, AutonomousStatus.RUNNING, "explicit user confirmation")
            assert type(risk_config) is TradingRiskConfig
            self._active_risk_binding = self._boundary_signature()
            self._intent = AutonomousStatus.RUNNING
            self._invalidate_epoch_locked()
            self._last_start_reasons = []
            return {"ok": True, "status": self.status.value}

    def _reject_start(self, actor: str, reason: str, reasons: list[str] | None = None) -> dict:
        self._last_start_reasons = reasons or [reason]
        self._audit.append(AuditEntry(actor, _now_iso(), self.status.value, self.status.value,
                                      f"START_REJECTED: {reason}"))
        self._record(Decision(_now_iso(), "*", execution_decision="START_REJECTED", reason=reason))
        return {"ok": False, "status": self.status.value, "reasons": self._last_start_reasons}

    def stop(self, actor: str = "user") -> AutonomousStatus:
        with self._boundary:
            if self._intent in (AutonomousStatus.RUNNING, AutonomousStatus.DRY_RUN):
                self._log_audit(actor, AutonomousStatus.ARMED, "stopped")
                self._intent = AutonomousStatus.ARMED
                self._active_risk_binding = None
                self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            return self.status

    def disarm(self, actor: str = "user") -> AutonomousStatus:
        with self._boundary:
            state = self._risk.state if type(self._risk) is RiskEngine else None
            if type(state) is not RiskState or state.killed is not True:
                self._log_audit(actor, AutonomousStatus.DISABLED, "disarmed")
                self._intent = AutonomousStatus.DISABLED
                self._active_risk_binding = None
                self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            return self.status

    def kill(self, reason: str = "manual", actor: str = "user") -> AutonomousStatus:
        with self._boundary:
            self._log_audit(actor, AutonomousStatus.KILLED, f"kill switch: {reason}")
            if type(self._risk) is RiskEngine:
                self._risk.kill_switch(reason)
            self._active_risk_binding = None
            self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            return self.status

    def reset_kill(self, actor: str = "user") -> AutonomousStatus:
        with self._boundary:
            self._require_paper_boundary()
            self._risk.reset_kill()
            self._log_audit(actor, AutonomousStatus.DISABLED, "kill reset (explicit)")
            self._intent = AutonomousStatus.DISABLED
            self._active_risk_binding = None
            self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            return self.status

    @contextmanager
    def risk_configuration_update(self, actor: str = "risk-config"):
        """Atomically invalidate old cycles while RiskEngine and policy are rebound together."""
        with self._boundary:
            if self._intent in (AutonomousStatus.RUNNING, AutonomousStatus.DRY_RUN):
                self._log_audit(actor, AutonomousStatus.ARMED, "risk configuration changed")
                self._intent = AutonomousStatus.ARMED
            self._active_risk_binding = None
            self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            try:
                yield
            finally:
                reasons = self._paper_wiring_reasons()
                if reasons:
                    self._trip_boundary(reasons)

    def start_new_day(self, equity: float, now: date | None = None) -> None:
        if not self._finite_positive(equity):
            raise ValueError("day-start equity must be finite and positive")
        trading_day = datetime.now(timezone.utc).date() if now is None else now
        if type(trading_day) is not date:
            raise ValueError("trading day must be an exact date")
        with self._boundary:
            if self._day is not None and trading_day <= self._day:
                raise ValueError("trading day must advance before the daily halt can reset")
            self._invalidate_epoch_locked()
            self._wait_for_inflight_locked()
            self._risk.start_new_day(float(equity))
            self._trades_today = 0
            self._day = trading_day

    # ------------------------------------------------------------- start safety
    def _start_safety(self, connected: bool, market_data: list[dict] | None, risk_config):
        reasons = self._paper_wiring_reasons()
        if connected is not True:
            reasons.append("IBKR not connected")
        rows = market_data if type(market_data) is list else []
        if not any(
            self._quality_ok(row)
            for row in rows
        ):
            reasons.append("no healthy REALTIME market data")
        state = self._risk.state if type(self._risk) is RiskEngine else None
        if type(state) is RiskState and state.killed is True:
            reasons.append("kill switch active")
        reasons.extend(self._config_reasons(risk_config))
        return (len(reasons) == 0, reasons)

    # ------------------------------------------------------------- data-quality gate
    @staticmethod
    def _quality_ok(row: dict | None) -> bool:
        if (
            type(row) is not dict
            or row.get("status") != "DATA_AVAILABLE"
            or row.get("market_data_type") != "REALTIME"
        ):
            return False
        bid, ask = row.get("bid"), row.get("ask")
        return (
            not isinstance(bid, bool)
            and isinstance(bid, (int, float))
            and math.isfinite(bid)
            and bid > 0
            and not isinstance(ask, bool)
            and isinstance(ask, (int, float))
            and math.isfinite(ask)
            and ask >= bid
        )

    @staticmethod
    def _gate_reason(row: dict | None) -> str:
        if not row:
            return "DATA_UNAVAILABLE"
        st = row.get("status")
        if st == "DATA_NOT_AVAILABLE":
            return "SUBSCRIPTION_REQUIRED" if row.get("error_code") == 10089 else "DATA_UNAVAILABLE"
        if st == "STALE":
            return "DATA_STALE"
        if st in ("ERROR",):
            return "DATA_INVALID"
        if st == "DELAYED":
            return "DATA_INVALID (delayed, not realtime)"
        return "DATA_UNAVAILABLE"

    def _md_row(self, market_data: list[dict], key: str) -> dict | None:
        base = key.split(":")[0].split(".")[0].upper()
        rows = market_data if type(market_data) is list else []
        for row in rows:
            if type(row) is not dict:
                continue
            symbol = row.get("symbol", "")
            if type(symbol) is not str:
                continue
            sym = symbol.upper().replace(".", "")
            if sym.startswith(base):
                return row
        return None

    # ------------------------------------------------------------- step
    async def step(self, *, now: datetime, bars: list, market_data: list[dict]) -> None:
        with self._boundary:
            reasons = self._runtime_boundary_reasons()
            if reasons:
                self._trip_boundary(reasons)
                return
            # Controlled observation window: auto-stop the dry run back to DISABLED.
            if (
                self._intent is AutonomousStatus.DRY_RUN
                and self._dry_run_until is not None
                and datetime.now(timezone.utc) >= self._dry_run_until
            ):
                self._log_audit(
                    "system", AutonomousStatus.DISABLED, "dry-run observation period elapsed"
                )
                self._intent = AutonomousStatus.DISABLED
                self._dry_run_until = None
                self._invalidate_epoch_locked()
            st = self.status
            if st is AutonomousStatus.DISABLED:
                return
            if self._desk_cycle_active:
                self._record(
                    Decision(
                        _now_iso(),
                        "*",
                        execution_decision="CYCLE_REJECTED",
                        reason="paper desk cycle already active",
                    )
                )
                return
            self._desk_cycle_active = True
            self._eval_count += 1
            cycle_epoch = self._boundary_epoch if st is AutonomousStatus.RUNNING else None

        token = self._cycle_epoch.set(cycle_epoch)
        task_token = self._cycle_task.set(asyncio.current_task())
        try:
            if st is AutonomousStatus.RUNNING:
                await self._execute_step(now, bars, market_data)
            else:
                # ARMED / DRY_RUN / HALTED / KILLED → compute + log, NEVER execute.
                await self._evaluate_step(now, bars, market_data, st)
        finally:
            self._cycle_task.reset(task_token)
            self._cycle_epoch.reset(token)
            with self._boundary:
                self._desk_cycle_active = False
                self._boundary.notify_all()

    async def observe(self, *, now: datetime, bars: list, market_data: list[dict]) -> dict:
        """READ-ONLY realtime intake (§ Phase 10.4). Feeds quality-gated REALTIME quotes to the desk
        and computes what it WOULD decide — WITHOUT ARM, WITHOUT execution, WITHOUT any order. This
        is how the autonomous engine consumes the live Massive feed while remaining DISABLED. It can
        never place a paper or IBKR order: it only reads (`to_broker=False`) and calls the read-only
        `desk.evaluate`. Returns {received, fed, decisions}."""
        with self._boundary:
            reasons = self._paper_wiring_reasons()
            if reasons:
                self._trip_boundary(reasons)
                raise RuntimeError("; ".join(reasons))
            if self._intent is not AutonomousStatus.DISABLED:
                raise RuntimeError("observe requires the engine to remain DISABLED")
            if self._desk_cycle_active:
                raise RuntimeError("paper desk cycle already active")
            self._desk_cycle_active = True
            self._obs_count += 1
        try:
            rows = bars if type(bars) is list else []
            received = [
                bar.instrument.symbol
                for bar in rows
                if type(bar) is Bar
                and self._quality_ok(self._md_row(market_data, bar.instrument.key))
            ]
            self._observed.update(received)
            fed = self._feed(rows, market_data, now, to_broker=False)
            n_dec = 0
            if fed:
                for d in await self._desk.evaluate(now=now):
                    self._record(
                        self._decision_from(d, market_data, now, "NO_ORDER (observe · disabled)")
                    )
                    n_dec += 1
            return {"received": received, "fed": fed, "decisions": n_dec}
        finally:
            with self._boundary:
                self._desk_cycle_active = False
                self._boundary.notify_all()

    @property
    def observed_instruments(self) -> set[str]:
        return set(self._observed)

    def _feed(self, bars: list, market_data: list[dict], now: datetime, *, to_broker: bool) -> int:
        fed = 0
        rows = bars if type(bars) is list else []
        for bar in rows:
            if type(bar) is not Bar:
                continue
            row = self._md_row(market_data, bar.instrument.key)
            if not self._quality_ok(row):
                self._record(Decision(now.isoformat(), bar.instrument.symbol,
                                      execution_decision="NO_TRADE", reason=self._gate_reason(row),
                                      source=(row or {}).get("source"),
                                      data_status=(row or {}).get("status", "NO_DATA"),
                                      final_decision="NO_DATA"))
                continue
            self._desk.on_bar(bar)
            bid, ask = row["bid"], row["ask"]
            quote = QuoteEvent(bar.instrument, float(bid), float(ask), now)
            self._desk.on_quote(quote)
            if to_broker:
                self._broker.set_quote(quote)
            fed += 1
        return fed

    async def _execute_step(self, now, bars, market_data) -> None:
        if self._feed(bars, market_data, now, to_broker=True) == 0:
            return
        before = len(self._journal.all()) if self._journal is not None else 0
        report = await self._desk.step(now=now)
        for er in report.executed:
            fill = er.result.fill if (er.result and er.result.fill) else None
            self._trades_today += 1
            self._record(Decision(
                now.isoformat(), er.order.instrument.symbol, action=er.order.side.value,
                approved_size=float(er.order.quantity), entry=(float(fill.price) if fill else None),
                risk_decision="APPROVED", execution_decision="PAPER_EXECUTED",
                reason=self._entry_reason(before)))
        for er in report.blocked:
            self._record(Decision(
                now.isoformat(), er.order.instrument.symbol, action=er.order.side.value,
                suggested_size=float(er.order.quantity), approved_size=0.0,
                risk_decision="REJECTED", execution_decision="REJECTED", reason=er.reason))

    async def _evaluate_step(self, now, bars, market_data, st) -> None:
        if self._feed(bars, market_data, now, to_broker=False) == 0:
            return
        exec_note = {
            AutonomousStatus.ARMED: "NO_ORDER (armed)",
            AutonomousStatus.DRY_RUN: "NO_ORDER (dry-run)",
            AutonomousStatus.HALTED: "NO_ORDER (halted)",
            AutonomousStatus.KILLED: "NO_ORDER (killed)",
        }.get(st, "NO_ORDER")
        for d in await self._desk.evaluate(now=now):
            note = (exec_note if d.get("execution_decision", "").startswith("NO_ORDER")
                    else d.get("execution_decision", exec_note))
            self._record(self._decision_from(d, market_data, now, note))

    def _entry_reason(self, journal_before: int) -> str:
        if self._journal is None:
            return "paper fill"
        recs = self._journal.all()
        if len(recs) <= journal_before:
            return "paper fill"
        t = recs[-1]
        agent = getattr(t, "agent", None) or getattr(t, "strategy", "?")
        action = getattr(t, "signal_action", None) or getattr(t, "direction", "?")
        return f"{agent} {action}"

    # ------------------------------------------------------------- metrics
    def metrics(self) -> dict:
        """Observation metrics computed from the decision log. No fabricated P&L (no trades)."""
        ds = list(self._decisions)
        conf = [d.confidence for d in ds if d.confidence is not None]
        risk = [d.expected_risk for d in ds if d.expected_risk is not None]
        size = [d.suggested_size for d in ds if d.suggested_size is not None]
        by_instrument: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for d in ds:
            if d.agent:
                by_instrument[d.instrument] = by_instrument.get(d.instrument, 0) + 1
                by_agent[d.agent] = by_agent.get(d.agent, 0) + 1
        mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
        return {
            "total_evaluations": self._eval_count,
            "observations": self._obs_count,                       # read-only observe() cycles
            "observed_instruments": sorted(self._observed),        # live instruments the engine consumed
            "opportunities_detected": sum(1 for d in ds if d.agent),
            "potential_trades": sum(1 for d in ds if d.suggested_size),
            "approved_decisions": sum(1 for d in ds if d.risk_decision == "APPROVED"),
            "rejected_decisions": sum(1 for d in ds if d.risk_decision == "REJECTED"),
            "no_data_decisions": sum(1 for d in ds if d.execution_decision == "NO_TRADE"),
            "risk_vetoes": sum(1 for d in ds if d.risk_decision == "REJECTED"),
            "avg_confidence": mean(conf),
            "avg_expected_risk": mean(risk),
            "avg_suggested_position": mean(size),
            "signals_by_instrument": by_instrument,
            "signals_by_agent": by_agent,
        }

    # ------------------------------------------------------------- read-model
    async def snapshot(self, *, market_data: list[dict] | None = None) -> dict:
        """Return a read model sourced only from the attested PaperBroker/RiskEngine graph."""
        with self._boundary:
            reasons = self._runtime_boundary_reasons()
            if reasons and self._intent is AutonomousStatus.RUNNING:
                self._trip_boundary(reasons)
            snapshot_epoch = self._boundary_epoch
            broker = self._broker if not reasons and type(self._broker) is PaperBroker else None

        account = None
        if broker is not None:
            try:
                candidate = await broker.get_account()
            except Exception as exc:  # noqa: BLE001 - snapshot must fail closed
                reasons.append(f"PaperBroker account unavailable: {type(exc).__name__}")
            else:
                with self._boundary:
                    post_reasons = self._runtime_boundary_reasons()
                    epoch_changed = self._boundary_epoch != snapshot_epoch
                    if epoch_changed:
                        reasons.append("paper boundary changed during account snapshot")
                    if post_reasons:
                        for reason in post_reasons:
                            if reason not in reasons:
                                reasons.append(reason)
                        if self._intent is AutonomousStatus.RUNNING:
                            self._trip_boundary(post_reasons)
                    elif not epoch_changed and (
                        type(candidate) is not Account
                        or type(candidate.positions) is not dict
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value)
                            for value in (
                                candidate.cash,
                                candidate.equity,
                                candidate.realized_pnl,
                                candidate.unrealized_pnl,
                                candidate.gross_exposure,
                                candidate.net_exposure,
                            )
                        )
                    ):
                        reasons.append("PaperBroker account snapshot is invalid")
                    elif not epoch_changed:
                        account = candidate

        with self._boundary:
            core_reasons = self._runtime_boundary_reasons()
            epoch_changed = self._boundary_epoch != snapshot_epoch
            if epoch_changed:
                local_reason = "paper boundary changed before snapshot assembly"
                if local_reason not in reasons:
                    reasons.append(local_reason)
            for reason in core_reasons:
                if reason not in reasons:
                    reasons.append(reason)
            if core_reasons and self._intent is AutonomousStatus.RUNNING:
                self._trip_boundary(core_reasons)
            if core_reasons or epoch_changed:
                account = None

            risk_config = None
            if (
                not core_reasons
                and type(self._risk) is RiskEngine
                and type(self._desk) is AutonomousTradingDesk
            ):
                policy = self._desk._policy  # noqa: SLF001 - attested graph
                try:
                    risk_config = TradingRiskConfig(
                        capital=policy.capital,
                        risk_per_trade_pct=self._risk.limits.max_trade_risk_pct,
                        max_daily_loss_pct=self._risk.limits.max_daily_loss_pct,
                    )
                except (TypeError, ValueError):
                    reasons.append("attested risk config is invalid")

            state = self._risk.state if type(self._risk) is RiskEngine else None
            r = state if not core_reasons and type(state) is RiskState else None
            daily_pnl = (
                account.equity - r.day_start_equity
                if (account is not None and r is not None and r.day_start_equity)
                else None
            )
            daily_loss_amount = max(0.0, -daily_pnl) if daily_pnl is not None else None
            max_daily = risk_config.max_daily_loss_amount if risk_config is not None else None
            remaining = (
                max(0.0, max_daily - daily_loss_amount)
                if (max_daily is not None and daily_loss_amount is not None)
                else None
            )
            risk_used = (
                daily_loss_amount / max_daily
                if (max_daily and daily_loss_amount is not None)
                else None
            )
            rows = market_data if type(market_data) is list else []
            avail = [
                row
                for row in rows
                if self._quality_ok(row)
            ]
            data_state = (
                "REALTIME"
                if any(row.get("market_data_type") == "REALTIME" for row in avail)
                else "STALE"
                if any(type(row) is dict and row.get("status") == "STALE" for row in rows)
                else "UNAVAILABLE"
            )
            risk_state = (
                "UNKNOWN"
                if r is None
                else "KILLED"
                if r.killed
                else "HALTED"
                if r.halted
                else "ACTIVE"
            )
            st = self.status
            boundary_verified = not reasons
            return {
                "mode": self.mode.upper() if type(self.mode) is str else "UNKNOWN",
                "status": st.value,
                "engine": "ERROR" if (self._error or not boundary_verified) else "HEALTHY",
                "data": data_state,
                "risk": risk_state,
                "paper_equity": account.equity if account is not None else None,
                "today_pnl": daily_pnl,
                "open_positions": len(account.positions) if account is not None else None,
                "trades_today": self._trades_today,
                "risk_used": risk_used,
                "remaining_daily_loss": remaining,
                "max_daily_loss": max_daily,
                "dry_run": st is AutonomousStatus.DRY_RUN,
                "dry_run_until": self._dry_run_until.isoformat() if self._dry_run_until else None,
                "metrics": self.metrics(),
                "start_rejected_reasons": list(self._last_start_reasons),
                "confirm_phrase": self.CONFIRM_PHRASE,
                "decisions": [d.as_dict() for d in list(self._decisions)[-60:][::-1]],
                "audit": [a.as_dict() for a in list(self._audit)[-30:][::-1]],
                "paper_boundary_verified": boundary_verified,
                "paper_boundary_reasons": list(reasons),
                "execution_adapter": "PaperBroker" if boundary_verified else None,
                "risk_config_bound": risk_config is not None and not core_reasons,
                "live_execution": False if boundary_verified else None,
                "ibkr_orders": 0 if boundary_verified else None,
            }
