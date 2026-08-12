"""Event specialist tests (§5/§8): calendar blackout & surprise lookup, flatten-into-event,
and trade-the-surprise."""

from datetime import datetime, timedelta, timezone

from atp.core.enums import Action, AssetClass, Regime
from atp.core.events import Instrument
from atp.features.engine import FeatureSet
from atp.macro import EconomicCalendar, Event
from atp.strategy.event import EventStrategy

AAPL = Instrument("AAPL", AssetClass.EQUITY)
NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _fs(ts=NOW, **over):
    base = dict(instrument=AAPL, ts=ts, price=100.0, n_bars=50, ready=True,
                sma_fast=100.0, sma_slow=100.0, close_std=1.0, trend=0.0, ret=0.0,
                realized_vol=0.02, vol_percentile=0.5, rel_volume=1.0)
    base.update(over)
    return FeatureSet(**base)


# --------------------------------------------------------------------------- calendar
def test_event_surprise_normalized():
    e = Event(NOW, AAPL.key, "earnings", expected=2.00, actual=2.20)
    assert abs(e.surprise - 0.10) < 1e-9          # 10% beat
    assert Event(NOW, AAPL.key, "earnings").surprise is None   # not released


def test_calendar_blackout_and_recent():
    cal = EconomicCalendar()
    future = Event(NOW + timedelta(hours=6), AAPL.key, "earnings", importance="high")
    cal.add(future)
    assert cal.in_blackout(AAPL.key, NOW, timedelta(hours=24)) is future
    assert cal.in_blackout(AAPL.key, NOW, timedelta(hours=2)) is None    # outside window

    past = Event(NOW - timedelta(hours=1), AAPL.key, "earnings", importance="high",
                 expected=2.0, actual=2.3)
    cal.add(past)
    got = cal.recent_surprise(AAPL.key, NOW, timedelta(hours=12), min_surprise=0.05)
    assert got is past


def test_calendar_importance_filter():
    cal = EconomicCalendar()
    cal.add(Event(NOW + timedelta(hours=3), AAPL.key, "minor", importance="low"))
    assert cal.in_blackout(AAPL.key, NOW, timedelta(hours=24), importance="high") is None


# --------------------------------------------------------------------------- strategy
def test_event_flattens_into_high_impact_event():
    cal = EconomicCalendar()
    cal.add(Event(NOW + timedelta(hours=6), AAPL.key, "earnings", importance="high"))
    s = EventStrategy(cal, blackout=timedelta(hours=24))
    sig = s.generate(_fs(), Regime.RANGE)
    assert sig is not None and sig.action is Action.CLOSE    # get flat before the binary event


def test_event_trades_positive_surprise():
    cal = EconomicCalendar()
    cal.add(Event(NOW - timedelta(hours=1), AAPL.key, "earnings", importance="high",
                  expected=2.0, actual=2.4))                 # +20% beat
    s = EventStrategy(cal, react=timedelta(hours=12), min_surprise=0.02)
    sig = s.generate(_fs(), Regime.RANGE)
    assert sig is not None and sig.action is Action.BUY


def test_event_trades_negative_surprise():
    cal = EconomicCalendar()
    cal.add(Event(NOW - timedelta(hours=1), AAPL.key, "earnings", importance="high",
                  expected=2.0, actual=1.6))                 # miss
    s = EventStrategy(cal, react=timedelta(hours=12), min_surprise=0.02)
    assert s.generate(_fs(), Regime.RANGE).action is Action.SELL


def test_event_ignores_small_surprise():
    cal = EconomicCalendar()
    cal.add(Event(NOW - timedelta(hours=1), AAPL.key, "earnings", importance="high",
                  expected=2.0, actual=2.01))                # 0.5% — below threshold
    s = EventStrategy(cal, min_surprise=0.02)
    assert s.generate(_fs(), Regime.RANGE) is None


def test_event_fires_once_per_state_transition():
    cal = EconomicCalendar()
    cal.add(Event(NOW + timedelta(hours=6), AAPL.key, "earnings", importance="high"))
    s = EventStrategy(cal)
    assert s.generate(_fs(), Regime.RANGE).action is Action.CLOSE   # transition idle->close
    assert s.generate(_fs(), Regime.RANGE) is None                  # still in blackout, no repeat


def test_event_idle_when_nothing_scheduled():
    s = EventStrategy(EconomicCalendar())
    assert s.generate(_fs(), Regime.RANGE) is None
