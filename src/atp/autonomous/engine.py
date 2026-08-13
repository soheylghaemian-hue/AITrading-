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

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from ..core.events import QuoteEvent


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
        self._desk = desk
        self._broker = broker
        self._risk = risk
        self._journal = journal
        self.mode = mode                       # always "paper"
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

    # ------------------------------------------------------------- status
    @property
    def status(self) -> AutonomousStatus:
        if self._risk.state.killed:
            return AutonomousStatus.KILLED
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
            final_decision=self._final_from(d))

    # ------------------------------------------------------------- transitions
    def arm(self, actor: str = "user") -> AutonomousStatus:
        if self._risk.state.killed:
            raise RuntimeError("kill switch engaged — reset before arming")
        if self._intent is AutonomousStatus.DISABLED:
            self._log_audit(actor, AutonomousStatus.ARMED, "explicit arm")
            self._intent = AutonomousStatus.ARMED
        return self.status

    def dry_run(self, actor: str = "user", duration_minutes: float = 60.0) -> AutonomousStatus:
        """Enter PAPER DRY RUN — full pipeline on real data, decisions logged, NO orders. Runs for
        a controlled window (default 60 min) then auto-stops back to DISABLED (no infinite process)."""
        if self._risk.state.killed:
            raise RuntimeError("kill switch engaged — reset first")
        if self._intent in (AutonomousStatus.DISABLED, AutonomousStatus.ARMED, AutonomousStatus.DRY_RUN):
            self._dry_run_until = (datetime.now(timezone.utc)
                                   + timedelta(minutes=max(1.0, duration_minutes)))
            self._log_audit(actor, AutonomousStatus.DRY_RUN,
                            f"dry-run (no orders) for {duration_minutes:.0f} min")
            self._intent = AutonomousStatus.DRY_RUN
        return self.status

    def start(self, *, confirm, actor: str = "user", connected: bool = False,
              market_data: list[dict] | None = None, risk_config=None) -> dict:
        """Two-step activation ARMED → RUNNING. Requires the exact confirmation AND all start-safety
        conditions. On any failure the reason is logged (audit + decision feed) and state is kept."""
        if self._intent is not AutonomousStatus.ARMED or self._risk.state.killed:
            return self._reject_start(actor, f"START requires state ARMED (current {self.status.value})")
        if confirm is not True and confirm != self.CONFIRM_PHRASE:
            return self._reject_start(actor, "confirmation required (two-step activation)")
        ok, reasons = self._start_safety(connected, market_data, risk_config)
        if not ok:
            return self._reject_start(actor, "; ".join(reasons), reasons)
        self._log_audit(actor, AutonomousStatus.RUNNING, "explicit user confirmation")
        self._intent = AutonomousStatus.RUNNING
        self._last_start_reasons = []
        return {"ok": True, "status": self.status.value}

    def _reject_start(self, actor: str, reason: str, reasons: list[str] | None = None) -> dict:
        self._last_start_reasons = reasons or [reason]
        self._audit.append(AuditEntry(actor, _now_iso(), self.status.value, self.status.value,
                                      f"START_REJECTED: {reason}"))
        self._record(Decision(_now_iso(), "*", execution_decision="START_REJECTED", reason=reason))
        return {"ok": False, "status": self.status.value, "reasons": self._last_start_reasons}

    def stop(self, actor: str = "user") -> AutonomousStatus:
        if self._intent in (AutonomousStatus.RUNNING, AutonomousStatus.DRY_RUN):
            self._log_audit(actor, AutonomousStatus.ARMED, "stopped")
            self._intent = AutonomousStatus.ARMED
        return self.status

    def disarm(self, actor: str = "user") -> AutonomousStatus:
        if not self._risk.state.killed:
            self._log_audit(actor, AutonomousStatus.DISABLED, "disarmed")
            self._intent = AutonomousStatus.DISABLED
        return self.status

    def kill(self, reason: str = "manual", actor: str = "user") -> AutonomousStatus:
        self._log_audit(actor, AutonomousStatus.KILLED, f"kill switch: {reason}")
        self._risk.kill_switch(reason)
        return self.status

    def reset_kill(self, actor: str = "user") -> AutonomousStatus:
        self._risk.reset_kill()
        self._log_audit(actor, AutonomousStatus.DISABLED, "kill reset (explicit)")
        self._intent = AutonomousStatus.DISABLED
        return self.status

    def start_new_day(self, equity: float, now: date | None = None) -> None:
        self._risk.start_new_day(equity)
        self._trades_today = 0
        self._day = now or datetime.now(timezone.utc).date()

    # ------------------------------------------------------------- start safety
    def _start_safety(self, connected: bool, market_data: list[dict] | None, risk_config):
        reasons: list[str] = []
        if not connected:
            reasons.append("IBKR not connected")
        if not any((r.get("status") == "DATA_AVAILABLE" and r.get("market_data_type") == "REALTIME")
                   for r in (market_data or [])):
            reasons.append("no healthy REALTIME market data")
        if self._risk.state.killed:
            reasons.append("kill switch active")
        if self._broker is None:
            reasons.append("paper execution unavailable")
        if risk_config is None:
            reasons.append("risk config not set")
        else:
            if not (risk_config.capital > 0):
                reasons.append("trading capital must be > 0")
            if not (risk_config.risk_per_trade_pct > 0):
                reasons.append("risk per trade must be > 0")
            if not (risk_config.max_daily_loss_pct > 0):
                reasons.append("daily loss limit must be > 0")
        return (len(reasons) == 0, reasons)

    # ------------------------------------------------------------- data-quality gate
    @staticmethod
    def _quality_ok(row: dict | None) -> bool:
        return bool(row) and row.get("status") == "DATA_AVAILABLE" and row.get("market_data_type") == "REALTIME"

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
        for row in market_data or []:
            sym = str(row.get("symbol", "")).upper().replace(".", "")
            if sym.startswith(base):
                return row
        return None

    # ------------------------------------------------------------- step
    async def step(self, *, now: datetime, bars: list, market_data: list[dict]) -> None:
        # Controlled observation window: auto-stop the dry run back to DISABLED when it elapses.
        if (self._intent is AutonomousStatus.DRY_RUN and self._dry_run_until is not None
                and datetime.now(timezone.utc) >= self._dry_run_until):
            self._log_audit("system", AutonomousStatus.DISABLED, "dry-run observation period elapsed")
            self._intent = AutonomousStatus.DISABLED
            self._dry_run_until = None
        st = self.status
        if st is AutonomousStatus.DISABLED:
            return
        self._eval_count += 1
        if st is AutonomousStatus.RUNNING:
            await self._execute_step(now, bars, market_data)
        else:
            # ARMED / DRY_RUN / HALTED / KILLED → compute + log decisions, NEVER execute.
            await self._evaluate_step(now, bars, market_data, st)

    async def observe(self, *, now: datetime, bars: list, market_data: list[dict]) -> dict:
        """READ-ONLY realtime intake (§ Phase 10.4). Feeds quality-gated REALTIME quotes to the desk
        and computes what it WOULD decide — WITHOUT ARM, WITHOUT execution, WITHOUT any order. This
        is how the autonomous engine consumes the live Massive feed while remaining DISABLED. It can
        never place a paper or IBKR order: it only reads (`to_broker=False`) and calls the read-only
        `desk.evaluate`. Returns {received, fed, decisions}."""
        self._obs_count += 1
        received = [b.instrument.symbol for b in bars
                    if self._quality_ok(self._md_row(market_data, b.instrument.key))]
        self._observed.update(received)
        fed = self._feed(bars, market_data, now, to_broker=False)
        n_dec = 0
        if fed:
            for d in await self._desk.evaluate(now=now):
                self._record(self._decision_from(d, market_data, now, "NO_ORDER (observe · disabled)"))
                n_dec += 1
        return {"received": received, "fed": fed, "decisions": n_dec}

    @property
    def observed_instruments(self) -> set[str]:
        return set(self._observed)

    def _feed(self, bars: list, market_data: list[dict], now: datetime, *, to_broker: bool) -> int:
        fed = 0
        for bar in bars:
            row = self._md_row(market_data, bar.instrument.key)
            if not self._quality_ok(row):
                self._record(Decision(now.isoformat(), bar.instrument.symbol,
                                      execution_decision="NO_TRADE", reason=self._gate_reason(row),
                                      source=(row or {}).get("source"),
                                      data_status=(row or {}).get("status", "NO_DATA"),
                                      final_decision="NO_DATA"))
                continue
            self._desk.on_bar(bar)
            bid, ask = row.get("bid"), row.get("ask")
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
                quote = QuoteEvent(bar.instrument, float(bid), float(ask), now)
                self._desk.on_quote(quote)
                if to_broker and hasattr(self._broker, "set_quote"):
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
    def snapshot(self, *, account, risk_config=None, market_data: list[dict] | None = None) -> dict:
        r = self._risk.state
        daily_pnl = (account.equity - r.day_start_equity) if (account is not None and r.day_start_equity) else None
        daily_loss_amount = max(0.0, -daily_pnl) if daily_pnl is not None else None
        max_daily = (risk_config.max_daily_loss_amount if risk_config is not None else None)
        remaining = (max(0.0, max_daily - daily_loss_amount)
                     if (max_daily is not None and daily_loss_amount is not None) else None)
        risk_used = (daily_loss_amount / max_daily if (max_daily and daily_loss_amount is not None) else None)
        avail = [row for row in (market_data or []) if row.get("status") == "DATA_AVAILABLE"]
        data_state = ("REALTIME" if any(row.get("market_data_type") == "REALTIME" for row in avail)
                      else "STALE" if any(row.get("status") == "STALE" for row in (market_data or []))
                      else "UNAVAILABLE")
        risk_state = ("KILLED" if r.killed else "HALTED" if r.halted else "ACTIVE")
        st = self.status
        return {
            "mode": self.mode.upper(),
            "status": st.value,
            "engine": "ERROR" if self._error else "HEALTHY",
            "data": data_state,
            "risk": risk_state,
            "paper_equity": (account.equity if account is not None else None),
            "today_pnl": daily_pnl,
            "open_positions": (len(account.positions) if account is not None else None),
            "trades_today": self._trades_today,
            "risk_used": risk_used,
            "remaining_daily_loss": remaining,
            "max_daily_loss": max_daily,
            "dry_run": st is AutonomousStatus.DRY_RUN,
            "dry_run_until": (self._dry_run_until.isoformat() if self._dry_run_until else None),
            "metrics": self.metrics(),
            "start_rejected_reasons": list(self._last_start_reasons),
            "confirm_phrase": self.CONFIRM_PHRASE,
            "decisions": [d.as_dict() for d in list(self._decisions)[-60:][::-1]],
            "audit": [a.as_dict() for a in list(self._audit)[-30:][::-1]],
            "live_execution": False,          # ALWAYS false — never trades live
            "ibkr_orders": 0,                 # never sends orders to the live broker API
        }
