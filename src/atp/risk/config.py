"""Trading-risk configuration — the three user-set parameters that bound the whole system.

Deliberately minimal (§15): the human sets only capital, risk-per-trade and max-daily-loss. The
monetary limits are derived, and the Position Sizer / Risk Engine compute everything else
(position size, leverage, exposure) automatically — and may NEVER exceed these bounds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class TradingRiskConfig:
    capital: float = 100_000.0          # capital mandate (max capital the system may manage)
    risk_per_trade_pct: float = 0.01    # max fraction of capital lost on a single trade
    max_daily_loss_pct: float = 0.02    # max fraction of capital lost in one trading day

    def __post_init__(self) -> None:
        for name in ("capital", "risk_per_trade_pct", "max_daily_loss_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
            setattr(self, name, float(value))
        if not (self.capital > 0):
            raise ValueError("capital must be > 0")
        for name, v in (("risk_per_trade_pct", self.risk_per_trade_pct),
                        ("max_daily_loss_pct", self.max_daily_loss_pct)):
            if not (0 < v <= 1.0):
                raise ValueError(f"{name} must be in (0, 1] (a fraction, e.g. 0.01 = 1%)")
        if self.risk_per_trade_pct > self.max_daily_loss_pct + 1e-12:
            raise ValueError("risk_per_trade_pct may not exceed max_daily_loss_pct "
                             "(one trade cannot risk more than the whole day's budget)")

    @property
    def max_risk_per_trade_amount(self) -> float:
        return self.capital * self.risk_per_trade_pct

    @property
    def max_daily_loss_amount(self) -> float:
        return self.capital * self.max_daily_loss_pct

    def as_dict(self) -> dict:
        return {
            "capital": self.capital,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_risk_per_trade": self.max_risk_per_trade_amount,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_daily_loss": self.max_daily_loss_amount,
        }


def trading_risk_view(config: TradingRiskConfig, *, daily_pnl: float, halted: bool) -> dict:
    """The live TRADING RISK panel: derived monetary limits + today's usage + status.

    `status` is DAILY LIMIT REACHED when the Risk Engine has halted (its daily-loss latch) or the
    realized daily loss has met the configured amount — in both cases new trades are blocked for
    the rest of the day. Otherwise ACTIVE."""
    daily_loss_amount = max(0.0, -daily_pnl)
    remaining = max(0.0, config.max_daily_loss_amount - daily_loss_amount)
    limit_reached = bool(halted) or (config.max_daily_loss_amount > 0 and daily_loss_amount >= config.max_daily_loss_amount)
    return {
        **config.as_dict(),
        "current_daily_pnl": daily_pnl,
        "remaining_daily_risk": remaining,
        "status": "DAILY LOSS LIMIT REACHED" if limit_reached else "ACTIVE",
    }
