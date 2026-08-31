"""Position sizing (§10/§15).

Risk-based sizing risks a fixed fraction of effective capital per trade, where effective capital
is the smaller of account equity and the policy's capital mandate. The result is capped by the
same basis so a large broker account can never silently enlarge the mandate (§14/§15).
"""

from __future__ import annotations

import math

from ..policy import TradingPolicy


class PositionSizer:
    def __init__(self, *, whole_units: bool = True, neutral_notional_pct: float = 0.10) -> None:
        # Equities/futures trade in whole units; set False for FX/crypto fractional sizing.
        self._whole_units = whole_units
        # Fixed notional fraction of equity used by "notional" sizing (market-neutral pairs).
        self._neutral_notional_pct = neutral_notional_pct

    def target_units(
        self,
        *,
        price: float,
        stop_distance: float,
        equity: float,
        policy: TradingPolicy,
        multiplier: float = 1.0,
        sizing: str = "risk",
        hedge_factor: float = 1.0,
        ref_price: float | None = None,
    ) -> float:
        try:
            capital_limit = float(policy.capital)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if (
            not math.isfinite(price)
            or not math.isfinite(equity)
            or price <= 0
            or equity <= 0
            or math.isnan(capital_limit)
            or capital_limit <= 0
        ):
            return 0.0
        effective_equity = min(equity, capital_limit)

        if sizing == "hedged" and ref_price and ref_price > 0:
            # β-weighted market-neutral pairs sizing (§8): both legs derive base units from a
            # COMMON reference price, then scale by the hedge factor (1 for leg A, β for leg B).
            # This makes qty_b = β·qty_a, which neutralizes the shared move regardless of the
            # price levels / intercept (equal-notional would not).
            base_units = (self._neutral_notional_pct * effective_equity) / ref_price
            units = base_units * hedge_factor
        elif sizing == "notional":
            units = (self._neutral_notional_pct * effective_equity) / (price * multiplier)
        else:
            if stop_distance <= 0:
                return 0.0
            risk_budget = policy.risk_per_trade * effective_equity
            units = risk_budget / (stop_distance * multiplier)

        # Cap by max gross notional per instrument.
        max_notional = policy.max_position_pct * effective_equity
        units_cap = max_notional / (price * multiplier)
        units = min(units, units_cap)

        if self._whole_units:
            units = float(math.floor(units))
        return max(0.0, units)
