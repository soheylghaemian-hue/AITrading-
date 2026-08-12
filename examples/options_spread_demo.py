"""Demo: multi-leg option execution & settlement (§16/§5).

    PYTHONPATH=src python3 examples/options_spread_demo.py

Prices a call debit spread and a straddle (net debit + combined greeks), executes the spread
against the paper broker as two legs, then settles it at expiry to intrinsic value.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from atp.brokers.paper import PaperBroker
from atp.core.events import QuoteEvent
from atp.options import (
    black_scholes,
    execute_combo,
    settle_expiration,
    straddle,
    vertical_call_spread,
)

EXP = "20260220"
TS = datetime(2026, 1, 5, tzinfo=timezone.utc)
SPOT, T, R, IV = 100.0, 0.12, 0.02, 0.25


def main_sync() -> None:
    print("=" * 62)
    print("  Multi-leg option structures (§5/§16)")
    print("=" * 62)
    spread = vertical_call_spread("ACME", EXP, 100, 110)
    debit = spread.net_debit(spot=SPOT, T=T, r=R, iv=IV)
    g = spread.greeks(spot=SPOT, T=T, r=R, iv=IV)
    print(f"  100/110 call debit spread:  net debit ${debit:,.2f}")
    print(f"    greeks  delta={g.delta:+.2f}  gamma={g.gamma:+.4f}  "
          f"vega={g.vega/100:+.2f}/volpt  theta={g.theta/365:+.2f}/day")
    print(f"    max value at expiry = strike width × 100 = ${(110-100)*100:,.0f}")

    strd = straddle("ACME", EXP, 100)
    gs = strd.greeks(spot=SPOT, T=T, r=R, iv=IV)
    print(f"  ATM straddle:  net debit ${strd.net_debit(spot=SPOT, T=T, r=R, iv=IV):,.2f}  "
          f"delta={gs.delta:+.2f} (≈neutral)  vega={gs.vega/100:+.2f}/volpt")


async def main() -> None:
    main_sync()
    print("-" * 62)
    print("  Execute the spread, then settle at expiry")
    spread = vertical_call_spread("ACME", EXP, 100, 110)
    long_leg, short_leg = spread.legs

    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    broker.set_quote(QuoteEvent(long_leg.instrument, black_scholes(SPOT, 100, T, R, IV, "C"), black_scholes(SPOT, 100, T, R, IV, "C"), TS))
    broker.set_quote(QuoteEvent(short_leg.instrument, black_scholes(SPOT, 110, T, R, IV, "C"), black_scholes(SPOT, 110, T, R, IV, "C"), TS))

    res = await execute_combo(broker, spread, quantity=5)   # 5 spreads
    print(f"    executed 5 spreads: net cash ${res.net_cash:,.2f} (debit paid)")

    # At expiry the underlying rallies to 112 => spread worth its max (10 wide).
    settle_ts = datetime(2026, 2, 20, tzinfo=timezone.utc)
    settled = await settle_expiration(broker, {"ACME": 112.0}, settle_ts)
    acct = await broker.get_account()
    print(f"    settled {len(settled)} legs at spot 112 => realized P&L ${acct.realized_pnl:,.2f}")
    print(f"    (both calls ITM: long 100c intrinsic 12, short 110c intrinsic 2 => spread = 10 × 5 × 100)")

    print("-" * 62)
    print("  Physical assignment: a covered-call style short call gets assigned")
    from atp.brokers.base import Order as _Order
    from atp.core.enums import Side as _Side
    from atp.options import option
    short_call = option("ACME", EXP, 105, "C")
    b2 = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await b2.connect()
    b2.set_quote(QuoteEvent(short_call, 2.0, 2.0, TS))
    await b2.place_order(_Order(short_call, _Side.SELL, 1))     # short 1 call @ 2.00
    s = await settle_expiration(b2, {"ACME": 112.0}, settle_ts, style="physical")
    pos = (await b2.get_positions()).get("ACME:equity")
    print(f"    short 105c assigned at 112 => stock position {pos.quantity:+.0f} shares @ {pos.avg_price:.0f}"
          f"   (delivered; now short {int(s[0].shares)} shares)")
    print("=" * 62)
    print("  Cash settlement to intrinsic, or physical assignment into underlying shares.")


if __name__ == "__main__":
    asyncio.run(main())
