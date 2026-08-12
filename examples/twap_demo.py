"""Demo: time-scheduled execution — TWAP vs VWAP (§16).

    PYTHONPATH=src python3 examples/twap_demo.py

Works a large parent order across bars instead of firing it in one shot: TWAP releases equal
slices, VWAP weights them by a volume profile. Shows the position ramping up slice by slice,
each release still passing the Risk Engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from atp.brokers.base import Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, Side
from atp.core.events import Instrument, QuoteEvent
from atp.execution.engine import ExecutionEngine
from atp.execution.scheduler import ExecutionScheduler
from atp.risk.engine import RiskEngine, RiskLimits, RiskState

INST = Instrument("ACME", AssetClass.EQUITY)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


async def work(label: str, *, slices: int | None, profile: list[float] | None) -> None:
    broker = PaperBroker(1_000_000, commission_per_unit=0, min_commission=0, slippage_bps=0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.0, T0))
    risk = RiskEngine(limits=RiskLimits(max_position_pct=1.0, max_gross_leverage=5.0),
                      state=RiskState(1_000_000, 1_000_000))
    execution = ExecutionEngine(broker, risk)
    sched = ExecutionScheduler(execution, slices=slices or 4, volume_profile=profile)

    sched.submit_parent(Order(INST, Side.BUY, 400), price=100.0)
    print(f"  {label}: parent BUY 400 worked over bars")
    bar = 0
    while sched.has_work():
        bar += 1
        account = await broker.get_account()
        (res, _), = await sched.tick(account, price_fn=lambda k: 100.0, now=T0 + timedelta(minutes=bar))
        pos = (await broker.get_positions())[INST.key].quantity
        print(f"    bar {bar}: filled {res.result.fill.quantity:>5.0f}  ->  position {pos:>5.0f}"
              f"   (remaining to work: {abs(sched.working_qty(INST.key)):.0f})")


async def main() -> None:
    print("=" * 62)
    print("  Time-scheduled execution (§16)")
    print("=" * 62)
    await work("TWAP (4 equal slices)", slices=4, profile=None)
    print("-" * 62)
    await work("VWAP (profile 1-3-4-2)", slices=None, profile=[1, 3, 4, 2])
    print("=" * 62)
    print("  Spreading the order over time reduces footprint; each slice is risk-checked.")


if __name__ == "__main__":
    asyncio.run(main())
