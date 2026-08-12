"""Data Quality Engine tests (§10): every bad-data class is rejected (NO TRADE)."""

from datetime import datetime, timedelta, timezone

from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument, QuoteEvent
from atp.dataquality import DataQualityConfig, DataQualityEngine

INST = Instrument("X", AssetClass.EQUITY)
NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _q(bid, ask, ts=NOW):
    return QuoteEvent(INST, bid, ask, ts)


def _bar(o, h, l, c, v=1000, ts=NOW):
    return Bar(INST, o, h, l, c, v, ts)


def _eng():
    return DataQualityEngine(DataQualityConfig(max_quote_age_seconds=30, max_spread_bps=500,
                                               max_jump_pct=0.35))


# --------------------------------------------------------------------------- quotes
def test_good_quote_passes():
    assert _eng().check_quote(_q(99.9, 100.1), NOW).ok


def test_impossible_price_rejected():
    assert not _eng().check_quote(_q(0.0, 100.1), NOW).ok
    assert not _eng().check_quote(_q(-1, 100.1), NOW).ok
    assert not _eng().check_quote(_q(float("nan"), 100.1), NOW).ok


def test_crossed_book_rejected():
    r = _eng().check_quote(_q(101.0, 100.0), NOW)
    assert not r.ok and "crossed" in r.reason


def test_abnormal_spread_rejected():
    r = _eng().check_quote(_q(90.0, 110.0), NOW)   # ~2000 bps spread
    assert not r.ok and "spread" in r.reason


def test_stale_quote_rejected():
    r = _eng().check_quote(_q(100, 100.1, ts=NOW - timedelta(seconds=120)), NOW)
    assert not r.ok and "stale" in r.reason


def test_future_dated_timestamp_rejected():
    r = _eng().check_quote(_q(100, 100.1, ts=NOW + timedelta(seconds=60)), NOW)
    assert not r.ok and "future" in r.reason


def test_non_monotonic_and_duplicate_rejected():
    eng = _eng()
    assert eng.check_quote(_q(100, 100.1, ts=NOW), NOW).ok
    back = eng.check_quote(_q(100, 100.1, ts=NOW - timedelta(seconds=1)), NOW)
    assert not back.ok and "non-monotonic" in back.reason
    dup = eng.check_quote(_q(100, 100.1, ts=NOW), NOW)
    assert not dup.ok and "duplicate" in dup.reason


def test_impossible_jump_rejected():
    eng = _eng()
    assert eng.check_quote(_q(100, 100.1, ts=NOW), NOW).ok
    r = eng.check_quote(_q(200, 200.1, ts=NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1))
    assert not r.ok and "jump" in r.reason


def test_feed_disconnect_blocks_everything():
    eng = _eng()
    eng.set_connected(False)
    r = eng.check_quote(_q(100, 100.1), NOW)
    assert not r.ok and "disconnect" in r.reason


# --------------------------------------------------------------------------- bars
def test_ohlc_out_of_range_rejected():
    r = _eng().check_bar(_bar(100, 101, 99, 105), NOW)   # close 105 > high 101
    assert not r.ok and "OHLC" in r.reason


def test_negative_volume_rejected():
    r = _eng().check_bar(_bar(100, 101, 99, 100, v=-5), NOW)
    assert not r.ok and "volume" in r.reason


def test_good_bar_passes():
    assert _eng().check_bar(_bar(100, 101, 99, 100.5), NOW).ok


# --------------------------------------------------------------------------- heartbeat
def test_heartbeat_timeout_flags_silence():
    eng = DataQualityEngine(DataQualityConfig(heartbeat_timeout_seconds=10))
    eng.check_quote(_q(100, 100.1, ts=NOW), NOW)         # seen at NOW
    ok = eng.check_heartbeat(INST.key, NOW + timedelta(seconds=5))
    assert ok.ok
    silent = eng.check_heartbeat(INST.key, NOW + timedelta(seconds=30))
    assert not silent.ok and "silent" in silent.reason
