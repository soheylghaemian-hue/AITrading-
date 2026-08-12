"""Position sizing (§10/§15).

Risk-based sizing: risk a fixed fraction of equity per trade (policy `risk_per_trade`),
where "risk" is the stop distance × size. The result is then capped so a single instrument's
gross notional can't exceed `max_position_pct` of equity (§14/§15). This keeps every entry's
downside bounded *before* the Risk Engine's independent veto ever runs.
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
        if price <= 0 or equity <= 0:
            return 0.0

        if sizing == "hedged" and ref_price and ref_price > 0:
            # β-weighted market-neutral pairs sizing (§8): both legs derive base units from a
            # COMMON reference price, then scale by the hedge factor (1 for leg A, β for leg B).
            # This makes qty_b = β·qty_a, which neutralizes the shared move regardless of the
            # price levels / intercept (equal-notional would not).
            base_units = (self._neutral_notional_pct * equity) / ref_price
            units = base_units * hedge_factor
        elif sizing == "notional":
            units = (self._neutral_notional_pct * equity) / (price * multiplier)
        else:
            if stop_distance <= 0:
                return 0.0
            risk_budget = policy.risk_per_trade * equity
            units = risk_budget / (stop_distance * multiplier)

        # Cap by max gross notional per instrument.
        max_notional = policy.max_position_pct * equity
        units_cap = max_notional / (price * multiplier)
        units = min(units, units_cap)

        if self._whole_units:
            units = float(math.floor(units))
        return max(0.0, units)
