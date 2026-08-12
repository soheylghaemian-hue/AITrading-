"""Context-feed tests (§5/§17): scheduled rates/events feeds, options chain feed, the hub,
and the LiveRunner refreshing feeds so a strategy sees live-updated context mid-run."""

from datetime import datetime, timedelta, timezone

import pytest

from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.feeds import (
    FeedHub,
    OptionsChainFeed,
    ScheduledEventsFeed,
    ScheduledRatesFeed,
)
from atp.macro import EconomicCalendar, Event, RatesTable
from atp.options import OptionsEngine

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
SPX = Instrument("SPX", AssetClass.INDEX)


# --------------------------------------------------------------------------- rates feed
async def test_scheduled_rates_feed_applies_when_due():
    rates = RatesTable()
    feed = ScheduledRatesFeed(rates, [
        (T0, "USD", 0.05),
        (T0 + timedelta(hours=1), "USD", 0.045),
        (T0 + timedelta(hours=2), "USD", 0.04),
    ])
    assert await feed.refresh(T0) == 1
    assert rates.rate("USD") == 0.05
    assert await feed.refresh(T0) == 0                     # nothing new
    assert await feed.refresh(T0 + timedelta(hours=2)) == 2  # both later changes apply
    assert rates.rate("USD") == 0.04
    assert rates.trend("USD") < 0                          # easing over the run


# --------------------------------------------------------------------------- events feed
async def test_scheduled_events_feed_schedules_then_reveals():
    cal = EconomicCalendar()
    ev = Event(T0 + timedelta(hours=6), SPX.key, "cpi", importance="high", expected=3.0, actual=3.4)
    feed = ScheduledEventsFeed(cal, [ev], horizon_hours=72)

    await feed.refresh(T0)                                  # schedule it (actual hidden)
    scheduled = cal.for_instrument(SPX.key)[0]
    assert scheduled.actual is None
    assert cal.in_blackout(SPX.key, T0, timedelta(hours=24)) is not None

    await feed.refresh(T0 + timedelta(hours=7))             # now past the event => reveal actual
    revealed = cal.for_instrument(SPX.key)[0]
    assert revealed.actual == 3.4
    assert cal.recent_surprise(SPX.key, T0 + timedelta(hours=7), timedelta(hours=12)) is not None


# --------------------------------------------------------------------------- options feed
async def test_options_chain_feed_updates_engine_from_spot():
    eng = OptionsEngine()
    spot = {"v": 100.0}
    feed = OptionsChainFeed(eng, SPX.key, lambda: spot["v"], base_iv=0.25)
    assert await feed.refresh(T0) == 1
    assert eng.features(SPX.key).atm_iv == pytest.approx(0.25)
    spot["v"] = 0.0
    assert await feed.refresh(T0) == 0                      # no spot => no update


# --------------------------------------------------------------------------- hub
async def test_feed_hub_refreshes_all_and_isolates_failures():
    rates = RatesTable()
    good = ScheduledRatesFeed(rates, [(T0, "USD", 0.05)])

    class _Bad:
        name = "bad"
        async def refresh(self, now):
            raise RuntimeError("boom")

    hub = FeedHub([good, _Bad()])
    result = await hub.refresh_all(T0)
    assert result["rates"] == 1
    assert result["bad"] == 0                               # failure isolated, not raised
    assert rates.rate("USD") == 0.05


# --------------------------------------------------------------------------- live integration
async def test_live_runner_refreshes_feeds_so_macro_strategy_trades():
    from atp.live import LiveRunner, ReplayFeed, build_paper_stack
    from atp.macro import RatesTable
    from atp.policy import TradingPolicy
    from atp.regime.classifier import RegimeClassifier
    from atp.strategy.macro import MacroStrategy

    rates = RatesTable()
    # An easing schedule spread across the run; the feed applies it as time passes.
    start = T0
    schedule = [(start + timedelta(minutes=i), "USD", 0.05 - 0.001 * i) for i in range(0, 40, 5)]
    ratefeed = ScheduledRatesFeed(rates, schedule)

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MacroStrategy(rates, trend_threshold=0.002)],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
    )
    bars = [Bar(SPX, p := 100 + 0.05 * i, p * 1.001, p * 0.999, p, 1000, start + timedelta(minutes=i))
            for i in range(60)]

    summary = await LiveRunner(
        desk=desk, broker=broker, feed=ReplayFeed(bars),
        feeds=FeedHub([ratefeed]), feed_refresh_every=1,
    ).run()

    assert summary.feed_refreshes > 0
    assert summary.feed_updates >= len(schedule)   # all scheduled rate changes were applied
    assert rates.trend("USD") < 0                  # easing detected over the run
    assert summary.executed > 0                    # macro strategy acted on the easing bias
