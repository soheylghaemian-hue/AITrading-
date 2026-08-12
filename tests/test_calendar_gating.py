"""Calendar session/holiday gating tests (§3): the desk does not trade when the market is
closed (outside session hours or on a holiday), and does when it is open."""

import math
from datetime import datetime, time, timedelta, timezone

from atp.backtest import Backtester
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.instruments import MarketCalendar
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("X", AssetClass.EQUITY)


def _bars(day, n=120, start_hour=15):
    """Oscillating bars on `day`, one per minute starting at start_hour:00 UTC."""
    base = datetime(day.year, day.month, day.day, start_hour, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        p = 100 + 4 * math.sin(i / 6.0) + 0.05 * i
        out.append(Bar(INST, p, p * 1.002, p * 0.998, p, 1000, base + timedelta(minutes=i)))
    return out


async def _run(bars, calendar):
    bt = Backtester(policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
                    regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
                    calendar=calendar)
    return await bt.run(bars)


async def test_no_trades_outside_session_hours():
    from datetime import date
    monday = date(2026, 1, 5)
    # Session 14:30–21:00 UTC. Bars at 22:00+ are after the close.
    cal = MarketCalendar(name="rth", session_open=time(14, 30), session_close=time(21, 0))
    res = await _run(_bars(monday, start_hour=22), cal)
    assert res.n_executed == 0                       # all bars after close => no trading


async def test_trades_during_session_hours():
    from datetime import date
    monday = date(2026, 1, 5)
    cal = MarketCalendar(name="rth", session_open=time(14, 30), session_close=time(21, 0))
    res = await _run(_bars(monday, start_hour=15), cal)   # 15:00 UTC is in session
    assert res.n_executed > 0


async def test_no_trades_on_holiday():
    from datetime import date
    monday = date(2026, 1, 5)
    cal = MarketCalendar(name="rth", session_open=time(0, 0), session_close=time(23, 59),
                         holidays=frozenset({"2026-01-05"}))
    res = await _run(_bars(monday, start_hour=15), cal)
    assert res.n_executed == 0                       # the whole day is a holiday


async def test_no_calendar_is_unchanged():
    from datetime import date
    monday = date(2026, 1, 5)
    # Without a calendar the desk uses the policy's default (all-hours) => trades normally.
    res = await _run(_bars(monday, start_hour=15), None)
    assert res.n_executed > 0
