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

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
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

    # ------------------------------------------------------------- transitions
    def arm(self, actor: str = "user") -> AutonomousStatus:
        if self._risk.state.killed:
            raise RuntimeError("kill switch engaged — reset before arming")
        if self._intent is AutonomousStatus.DISABLED:
            self._log_audit(actor, AutonomousStatus.ARMED, "explicit arm")
            self._intent = AutonomousStatus.ARMED
        return self.status

    def dry_run(self, actor: str = "user") -> AutonomousStatus:
        if self._risk.state.killed:
            raise RuntimeError("kill switch engaged — reset first")
        if self._intent in (AutonomousStatus.DISABLED, AutonomousStatus.ARMED):
            self._log_audit(actor, AutonomousStatus.DRY_RUN, "dry-run (no orders)")
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
        st = self.status
        if st is AutonomousStatus.DISABLED:
            return
        if st is AutonomousStatus.RUNNING:
            await self._execute_step(now, bars, market_data)
        else:
            # ARMED / DRY_RUN / HALTED / KILLED → compute + log decisions, NEVER execute.
            await self._evaluate_step(now, bars, market_data, st)

    def _feed(self, bars: list, market_data: list[dict], now: datetime, *, to_broker: bool) -> int:
        fed = 0
        for bar in bars:
            row = self._md_row(market_data, bar.instrument.key)
            if not self._quality_ok(row):
                self._record(Decision(now.isoformat(), bar.instrument.symbol,
                                      execution_decision="NO_TRADE", reason=self._gate_reason(row)))
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
            self._record(Decision(
                now.isoformat(), d.get("instrument", "*"), agent=d.get("agent"),
                action=d.get("action"), signal_strength=d.get("signal_strength"),
                confidence=d.get("confidence"), expected_risk=d.get("expected_risk"),
                suggested_size=d.get("suggested_size"), approved_size=d.get("approved_size"),
                entry=d.get("entry"), stop=d.get("stop"), target=d.get("target"),
                risk_decision=d.get("risk_decision"),
                execution_decision=(exec_note if d.get("execution_decision", "").startswith("NO_ORDER")
                                    else d.get("execution_decision", exec_note)),
                reason=d.get("reason", "")))

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
            "start_rejected_reasons": list(self._last_start_reasons),
            "confirm_phrase": self.CONFIRM_PHRASE,
            "decisions": [d.as_dict() for d in list(self._decisions)[-60:][::-1]],
            "audit": [a.as_dict() for a in list(self._audit)[-30:][::-1]],
            "live_execution": False,          # ALWAYS false — never trades live
            "ibkr_orders": 0,                 # never sends orders to the live broker API
        }
