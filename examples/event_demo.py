"""Demo: the Event specialist — de-risk into events, trade the surprise (§5/§8).

    PYTHONPATH=src python3 examples/event_demo.py

Walks a clock past a scheduled earnings event and shows the specialist flatten beforehand, then
take the surprise's direction afterwards. Completes the 9 §8 specialists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from atp.core.enums import AssetClass, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.macro import EconomicCalendar, Event
from atp.strategy.event import EventStrategy

AAPL = Instrument("AAPL", AssetClass.EQUITY)
EARNINGS = datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc)   # after the close


def _fs(ts):
    return FeatureSet(instrument=AAPL, ts=ts, price=100.0, n_bars=50, ready=True,
                      sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                      realized_vol=0.02, vol_percentile=0.5, rel_volume=1.0)


def main() -> None:
    cal = EconomicCalendar()
    # Scheduled (not yet released) — a high-impact earnings print.
    cal.add(Event(EARNINGS, AAPL.key, "earnings", importance="high"))
    strat = EventStrategy(cal, blackout=timedelta(hours=24), react=timedelta(hours=12), min_surprise=0.02)

    print("=" * 64)
    print("  Event specialist (§8): AAPL earnings @", EARNINGS.strftime("%Y-%m-%d %H:%M"))
    print("=" * 64)
    clock = [
        EARNINGS - timedelta(hours=30),   # far out
        EARNINGS - timedelta(hours=6),     # inside blackout
        EARNINGS - timedelta(hours=3),     # still blackout (no repeat)
    ]
    for ts in clock:
        sig = strat.generate(_fs(ts), Regime.RANGE)
        print(f"    {ts:%m-%d %H:%M}  ->  {sig.action.value.upper()+': '+sig.rationale if sig else 'idle'}")

    # Earnings released: a beat.
    cal.add(Event(EARNINGS, AAPL.key, "earnings", importance="high", expected=2.00, actual=2.30))
    strat._prev.clear()  # new print => fresh state for the demo
    after = EARNINGS + timedelta(hours=1)
    sig = strat.generate(_fs(after), Regime.RANGE)
    print(f"    {after:%m-%d %H:%M}  ->  {sig.action.value.upper()+': '+sig.rationale if sig else 'idle'}  (beat)")
    print("=" * 64)
    print("  Flat across the binary event, then leans into the surprise. Fed by an events feed.")


if __name__ == "__main__":
    main()
