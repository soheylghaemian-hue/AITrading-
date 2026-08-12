"""Autonomous Trading Policy (§15).

The policy is the *one* human decision. It is set once (capital mandate, risk per trade,
loss limits, allowed asset classes, leverage, trading hours, max open positions) and from
then on the desk acts autonomously — BUY/SELL/HOLD/CLOSE/REDUCE/HEDGE — provided the Risk
Engine agrees (§15). Nothing here fabricates profitability; it only bounds behavior.

Implemented as a dependency-free dataclass that mimics the small slice of the pydantic API
the codebase uses (`model_copy(update=...)`), so the offline suite needs no third-party
packages. Treat instances as immutable — always go through `model_copy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime, time

from .core.enums import AssetClass
from .risk.engine import RiskLimits


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


@dataclass(slots=True)
class TradingPolicy:
    # --- Capital mandate ---------------------------------------------------
    capital: float = 100_000.0

    # --- Per-trade / per-day risk (fractions of equity) --------------------
    risk_per_trade: float = 0.01          # max fraction of equity risked on one trade
    daily_loss_limit: float = 0.03        # halt new risk after this daily drawdown
    max_portfolio_risk: float = 0.06      # aggregate open risk budget
    max_position_pct: float = 0.20        # max gross notional in one instrument / equity
    max_leverage: float = 1.0             # gross exposure / equity
    max_open_positions: int = 10

    # --- Universe ----------------------------------------------------------
    allowed_asset_classes: tuple[AssetClass, ...] = (
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.INDEX,
        AssetClass.COMMODITY,
        AssetClass.FX,
        AssetClass.FUTURE,
    )

    # --- Trading hours (wall-clock gate, honored live) ---------------------
    trading_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon–Fri
    trading_start: str = "00:00"
    trading_end: str = "23:59"

    # --- Data-quality gate -------------------------------------------------
    max_quote_age_seconds: float = 30.0

    # ---------------------------------------------------------------- API --
    def model_copy(self, *, update: dict | None = None) -> "TradingPolicy":
        """Return a copy with `update` applied (pydantic-compatible subset)."""
        if not update:
            return replace(self)
        valid = {f.name for f in fields(self)}
        unknown = set(update) - valid
        if unknown:
            raise ValueError(f"unknown policy fields: {sorted(unknown)}")
        return replace(self, **update)

    def allows(self, asset_class: AssetClass) -> bool:
        return asset_class in self.allowed_asset_classes

    def within_trading_hours(self, now: datetime) -> bool:
        """True if `now` falls inside the configured trading window."""
        if now.weekday() not in self.trading_days:
            return False
        t = now.timetz().replace(tzinfo=None)
        return _parse_hhmm(self.trading_start) <= t <= _parse_hhmm(self.trading_end)

    def to_risk_limits(self) -> RiskLimits:
        """Project the policy onto the independent Risk Engine's limit set (§14)."""
        return RiskLimits(
            max_daily_loss_pct=self.daily_loss_limit,
            max_drawdown_pct=max(self.daily_loss_limit * 3, 0.15),
            max_position_pct=self.max_position_pct,
            max_gross_leverage=self.max_leverage,
            max_open_positions=self.max_open_positions,
            max_trade_risk_pct=self.risk_per_trade,
            max_portfolio_risk_pct=self.max_portfolio_risk,
        )
