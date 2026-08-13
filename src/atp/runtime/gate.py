"""Fail-closed trading gate + durable daily-loss (§ Phase B).

The system fails CLOSED: when anything is unavailable or ambiguous, NO NEW TRADE. The gate is the one
place every "may I open risk now?" question is answered, from durable state only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..store.money import D
from .lifecycle import LifecycleManager, RuntimeStatus


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass(slots=True)
class GateResult:
    allowed: bool
    reason: str


class TradingGate:
    def __init__(self, store, lifecycle: LifecycleManager):
        self._store = store
        self._life = lifecycle

    def can_trade(self, *, trade_date: str | None = None) -> GateResult:
        # 1) database is the source of truth — if it is unreachable, block.
        if not self._store.ping():
            return GateResult(False, "database unavailable → NO NEW TRADE")
        # 2) kill switch (durable) always wins.
        if self._store.get_kill_switch().engaged:
            return GateResult(False, "kill switch engaged → HALT")
        # 3) runtime must be RUNNING; RECOVERY_REQUIRED / anything else blocks.
        st = self._life.status
        if st is RuntimeStatus.RECOVERY_REQUIRED:
            return GateResult(False, "recovery required → NO NEW TRADE")
        if st is not RuntimeStatus.RUNNING:
            return GateResult(False, f"not RUNNING (state {st.value}) → NO NEW TRADE")
        # 4) risk state must load — if not, block.
        if self._store.get_risk_state() is None:
            return GateResult(False, "risk state unavailable → NO NEW TRADE")
        # 5) daily-loss lock (durable) for today.
        d = trade_date or today_utc()
        if self._store.get_daily_loss_lock(d).engaged:
            return GateResult(False, "daily loss limit reached → HALT")
        return GateResult(True, "ok")


def remaining_daily_budget(store, *, trade_date: str | None = None):
    """Remaining daily-loss budget from DURABLE state = max_daily_loss − loss_so_far.
    Returns a Decimal, or None if config/pnl are missing."""
    d = trade_date or today_utc()
    cfg = store.get_risk_config()
    pnl = store.get_daily_pnl(d)
    if cfg is None or pnl is None:
        return None
    limit = cfg.capital * cfg.max_daily_loss_pct
    loss_so_far = max(D(0), -(pnl.realized_pnl + pnl.unrealized_pnl))
    return max(D(0), limit - loss_so_far)


def enforce_daily_loss(store, *, trade_date: str | None = None) -> bool:
    """If today's loss has reached the limit, engage the durable daily-loss lock. Returns True if
    the lock is (now) engaged."""
    d = trade_date or today_utc()
    rem = remaining_daily_budget(store, trade_date=d)
    if rem is not None and rem <= D(0):
        if not store.get_daily_loss_lock(d).engaged:
            store.set_daily_loss_lock(trade_date=d, engaged=True,
                                      reason="daily loss limit reached (durable)")
        return True
    return store.get_daily_loss_lock(d).engaged
