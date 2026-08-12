"""Position reconciliation (§17, §18).

The desk's internal view of what it holds and the broker's actual record must agree. They can
drift after a disconnect, a missed fill, a manual intervention, or a restart (§18). This
module compares the two and, on any mismatch, is expected to **stop new trading**, let the
state be reconstructed, and only then resume (§17).

Kept deliberately independent of any specific broker: it consumes the `Broker` interface and
a plain internal book (`{instrument_key: signed_quantity}`), so it works identically for the
PaperBroker and the IBKR adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..logging_config import get_logger
from .base import Broker, Position

log = get_logger("reconcile")


@dataclass(slots=True)
class ReconciliationBreak:
    key: str
    internal_qty: float
    broker_qty: float

    @property
    def diff(self) -> float:
        return self.broker_qty - self.internal_qty


@dataclass(slots=True)
class ReconciliationReport:
    breaks: list[ReconciliationBreak] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    internal_count: int = 0
    broker_count: int = 0

    @property
    def is_consistent(self) -> bool:
        return not self.breaks

    def summary(self) -> str:
        if self.is_consistent:
            return f"consistent ({self.broker_count} positions)"
        parts = [f"{b.key}: internal={b.internal_qty:g} broker={b.broker_qty:g}" for b in self.breaks]
        return f"{len(self.breaks)} break(s): " + "; ".join(parts)


def diff_positions(
    internal_book: dict[str, float],
    broker_positions: dict[str, Position],
    *,
    tol: float = 1e-6,
) -> ReconciliationReport:
    """Compare an internal `{key: qty}` book against broker positions. Pure function."""
    broker_qty = {k: p.quantity for k, p in broker_positions.items()}
    report = ReconciliationReport(
        internal_count=sum(1 for q in internal_book.values() if abs(q) > tol),
        broker_count=len(broker_qty),
    )
    for key in set(internal_book) | set(broker_qty):
        iq = internal_book.get(key, 0.0)
        bq = broker_qty.get(key, 0.0)
        if abs(bq - iq) > tol:
            report.breaks.append(ReconciliationBreak(key=key, internal_qty=iq, broker_qty=bq))
    report.breaks.sort(key=lambda b: b.key)
    return report


@dataclass(slots=True)
class GenericBreak:
    field: str
    internal: float
    broker: float

    @property
    def diff(self) -> float:
        return self.broker - self.internal


@dataclass(slots=True)
class InternalState:
    """The desk's own view of its account, for full reconciliation (§17)."""

    positions: dict[str, float]                      # instrument key -> signed quantity
    cash: float | None = None
    realized_pnl: float | None = None
    open_orders: dict[str, float] = field(default_factory=dict)  # key -> signed working qty


@dataclass(slots=True)
class FullReconciliationReport:
    position_breaks: list[ReconciliationBreak] = field(default_factory=list)
    cash_break: GenericBreak | None = None
    pnl_break: GenericBreak | None = None
    order_breaks: list[GenericBreak] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_consistent(self) -> bool:
        return not (self.position_breaks or self.cash_break or self.pnl_break or self.order_breaks)

    def summary(self) -> str:
        if self.is_consistent:
            return "consistent (positions, cash, P&L, orders)"
        parts = [f"{b.key}: internal={b.internal_qty:g} broker={b.broker_qty:g}" for b in self.position_breaks]
        for b in (self.cash_break, self.pnl_break):
            if b is not None:
                parts.append(f"{b.field}: internal={b.internal:g} broker={b.broker:g}")
        parts += [f"{b.field}: internal={b.internal:g} broker={b.broker:g}" for b in self.order_breaks]
        return f"{_break_count(self)} break(s): " + "; ".join(parts)


def _break_count(r: FullReconciliationReport) -> int:
    return len(r.position_breaks) + (r.cash_break is not None) + (r.pnl_break is not None) + len(r.order_breaks)


async def reconcile_full(state: InternalState, broker: Broker, *,
                         tol: float = 1e-6, cash_tol: float = 1.0) -> FullReconciliationReport:
    """Compare the internal state against the broker: positions, cash, realized P&L, open orders
    (§17). Any field the internal state doesn't track (None) is skipped."""
    positions = await broker.get_positions()
    pos_report = diff_positions(state.positions, positions, tol=tol)
    report = FullReconciliationReport(position_breaks=pos_report.breaks)

    account = await broker.get_account()
    if state.cash is not None and abs(account.cash - state.cash) > cash_tol:
        report.cash_break = GenericBreak("cash", state.cash, account.cash)
    if state.realized_pnl is not None and abs(account.realized_pnl - state.realized_pnl) > cash_tol:
        report.pnl_break = GenericBreak("realized_pnl", state.realized_pnl, account.realized_pnl)

    if state.open_orders and hasattr(broker, "open_orders"):
        raw = await broker.open_orders()  # type: ignore[attr-defined]
        broker_orders = {
            o["instrument_key"]: (o["quantity"] if str(o["action"]).upper().startswith("B") else -o["quantity"])
            for o in raw
        }
        for key in set(state.open_orders) | set(broker_orders):
            iq, bq = state.open_orders.get(key, 0.0), broker_orders.get(key, 0.0)
            if abs(bq - iq) > tol:
                report.order_breaks.append(GenericBreak(f"order:{key}", iq, bq))

    if not report.is_consistent:
        log.error("FULL RECONCILIATION MISMATCH — %s", report.summary())
    return report


class Reconciler:
    """Runs reconciliation against a broker and halts risk on any mismatch (§17)."""

    def __init__(self, broker: Broker, *, risk=None, tol: float = 1e-6) -> None:
        self._broker = broker
        self._risk = risk
        self._tol = tol

    async def run(self, internal_book: dict[str, float]) -> ReconciliationReport:
        positions = await self._broker.get_positions()
        report = diff_positions(internal_book, positions, tol=self._tol)
        if report.is_consistent:
            log.debug("reconciliation %s", report.summary())
        else:
            log.error("RECONCILIATION MISMATCH — %s", report.summary())
            if self._risk is not None:
                # Stop new trades; reductions still allowed so the desk can flatten to truth.
                self._risk.force_halt(f"reconciliation break: {report.summary()}")
        return report

    async def run_full(self, state: InternalState, *, cash_tol: float = 1.0) -> FullReconciliationReport:
        """Full reconciliation (positions, cash, P&L, open orders, §17). Halts risk on any break."""
        report = await reconcile_full(state, self._broker, tol=self._tol, cash_tol=cash_tol)
        if not report.is_consistent and self._risk is not None:
            self._risk.force_halt(f"reconciliation break: {report.summary()}")
        return report
