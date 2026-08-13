"""PaperAutonomousEngine — armed, observable, internally-executed paper trading (§ Phase 8).

It drives the EXISTING AutonomousTradingDesk (features → regime → 9 agents → opportunity →
portfolio → sizing → RISK VETO → execution → journal) but adds:

  * an explicit mode/status machine (DISABLED default; the user must arm it),
  * a data-quality gate — NO TRADE on unavailable/stale/invalid market data (never fake data),
  * a decision log for observability (why a trade filled / was rejected / was skipped),
  * a dashboard read-model block.

It NEVER sends an order to IBKR: execution goes to the desk's PaperBroker (internal simulation
with real bid/ask/spread/slippage/commission/impact). The Risk Engine remains authoritative and
the daily-loss lock holds until the next trading day. There is no automatic PAPER→LIVE promotion.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum

from ..core.events import QuoteEvent


class AutonomousStatus(str, Enum):
    DISABLED = "DISABLED"   # off (default) — nothing steps
    ARMED = "ARMED"         # enabled and ready, not yet stepping
    RUNNING = "RUNNING"     # actively making paper decisions
    HALTED = "HALTED"       # daily-loss / risk halt — no new trades until next day
    KILLED = "KILLED"       # emergency kill switch — all trading blocked


@dataclass(slots=True)
class Decision:
    ts: str
    instrument: str
    action: str | None
    quantity: float | None
    price: float | None
    decision: str            # FILLED | REJECTED | NO_DATA | HALTED
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


class PaperAutonomousEngine:
    def __init__(self, *, desk, broker, risk, journal=None, mode: str = "paper",
                 max_decisions: int = 200):
        self._desk = desk
        self._broker = broker
        self._risk = risk
        self._journal = journal
        self.mode = mode                      # always "paper" here — never "live"
        self._status = AutonomousStatus.DISABLED
        self._decisions: deque[Decision] = deque(maxlen=max_decisions)
        self._trades_today = 0
        self._day: date | None = None

    # ------------------------------------------------------------- status
    @property
    def status(self) -> AutonomousStatus:
        """Effective status — risk state overrides the armed state (kill/halt win)."""
        if self._risk.state.killed:
            return AutonomousStatus.KILLED
        if self._status == AutonomousStatus.DISABLED:
            return AutonomousStatus.DISABLED
        if self._risk.state.halted:
            return AutonomousStatus.HALTED
        return self._status

    def arm(self) -> AutonomousStatus:
        """Explicit user action to enable paper autonomous mode. Never enables live trading."""
        if self._risk.state.killed:
            raise RuntimeError("kill switch engaged — reset before arming")
        if self._status == AutonomousStatus.DISABLED:
            self._status = AutonomousStatus.RUNNING   # armed + ready to step
        return self.status

    def disarm(self) -> AutonomousStatus:
        self._status = AutonomousStatus.DISABLED
        return self.status

    def kill(self, reason: str = "manual") -> AutonomousStatus:
        self._risk.kill_switch(reason)
        return self.status

    def reset_kill(self) -> None:
        self._risk.reset_kill()

    def start_new_day(self, equity: float, now: date | None = None) -> None:
        """Clear the daily-loss lock at the next trading day (§15)."""
        self._risk.start_new_day(equity)
        self._trades_today = 0
        self._day = now or datetime.utcnow().date()

    # ------------------------------------------------------------- data-quality gate
    @staticmethod
    def _quality_ok(row: dict | None) -> bool:
        """A trade may use an instrument ONLY when its market data is REALTIME available.
        DELAYED/STALE/NOT_AVAILABLE/ERROR → no trade (never fabricate a price)."""
        return bool(row) and row.get("status") == "DATA_AVAILABLE"

    def _md_row(self, market_data: list[dict], key: str) -> dict | None:
        base = key.split(":")[0].split(".")[0].upper()
        for row in market_data or []:
            sym = str(row.get("symbol", "")).upper().replace(".", "")
            if sym.startswith(base):
                return row
        return None

    def _record(self, dec: Decision) -> None:
        self._decisions.append(dec)

    # ------------------------------------------------------------- step
    async def step(self, *, now: datetime, bars: list, market_data: list[dict]) -> None:
        """One autonomous decision cycle — ONLY when RUNNING. Feeds only quality-gated data to
        the desk, then runs the desk's own risk-vetoed execution and logs the decisions."""
        st = self.status
        if st is not AutonomousStatus.RUNNING:
            if st is AutonomousStatus.HALTED:
                self._record(Decision(now.isoformat(), "*", None, None, None, "HALTED",
                                      f"daily-loss lock: {self._risk.state.halt_reason}"))
            return

        # Feed only instruments with valid REALTIME data; gate the rest (NO TRADE, logged).
        fed = 0
        for bar in bars:
            row = self._md_row(market_data, bar.instrument.key)
            if self._quality_ok(row):
                self._desk.on_bar(bar)
                bid, ask = row.get("bid"), row.get("ask")
                if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
                    quote = QuoteEvent(bar.instrument, float(bid), float(ask), now)
                    self._desk.on_quote(quote)
                    if hasattr(self._broker, "set_quote"):
                        self._broker.set_quote(quote)   # paper broker fills against the real quote
                fed += 1
            else:
                self._record(Decision(now.isoformat(), bar.instrument.symbol, None, None, None,
                                      "NO_DATA", f"market data not tradable: {row.get('status') if row else 'NOT_AVAILABLE'}"))
        if fed == 0:
            return

        before = len(self._journal.all()) if self._journal is not None else 0
        report = await self._desk.step(now=now)

        for er in report.executed:
            fill = er.result.fill if (er.result and er.result.fill) else None
            self._trades_today += 1
            self._record(Decision(
                now.isoformat(), er.order.instrument.symbol, er.order.side.value,
                float(er.order.quantity), (float(fill.price) if fill else None),
                "FILLED", self._entry_reason(before)))
        for er in report.blocked:
            self._record(Decision(
                now.isoformat(), er.order.instrument.symbol, er.order.side.value,
                float(er.order.quantity), None, "REJECTED", er.reason))

    def _entry_reason(self, journal_before: int) -> str:
        """Explain a fill from the freshly-journaled trade context (agent/signal/stop)."""
        if self._journal is None:
            return "paper fill"
        recs = self._journal.all()
        if len(recs) <= journal_before:
            return "paper fill"
        t = recs[-1]
        agent = getattr(t, "agent", None) or getattr(t, "strategy", "?")
        action = getattr(t, "signal_action", None) or getattr(t, "direction", "?")
        stop = getattr(t, "expected_risk", None) or getattr(t, "stop_price", None)
        conf = getattr(t, "signal_strength", None)
        return f"{agent} {action} · conf {conf} · stop {stop}"

    # ------------------------------------------------------------- read-model
    def snapshot(self, *, account, risk_config=None) -> dict:
        """The AUTONOMOUS TRADING dashboard block. Real values only."""
        r = self._risk.state
        daily_pnl = (account.equity - r.day_start_equity) if account is not None and r.day_start_equity else None
        daily_loss_amount = max(0.0, -daily_pnl) if daily_pnl is not None else None
        max_daily = (risk_config.max_daily_loss_amount if risk_config is not None else None)
        remaining = (max(0.0, max_daily - daily_loss_amount)
                     if (max_daily is not None and daily_loss_amount is not None) else None)
        risk_used = (daily_loss_amount / max_daily if (max_daily and daily_loss_amount is not None) else None)
        return {
            "mode": self.mode.upper(),
            "status": self.status.value,
            "paper_equity": (account.equity if account is not None else None),
            "today_pnl": daily_pnl,
            "open_positions": (len(account.positions) if account is not None else None),
            "trades_today": self._trades_today,
            "risk_used": risk_used,
            "remaining_daily_loss": remaining,
            "max_daily_loss": max_daily,
            "decisions": [d.as_dict() for d in list(self._decisions)[-40:][::-1]],
            "live_execution": False,          # ALWAYS false — this engine never trades live
            "ibkr_orders": 0,                 # this engine never sends orders to the live broker API
        }
