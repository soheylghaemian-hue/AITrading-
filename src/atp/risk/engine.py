"""Risk Engine (§14, §24 Phase 11).

Independent, with absolute veto power over every trading decision (§14). It sits *after*
sizing and *before* execution; if any control is breached the answer is NO TRADE. Nothing
downstream can override it — that is the whole point of a separate engine.

Controls (§14): daily loss (kill switch), drawdown, per-instrument position size, gross
leverage / portfolio exposure, max open positions, per-trade risk, and correlated-cluster
exposure. Operational safety: an emergency **kill switch** (blocks everything), a **broker
disconnect** gate (no orders while offline), and an **invalid-price** guard. Position-mismatch
and stale-data are enforced via `force_halt` (from reconciliation) and the Data Quality Engine.

Order of precedence: kill switch → broker disconnect → invalid price → (risk-reducing always
ok) → halt → sizing/exposure caps. Risk-*reducing* orders are permitted once past the hard
stops — you may always cut risk, never only add it, but a kill switch stops even reductions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Callable

from ..brokers.base import Account, Order
from ..logging_config import get_logger

log = get_logger("risk")


@dataclass(slots=True)
class RiskLimits:
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_position_pct: float = 0.20
    max_gross_leverage: float = 1.0
    max_open_positions: int = 10
    max_trade_risk_pct: float = 0.01
    max_portfolio_risk_pct: float = 0.06
    max_correlated_exposure_pct: float = 0.35   # cluster of correlated names vs equity (§14)
    correlation_threshold: float = 0.5          # |corr| at/above which names count as one cluster


@dataclass(slots=True)
class RiskState:
    day_start_equity: float
    peak_equity: float
    halted: bool = False
    halt_reason: str = ""
    broker_connected: bool = True               # §17: no orders while disconnected
    killed: bool = False                        # §14 emergency kill switch — stops ALL orders
    kill_reason: str = ""


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.approved


class RiskEngine:
    def __init__(self, *, limits: RiskLimits, state: RiskState) -> None:
        self._limits = limits
        self._state = state

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def start_new_day(self, equity: float) -> None:
        """Reset the daily baseline (call at each session open, §15 trading hours)."""
        self._state.day_start_equity = equity
        self._state.halted = False
        self._state.halt_reason = ""

    def mark_equity(self, equity: float) -> None:
        """Update peak equity and latch a halt if a portfolio-level limit is breached (§14)."""
        self._state.peak_equity = max(self._state.peak_equity, equity)

        if self._state.day_start_equity > 0:
            daily_loss = (self._state.day_start_equity - equity) / self._state.day_start_equity
            if daily_loss >= self._limits.max_daily_loss_pct:
                self._halt(f"daily loss {daily_loss:.2%} >= {self._limits.max_daily_loss_pct:.2%}")

        if self._state.peak_equity > 0:
            dd = (self._state.peak_equity - equity) / self._state.peak_equity
            if dd >= self._limits.max_drawdown_pct:
                self._halt(f"drawdown {dd:.2%} >= {self._limits.max_drawdown_pct:.2%}")

    def force_halt(self, reason: str) -> None:
        """Externally trip the halt (e.g. reconciliation break or broker-health failure, §17/§18).

        Public because operational safety signals outside the P&L path must be able to stop
        new risk. Reductions/flattening remain allowed while halted (see `check_order`)."""
        self._halt(reason)

    def _halt(self, reason: str) -> None:
        if not self._state.halted:
            log.warning("RISK HALT: %s — new risk blocked until reset", reason)
        self._state.halted = True
        self._state.halt_reason = reason

    # ------------------------------------------------------------- operational safety
    def set_broker_connected(self, connected: bool) -> None:
        """Broker-health signal (§17). While disconnected, no orders may be sent."""
        if self._state.broker_connected and not connected:
            log.warning("BROKER DISCONNECTED — no new orders")
        self._state.broker_connected = connected

    def kill_switch(self, reason: str = "manual") -> None:
        """Emergency stop (§14): block ALL orders (including reductions) until reset."""
        if not self._state.killed:
            log.error("KILL SWITCH ENGAGED — all orders blocked (%s)", reason)
        self._state.killed = True
        self._state.kill_reason = reason

    def reset_kill(self) -> None:
        self._state.killed = False
        self._state.kill_reason = ""

    def update_limits(self, **changes: float) -> None:
        """Update risk limits at runtime (e.g. from the TRADING RISK config). Only known fields
        are accepted; the new caps take effect immediately for every subsequent `check_order`
        and `mark_equity` — the Risk Engine stays the single authority (§14)."""
        valid = {f.name for f in fields(self._limits)}
        for name, value in changes.items():
            if name not in valid:
                raise ValueError(f"unknown risk limit: {name}")
            setattr(self._limits, name, value)

    def check_order(
        self,
        order: Order,
        account: Account,
        *,
        price: float,
        current_qty: float,
        stop_distance: float = 0.0,
        correlation_fn: Callable[[str, str], float] | None = None,
    ) -> RiskDecision:
        """Approve or veto an order given the account state (§14). NO TRADE on any breach."""
        # --- hard stops: nothing goes out, not even a reduction --------------
        if self._state.killed:
            return RiskDecision(False, f"kill switch: {self._state.kill_reason}")
        if not self._state.broker_connected:
            return RiskDecision(False, "broker disconnected — no new orders")
        if price <= 0 or math.isnan(price):
            return RiskDecision(False, f"invalid price {price}")

        new_qty = current_qty + order.signed_quantity
        mult = order.instrument.multiplier
        reducing = abs(new_qty) < abs(current_qty)

        # You may always reduce risk (once past the hard stops), even while halted.
        if reducing:
            return RiskDecision(True, "risk-reducing")

        if self._state.halted:
            return RiskDecision(False, f"halted: {self._state.halt_reason}")

        equity = account.equity
        if equity <= 0:
            return RiskDecision(False, "non-positive equity")

        # Per-instrument gross notional cap.
        inst_notional = abs(new_qty) * price * mult
        if inst_notional / equity > self._limits.max_position_pct + 1e-9:
            return RiskDecision(
                False,
                f"position {inst_notional / equity:.1%} > max {self._limits.max_position_pct:.0%}",
            )

        # Gross leverage cap (replace this instrument's contribution in the gross total).
        key = order.instrument.key
        existing = account.positions.get(key)
        existing_notional = existing.notional if existing else abs(current_qty) * price * mult
        projected_gross = account.gross_exposure - existing_notional + inst_notional
        if projected_gross / equity > self._limits.max_gross_leverage + 1e-9:
            return RiskDecision(
                False,
                f"leverage {projected_gross / equity:.2f}x > max {self._limits.max_gross_leverage:.2f}x",
            )

        # Max open positions (only when opening a brand-new slot).
        opening_new = current_qty == 0 and new_qty != 0 and key not in account.positions
        if opening_new and len(account.positions) + 1 > self._limits.max_open_positions:
            return RiskDecision(
                False, f"open positions {len(account.positions) + 1} > max {self._limits.max_open_positions}"
            )

        # Per-trade risk: stop distance × added size vs equity budget.
        added = abs(new_qty) - abs(current_qty)
        trade_risk = added * stop_distance * mult
        if stop_distance > 0 and trade_risk / equity > self._limits.max_trade_risk_pct + 1e-9:
            return RiskDecision(
                False,
                f"trade risk {trade_risk / equity:.2%} > max {self._limits.max_trade_risk_pct:.2%}",
            )

        # Correlation exposure (§14): the new position plus its correlated cluster in the
        # existing book must not exceed the correlated-exposure limit.
        if correlation_fn is not None:
            cluster = inst_notional
            for k, p in account.positions.items():
                if k == key:
                    continue
                c = abs(correlation_fn(key, k))
                if c >= self._limits.correlation_threshold:
                    cluster += c * p.notional
            if cluster / equity > self._limits.max_correlated_exposure_pct + 1e-9:
                return RiskDecision(
                    False,
                    f"correlated exposure {cluster / equity:.0%} > max "
                    f"{self._limits.max_correlated_exposure_pct:.0%}",
                )

        return RiskDecision(True, "ok")
