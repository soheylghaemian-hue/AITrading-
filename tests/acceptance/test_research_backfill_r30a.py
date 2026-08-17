"""§ Phase R3.0A acceptance — historical OHLC backfill & research data integrity (RESEARCH DATA ONLY).

Covers correction #11's validation matrix on IMMUTABLE, versioned research datasets built from split-adjusted
1-minute aggregates normalized to regular-session (RTH) daily bars — with NO trading, NO order/execution/
broker path, and NEVER touching live `ohlc_bars`:

  RTH boundaries: 09:30 open INCLUDED, 16:00 close EXCLUDED, pre/after-hours excluded, early-close 13:00
  boundary, DST (winter 14:30 UTC vs summer 13:30 UTC open); session membership by NY session date;
  minute ordering + uniqueness; OHLC invariants; Decimal adjusted volume never rounded; returned
  adjustment flag verified (mismatch → FAILED); pagination + raw-page checksum; retry/backoff; complete
  vs incomplete (in-progress) session; missing-minute threshold + missing-open rejection; deterministic
  normalized dataset checksum; DB-enforced immutability of COMPLETED and FAILED datasets; retry linkage;
  supersedes as a DERIVED relationship (no mutation, no SUPERSEDED status); idempotent reuse + RUNNING
  conflict; bounds; canonical request checksum; explicit dataset pinning (no implicit 'latest').
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.backfill import (
    build_request, run_backfill, enqueue_backfill, claim_dataset, reclaim_stale, process_one,
    claim_next_one, execute_dataset, validate_selection, MockAggregatesProvider, MinuteBar, CHUNK_SESSIONS,
    dataset_checksum, raw_pages_checksum, validate_minutes, validate_daily_bars,
)
from atp.research.backfill import normalize as norm
from atp.research.backfill.dataset import DatasetRequestError
from atp.research.backfill.provider import EntitlementError, PolygonAggregatesProvider, ProviderError
from atp.research.backfill.runner import BackfillConflict
from atp.research.backfill.validate import ValidationError
from atp.store import open_store

NOW = datetime(2023, 12, 1, tzinfo=timezone.utc)     # after every fixture session below


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def rth_minutes(d, base=Decimal("100.00"), vol=Decimal("1000.5"), drop=(), tc=5):
    """A full RTH session of 1-minute bars for date d, minus any minute indices in `drop`."""
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    out, t, i = [], o, 0
    while t < c:
        if i not in set(drop):
            px = base + Decimal(i) * Decimal("0.01")
            out.append(MinuteBar(ts=t, open=px, high=px + Decimal("0.10"), low=px - Decimal("0.10"),
                                 close=px + Decimal("0.02"), volume=vol, trade_count=tc))
        t += timedelta(minutes=1)
        i += 1
    return out


def _mb(ts, px, vol=Decimal("1000.5"), tc=5):
    return MinuteBar(ts=ts, open=px, high=px + Decimal("0.1"), low=px - Decimal("0.1"),
                     close=px, volume=vol, trade_count=tc)


# --------------------------------------------------------------------- RTH boundaries / membership
def test_open_included_close_excluded():
    d = date(2023, 1, 3)                              # regular winter session
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    mins = rth_minutes(d)
    assert mins[0].ts == o                            # first eligible minute is exactly 09:30 ET
    assert mins[-1].ts == c - timedelta(minutes=1)    # last is 15:59, not 16:00
    out = norm.normalize_minutes_to_daily("NVDA", mins, o, c, now=NOW)
    assert len(out["bars"]) == 1 and out["out_of_session_minutes"] == 0
    assert norm.expected_rth_minutes(d) == 390


def test_premarket_and_afterhours_excluded():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    pre = [_mb(o - timedelta(minutes=k), Decimal("99")) for k in range(1, 6)]       # 5 pre-market
    post = [_mb(c + timedelta(minutes=k), Decimal("101")) for k in range(0, 5)]     # 16:00 + after
    mins = pre + rth_minutes(d) + post
    out = norm.normalize_minutes_to_daily("NVDA", mins, o, c, now=NOW)
    assert out["out_of_session_minutes"] == 10        # 5 pre + (16:00 boundary + 4 after) excluded
    b = out["bars"][0]
    assert b["open"] == rth_minutes(d)[0].open        # open is the 09:30 minute, not pre-market
    assert b["high"] <= Decimal("100") + Decimal("390") * Decimal("0.01") + Decimal("0.1")  # no 101 after-hours


def test_early_close_boundary():
    d = date(2023, 11, 24)                            # Black Friday — early close 13:00 ET
    assert cal.is_early_close(d) and norm.expected_rth_minutes(d) == 210
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    at_close = [_mb(c, Decimal("100"))]               # the 13:00 minute must be excluded
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d) + at_close, o, c, now=NOW)
    assert len(out["bars"]) == 1 and out["out_of_session_minutes"] == 1


def test_dst_open_differs_winter_vs_summer():
    winter, summer = date(2023, 1, 3), date(2023, 6, 20)
    assert cal.session_open_utc(winter).hour == 14 and cal.session_open_utc(winter).minute == 30   # EST 14:30Z
    assert cal.session_open_utc(summer).hour == 13 and cal.session_open_utc(summer).minute == 30   # EDT 13:30Z
    for d in (winter, summer):
        o, c = cal.session_open_utc(d), cal.session_close_utc(d)
        out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d), o, c, now=NOW)
        assert len(out["bars"]) == 1 and out["bars"][0]["session_date"] == d.isoformat()


def test_incomplete_current_session_excluded():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    mid_session = o + timedelta(minutes=100)          # "now" is BEFORE the close → session in progress
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d), o, c, now=mid_session)
    assert out["bars"] == []
    assert out["missing_sessions"][0]["reason"] == "INCOMPLETE_CURRENT_SESSION"


def test_missing_minute_threshold_rejects_sparse_session():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    drop = tuple(range(50, 390))                      # keep only 50/390 (~13%) < 90%
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, drop=drop), o, c, now=NOW)
    assert out["bars"] == []
    assert out["missing_sessions"][0]["reason"] == "INSUFFICIENT_SESSION_MINUTES"


def test_missing_open_minute_rejects_session_even_if_dense():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, drop=(0,)), o, c, now=NOW)  # drop 09:30
    assert out["bars"] == []
    m = out["missing_sessions"][0]
    assert m["reason"] == "INSUFFICIENT_SESSION_MINUTES" and m["open_minute_present"] is False


def test_missing_close_minute_excludes_normal_session():
    d = date(2023, 1, 3)                              # normal session; final RTH minute is 15:59
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    mins = rth_minutes(d)
    assert mins[-1].ts == c - timedelta(minutes=1)   # 15:59 present in a full session
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, drop=(389,)), o, c, now=NOW)  # drop 15:59
    assert out["bars"] == []
    m = out["missing_sessions"][0]
    assert m["reason"] == "INSUFFICIENT_SESSION_MINUTES"
    assert m["open_minute_present"] is True and m["close_minute_present"] is False


def test_missing_close_minute_excludes_early_close_session():
    d = date(2023, 11, 24)                            # early close 13:00 → final RTH minute is 12:59
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    exp = norm.expected_rth_minutes(d)               # 210
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, drop=(exp - 1,)), o, c, now=NOW)  # drop 12:59
    assert out["bars"] == []
    assert out["missing_sessions"][0]["close_minute_present"] is False


def test_complete_normal_and_early_close_sessions_normalize():
    normal, early = date(2023, 1, 3), date(2023, 11, 24)
    for d in (normal, early):
        o, c = cal.session_open_utc(d), cal.session_close_utc(d)
        out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d), o, c, now=NOW)
        assert len(out["bars"]) == 1 and out["missing_sessions"] == []
        bar = out["bars"][0]
        # close is the ACTUAL final RTH minute's close (never manufactured from an earlier minute)
        assert bar["close"] == rth_minutes(d)[-1].close


def test_extra_afterhours_minutes_do_not_compensate_missing_rth():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    dense_ah = [_mb(c + timedelta(minutes=k), Decimal("101")) for k in range(400)]  # lots of after-hours
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, drop=tuple(range(50, 390))) + dense_ah,
                                          o, c, now=NOW)
    assert out["bars"] == []                          # after-hours volume can't rescue a sparse RTH session


# --------------------------------------------------------------------- minute + daily validation
def test_minute_ordering_and_uniqueness_enforced():
    d = date(2023, 1, 3)
    mins = rth_minutes(d)
    with pytest.raises(ValidationError):
        validate_minutes("NVDA", [mins[1], mins[0]])          # out of order
    with pytest.raises(ValidationError):
        validate_minutes("NVDA", [mins[0], mins[0]])          # duplicate ts


def test_minute_ohlc_invariants_enforced():
    ts = cal.session_open_utc(date(2023, 1, 3))
    bad = MinuteBar(ts=ts, open=Decimal("10"), high=Decimal("9"), low=Decimal("11"),
                    close=Decimal("10"), volume=Decimal("1"))
    with pytest.raises(ValidationError):
        validate_minutes("NVDA", [bad])
    neg_vol = MinuteBar(ts=ts, open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
                        close=Decimal("10"), volume=Decimal("-1"))
    with pytest.raises(ValidationError):
        validate_minutes("NVDA", [neg_vol])


def test_daily_bar_membership_and_invariants_enforced():
    good = {"symbol": "NVDA", "interval": "1D", "ts": "2023-01-03T00:00:00+00:00",
            "session_date": "2023-01-03", "open": "10", "high": "12", "low": "9", "close": "11",
            "volume": "100", "trade_count": 3, "adjustment_policy": norm.ADJUSTMENT_POLICY}
    validate_daily_bars([good])                                # ok
    holiday = {**good, "ts": "2023-01-01T00:00:00+00:00", "session_date": "2023-01-01"}
    with pytest.raises(ValidationError):
        validate_daily_bars([holiday])                         # not a session day
    bad_ohlc = {**good, "high": "8"}
    with pytest.raises(ValidationError):
        validate_daily_bars([bad_ohlc])


def test_decimal_adjusted_volume_never_rounded():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    out = norm.normalize_minutes_to_daily("NVDA", rth_minutes(d, vol=Decimal("1000.5")), o, c, now=NOW)
    vol = out["bars"][0]["volume"]
    assert isinstance(vol, Decimal) and vol == Decimal("1000.5") * 390 == Decimal("390195.0")


def test_trade_count_null_when_any_minute_missing_it():
    d = date(2023, 1, 3)
    o, c = cal.session_open_utc(d), cal.session_close_utc(d)
    mins = rth_minutes(d)
    mins[10] = MinuteBar(ts=mins[10].ts, open=mins[10].open, high=mins[10].high, low=mins[10].low,
                         close=mins[10].close, volume=mins[10].volume, trade_count=None)
    out = norm.normalize_minutes_to_daily("NVDA", mins, o, c, now=NOW)
    assert out["bars"][0]["trade_count"] is None


# --------------------------------------------------------------------- provider: pagination, retry, probe
def test_pagination_and_raw_pages_checksum_sensitivity():
    d0, d1 = date(2023, 1, 3), date(2023, 1, 4)
    mins = {"NVDA": rth_minutes(d0) + rth_minutes(d1)}
    p = MockAggregatesProvider(mins, adjusted=True, page_size=200)
    fetched = p.fetch_minutes("NVDA", "2023-01-03", "2023-01-04")
    assert len(fetched.pages) > 1                              # multiple raw pages
    ck1 = raw_pages_checksum({"NVDA": fetched.pages})
    # mutating one page's data changes the raw-pages checksum
    pages2 = [dict(pg) for pg in fetched.pages]
    pages2[0] = {**pages2[0], "results": pages2[0]["results"][:-1]}
    assert raw_pages_checksum({"NVDA": pages2}) != ck1


def test_provider_retry_then_success(monkeypatch):
    import urllib.error
    import urllib.request
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)
        return FakeResp(json.dumps({"status": "OK", "adjusted": True, "results": []}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    p = PolygonAggregatesProvider("secret-key", delay_s=0, max_retries=5)
    body = p._get("https://api.polygon.io/x")
    assert body["status"] == "OK" and calls["n"] == 3


def test_provider_retry_exhausted_and_entitlement(monkeypatch):
    import urllib.error
    import urllib.request

    def five_hundred(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", five_hundred)
    with pytest.raises(ProviderError):
        PolygonAggregatesProvider("k", delay_s=0, max_retries=1)._get("https://x")

    def four_oh_one(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", four_oh_one)
    with pytest.raises(EntitlementError):
        PolygonAggregatesProvider("k", delay_s=0)._get("https://x")


def test_probe_shapes():
    assert PolygonAggregatesProvider(None).probe("NVDA", "2023-01-03")["configured"] is False
    r = MockAggregatesProvider({}, adjusted=True).probe("NVDA", "2023-01-03")
    assert r["entitled"] and r["returned_adjusted"] is True and "request_id" in r


# --------------------------------------------------------------------- runner: end-to-end + integrity
def _two_symbol_provider(days, adjusted=True):
    mins = {"NVDA": [m for d in days for m in rth_minutes(d, Decimal("450"))],
            "AAPL": [m for d in days for m in rth_minutes(d, Decimal("130"))]}
    return MockAggregatesProvider(mins, adjusted=adjusted, page_size=500)


def test_backfill_completes_and_checksum_is_deterministic():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    req = build_request(["NVDA", "AAPL"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    a = run_backfill(_store(), req, _two_symbol_provider(days), owner="op", now=NOW)
    b = run_backfill(_store(), req, _two_symbol_provider(days), owner="op", now=NOW)
    assert a["status"] == "COMPLETED" and a["row_count"] == 4
    assert a["dataset_checksum"] == b["dataset_checksum"]      # deterministic across independent stores


def test_returned_adjustment_flag_mismatch_fails():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=False)
    res = run_backfill(_store(), req, prov, owner="op", now=NOW)
    assert res["status"] == "FAILED" and res["failure_code"] == "PROVIDER_ADJUSTMENT_MISMATCH"


def test_idempotent_reuse_completed():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    r1 = run_backfill(store, req, prov, owner="op", now=NOW)
    r2 = run_backfill(store, req, prov, owner="op", now=NOW)
    assert r2["reused"] and r2["dataset_id"] == r1["dataset_id"]


def test_running_conflict_no_second_dataset():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    enq = enqueue_backfill(store, req, owner="op")          # PLANNED
    assert enq["status"] == "PLANNED" and enq["created"]
    assert claim_dataset(store, enq["dataset_id"])          # a worker claims it → RUNNING
    # a synchronous run for the IDENTICAL request must not create a second dataset → conflict
    with pytest.raises(BackfillConflict):
        run_backfill(store, req, prov, owner="op", now=NOW)
    assert len(store.rd_list_datasets(limit=50)) == 1       # still exactly one dataset


def test_enqueue_performs_no_provider_io():
    """enqueue_backfill only creates a PLANNED row — it never fetches or normalizes (that is the worker)."""
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-31", now=NOW)
    enq = enqueue_backfill(store, req, owner="op")
    ds = store.rd_get_dataset(enq["dataset_id"])
    assert ds.status == "PLANNED" and ds.row_count is None and ds.dataset_checksum is None
    assert store.rd_count_bars(enq["dataset_id"]) == 0      # zero bars persisted by enqueue


def test_completed_and_failed_datasets_are_db_immutable():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    ds = run_backfill(store, req, prov, owner="op", now=NOW)["dataset_id"]

    def tamper(sql, params=()):
        with store.tx() as cur:
            store._exec(cur, sql, params)

    for sql, params in [
        ("UPDATE research_datasets SET status='RUNNING' WHERE dataset_id=?", (ds,)),
        ("DELETE FROM research_datasets WHERE dataset_id=?", (ds,)),
        ("UPDATE research_ohlc_bars SET close='0' WHERE dataset_id=?", (ds,)),
        ("DELETE FROM research_ohlc_bars WHERE dataset_id=?", (ds,)),
        ("INSERT INTO research_ohlc_bars (dataset_id,symbol,interval,ts,session_date,open,high,low,close,"
         "volume,trade_count,source,adjustment_policy,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (ds, "NVDA", "1D", "2023-01-05T00:00:00+00:00", "2023-01-05", "1", "1", "1", "1", "1", 1, "X", "Y", "now")),
    ]:
        with pytest.raises(Exception):
            tamper(sql, params)
    assert store.rd_get_dataset(ds).status == "COMPLETED"

    # a FAILED dataset is equally immutable
    bad = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=False)
    failed = run_backfill(store, build_request(["AAPL"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                          MockAggregatesProvider({"AAPL": [m for d in days for m in rth_minutes(d)]},
                                                 adjusted=False), owner="op", now=NOW)
    assert failed["status"] == "FAILED"
    with pytest.raises(Exception):
        tamper("UPDATE research_datasets SET status='COMPLETED' WHERE dataset_id=?", (failed["dataset_id"],))


def test_failed_then_retry_links_retry_of():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    f = run_backfill(store, req, MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]},
                                                        adjusted=False), owner="op", now=NOW)
    assert f["status"] == "FAILED"
    r = run_backfill(store, req, MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]},
                                                        adjusted=True), owner="op", now=NOW)
    assert r["status"] == "COMPLETED" and r["retry_of"] == f["dataset_id"]
    assert store.rd_get_dataset(f["dataset_id"]).status == "FAILED"        # predecessor untouched


def test_supersedes_is_derived_not_a_mutation():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    old = run_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                       MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True),
                       owner="op", now=NOW)
    old_id, old_ck = old["dataset_id"], old["dataset_checksum"]
    # a replacement over a different range declares it supersedes `old`
    days2 = [date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)]
    new = run_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-05", now=NOW),
                       MockAggregatesProvider({"NVDA": [m for d in days2 for m in rth_minutes(d)]}, adjusted=True),
                       owner="op", now=NOW, supersedes_dataset_id=old_id)
    assert store.rd_superseded_by(old_id) == [new["dataset_id"]]           # DERIVED read-model
    old_row = store.rd_get_dataset(old_id)
    assert old_row.status == "COMPLETED" and old_row.dataset_checksum == old_ck   # byte-for-byte unchanged
    assert store.rd_get_dataset(new["dataset_id"]).status != "SUPERSEDED"  # no such status exists


# --------------------------------------------------------------------- request bounds + canonical checksum
def test_request_bounds():
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA", "AAPL", "SPY", "QQQ"], "1D", "2023-01-03", "2023-06-01", now=NOW)  # >3 symbols
    with pytest.raises(DatasetRequestError):
        build_request(["TSLA"], "1D", "2023-01-03", "2023-06-01", now=NOW)                        # not approved
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA"], "1h", "2023-01-03", "2023-06-01", now=NOW)                        # intraday
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA"], "1D", "2022-12-30", "2023-06-01", now=NOW)                        # before start
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA"], "1D", "2023-01-03", "2029-01-01", now=NOW)                        # after last session


def test_request_checksum_is_canonical_and_policy_sensitive():
    a = build_request(["NVDA", "AAPL"], "1D", "2023-01-03", "2023-06-01", now=NOW)
    b = build_request(["AAPL", "NVDA"], "1D", "2023-01-03", "2023-06-01", now=NOW)   # order-independent
    assert a.request_checksum == b.request_checksum
    c = build_request(["NVDA", "AAPL"], "1D", "2023-01-03", "2023-06-02", now=NOW)   # different range
    assert c.request_checksum != a.request_checksum


# --------------------------------------------------------------------- explicit dataset pinning (selection)
def test_dataset_selection_requires_valid_completed_dataset():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    ds = run_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                      MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True),
                      owner="op", now=NOW)["dataset_id"]
    row, pin, errors = validate_selection(store, ds, ["NVDA"], "1D", "2023-01-03", "2023-01-04")
    assert not errors and pin["dataset_id"] == ds and pin["checksum"] == row.dataset_checksum
    # unknown dataset id → error (no implicit 'latest')
    _, _, e_missing = validate_selection(store, "does-not-exist", ["NVDA"], "1D", "2023-01-03", "2023-01-04")
    assert e_missing
    # requested symbol not in dataset → error
    _, _, e_sym = validate_selection(store, ds, ["AAPL"], "1D", "2023-01-03", "2023-01-04")
    assert e_sym
    # range not covered → error
    _, _, e_rng = validate_selection(store, ds, ["NVDA"], "1D", "2023-01-03", "2023-06-01")
    assert e_rng


def test_never_touches_live_ohlc_bars():
    """The backfill writes ONLY research tables — the live `ohlc_bars` table stays empty."""
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    run_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                 MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True),
                 owner="op", now=NOW)
    assert store._one("SELECT COUNT(*) FROM ohlc_bars")[0] == 0
    assert store._one("SELECT COUNT(*) FROM research_ohlc_bars")[0] == 2


# --------------------------------------------------------------------- R3.0A.1 chunked / bounded worker
def _many_sessions(n, start=date(2023, 1, 3)):
    out, d = [], start
    while len(out) < n:
        if cal.is_session_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


class _CountingProvider(MockAggregatesProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fetch_calls = 0

    def fetch_minutes(self, *a, **k):
        self.fetch_calls += 1
        return super().fetch_minutes(*a, **k)


def test_processing_is_chunked_and_bounded():
    days = _many_sessions(CHUNK_SESSIONS * 2 + 5)          # spans 3 chunks
    store = _store()
    req = build_request(["NVDA"], "1D", days[0].isoformat(), days[-1].isoformat(), now=NOW)
    prov = _CountingProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True, page_size=5000)
    enq = enqueue_backfill(store, req, owner="op")
    res = execute_dataset(store, enq["dataset_id"], provider=prov, now=NOW)
    assert res["status"] == "COMPLETED" and res["row_count"] == len(days)
    # exactly one bounded fetch PER CHUNK (never one multi-year request); 1 symbol × 3 chunks
    assert prov.fetch_calls == 3
    chunk_events = [e for e in store.rd_list_events(enq["dataset_id"]) if e.event_type == "CHUNK"]
    assert len(chunk_events) == 3
    assert [e.event_type for e in store.rd_list_events(enq["dataset_id"])][-1] == "COMPLETE"


def test_worker_reclaims_stale_running_then_retry_creates_new_dataset():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    enq = enqueue_backfill(store, req, owner="op")
    assert claim_dataset(store, enq["dataset_id"])         # RUNNING, but the worker then "crashes"
    with store.tx() as cur:                                # force a stale heartbeat
        store._exec(cur, "UPDATE research_datasets SET updated_at=? WHERE dataset_id=?",
                    ("2000-01-01T00:00:00+00:00", enq["dataset_id"]))
    reclaimed_ids = reclaim_stale(store, now=NOW, stale_after_s=1)
    assert enq["dataset_id"] in reclaimed_ids
    reclaimed = store.rd_get_dataset(enq["dataset_id"])
    assert reclaimed.status == "FAILED" and reclaimed.failure_code == "STALE_RUNNING_RECLAIMED"
    # a retry links to the reclaimed FAILED predecessor and creates a NEW dataset id (no reuse/mutation)
    enq2 = enqueue_backfill(store, req, owner="op")
    assert enq2["created"] and enq2["dataset_id"] != enq["dataset_id"]
    assert enq2["retry_of"] == enq["dataset_id"]


def test_only_one_claim_of_a_planned_dataset():
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    enq = enqueue_backfill(store, req, owner="op")
    assert claim_dataset(store, enq["dataset_id"]) is True     # first claim wins (PLANNED→RUNNING)
    assert claim_dataset(store, enq["dataset_id"]) is False    # the guarded UPDATE rejects a second claim


def test_retry_cannot_mutate_or_reuse_a_failed_terminal():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)
    f = run_backfill(store, req, MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]},
                                                        adjusted=False), owner="op", now=NOW)
    assert f["status"] == "FAILED"
    with pytest.raises(Exception):                             # terminal → any UPDATE rejected by trigger
        with store.tx() as cur:
            store._exec(cur, "UPDATE research_datasets SET status='RUNNING' WHERE dataset_id=?", (f["dataset_id"],))
    good = run_backfill(store, req, MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]},
                                                           adjusted=True), owner="op", now=NOW)
    assert good["dataset_id"] != f["dataset_id"] and good["status"] == "COMPLETED"   # NEW id, not reused


def test_persisted_dataset_checksum_reverifies_after_chunked_run():
    days = _many_sessions(CHUNK_SESSIONS + 3)             # 2 chunks
    store = _store()
    req = build_request(["NVDA"], "1D", days[0].isoformat(), days[-1].isoformat(), now=NOW)
    res = run_backfill(store, req, MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]},
                                                          adjusted=True, page_size=5000), owner="op", now=NOW)
    # the stored checksum equals a fresh checksum recomputed from the PERSISTED bars (select-time re-verify)
    _, pin, errors = validate_selection(store, res["dataset_id"], ["NVDA"], "1D",
                                        days[0].isoformat(), days[-1].isoformat())
    assert not errors and pin["checksum"] == res["dataset_checksum"]


# --------------------------------------------------------------------- pagination safety (real client)
class _FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pagination_page_limit_exceeded(monkeypatch):
    import urllib.request
    body = json.dumps({"status": "OK", "adjusted": True, "results": [],
                       "next_url": "https://api.polygon.io/v2/aggs/next?cursor=abc"}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(body))
    p = PolygonAggregatesProvider("secret", delay_s=0, max_retries=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03", max_pages=3)   # next_url never ends
    assert e.value.code == "PROVIDER_PAGE_LIMIT_EXCEEDED"


def test_cross_origin_next_url_rejected_before_credential_sent(monkeypatch):
    import urllib.request
    calls = {"n": 0, "hosts": []}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        calls["hosts"].append(req.host)
        return _FakeResp(json.dumps({"status": "OK", "adjusted": True, "results": [],
                                     "next_url": "https://evil.example.com/steal?apiKey=x"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    p = PolygonAggregatesProvider("secret", delay_s=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03", max_pages=5)
    assert e.value.code == "PROVIDER_UNSAFE_PAGE_URL"
    assert calls["n"] == 1                                # the cross-origin next_url was NEVER fetched
    assert all("evil.example.com" not in h for h in calls["hosts"])   # credential never sent off-origin


def test_downgraded_http_next_url_rejected(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(
        json.dumps({"status": "OK", "adjusted": True, "results": [],
                    "next_url": "http://api.polygon.io/v2/aggs/next?cursor=abc"}).encode()))   # http downgrade
    p = PolygonAggregatesProvider("secret", delay_s=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03", max_pages=5)
    assert e.value.code == "PROVIDER_UNSAFE_PAGE_URL"


# --------------------------------------------------------------------- R3.0A.2 reclaim atomicity
def _dbfile():
    return str(Path(tempfile.mkdtemp()) / "atp.db")


def test_reclaim_race_fresh_heartbeat_prevents_reclamation():
    """A legitimate worker writing a fresh heartbeat AFTER the stale candidate is selected but BEFORE the
    terminal flip must NOT be reclaimed (the flip re-checks updated_at < cutoff atomically)."""
    dbfile = _dbfile()
    s1, s2 = open_store(dbfile), open_store(dbfile, migrate=False)   # two independent connections
    enq = enqueue_backfill(s1, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW), owner="op")
    ds_id = enq["dataset_id"]
    assert claim_dataset(s1, ds_id)                                  # RUNNING
    with s1.tx() as cur:                                             # make it look stale
        s1._exec(cur, "UPDATE research_datasets SET updated_at=? WHERE dataset_id=?",
                 ("2000-01-01T00:00:00+00:00", ds_id))

    def heartbeat(candidate_id):                                     # injected between select and flip
        with s2.tx() as cur:
            s2._exec(cur, "UPDATE research_datasets SET updated_at=? WHERE dataset_id=? AND status='RUNNING'",
                     ("2099-01-01T00:00:00+00:00", candidate_id))

    reclaimed = s1.rd_reclaim_stale_running("2020-01-01T00:00:00+00:00", failure_code="STALE_RUNNING_RECLAIMED",
                                            failure_reason="x", _probe=heartbeat)
    assert reclaimed == []                                          # the fresh heartbeat won the race
    assert s1.rd_get_dataset(ds_id).status == "RUNNING"             # dataset stays RUNNING
    assert not [e for e in s1.rd_list_events(ds_id) if e.event_type == "RECLAIM"]   # no event left behind


def test_genuine_stale_reclaim_is_atomic_single_and_audited():
    store = _store()
    enq = enqueue_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW), owner="op")
    ds_id = enq["dataset_id"]
    assert claim_dataset(store, ds_id)
    with store.tx() as cur:
        store._exec(cur, "UPDATE research_datasets SET updated_at=? WHERE dataset_id=?",
                    ("2000-01-01T00:00:00+00:00", ds_id))
    assert reclaim_stale(store, now=NOW, stale_after_s=1) == [ds_id]
    ds = store.rd_get_dataset(ds_id)
    assert ds.status == "FAILED" and ds.failure_code == "STALE_RUNNING_RECLAIMED"
    reclaim_events = [e for e in store.rd_list_events(ds_id) if e.event_type == "RECLAIM"]
    assert len(reclaim_events) == 1 and reclaim_events[0].severity == "ERROR"     # one immutable reclaim event
    # idempotent: a second reclaim is a no-op (terminal); no second transition, no second event
    assert reclaim_stale(store, now=NOW, stale_after_s=1) == []
    assert len([e for e in store.rd_list_events(ds_id) if e.event_type == "RECLAIM"]) == 1


# --------------------------------------------------------------------- R3.0A.2 bounded worker (one dataset)
def test_process_one_touches_only_the_named_dataset():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    a = enqueue_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                         owner="op")["dataset_id"]
    b = enqueue_backfill(store, build_request(["AAPL"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                         owner="op")["dataset_id"]
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)],
                                   "AAPL": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    res = process_one(store, a, prov, now=NOW)
    assert res["status"] == "COMPLETED" and res["dataset_id"] == a
    assert store.rd_get_dataset(a).status == "COMPLETED"
    assert store.rd_get_dataset(b).status == "PLANNED"             # the OTHER PLANNED dataset is untouched


def test_process_one_rejects_unknown_terminal_and_running():
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    store = _store()
    prov = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    assert process_one(store, "does-not-exist", prov, now=NOW)["error_code"] == "DATASET_NOT_FOUND"
    done = run_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                        prov, owner="op", now=NOW)["dataset_id"]
    r = process_one(store, done, prov, now=NOW)
    assert r["error_code"] == "DATASET_NOT_PLANNED" and r["actual_status"] == "COMPLETED"
    running = enqueue_backfill(store, build_request(["AAPL"], "1D", "2023-01-03", "2023-01-04", now=NOW),
                               owner="op")["dataset_id"]
    assert claim_dataset(store, running)
    r2 = process_one(store, running, MockAggregatesProvider({"AAPL": []}, adjusted=True), now=NOW)
    assert r2["error_code"] == "DATASET_NOT_PLANNED" and r2["actual_status"] == "RUNNING"


def test_worker_cli_exit_codes(monkeypatch):
    from atp.research.backfill import worker as w
    days = [date(2023, 1, 3), date(2023, 1, 4)]
    dbfile = _dbfile()
    store = open_store(dbfile)
    good = MockAggregatesProvider({"NVDA": [m for d in days for m in rth_minutes(d)],
                                   "AAPL": [m for d in days for m in rth_minutes(d)]}, adjusted=True)
    monkeypatch.setenv("ATP_STORE_URL", "sqlite:///" + dbfile)
    monkeypatch.setenv("ATP_BACKFILL_ENABLED", "1")
    monkeypatch.setenv("MASSIVE_API_KEY", "k")

    assert w.main([], provider=good) == 2                          # no --dataset-id / --next → usage error
    enq = enqueue_backfill(store, build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW), owner="op")
    assert w.main(["--dataset-id", enq["dataset_id"]], provider=good) == 0            # COMPLETED → 0
    assert store.rd_get_dataset(enq["dataset_id"]).status == "COMPLETED"
    assert w.main(["--dataset-id", "nope"], provider=good) == 1                       # unknown → non-zero
    enq2 = enqueue_backfill(store, build_request(["AAPL"], "1D", "2023-01-03", "2023-01-04", now=NOW), owner="op")
    bad = MockAggregatesProvider({"AAPL": [m for d in days for m in rth_minutes(d)]}, adjusted=False)
    assert w.main(["--dataset-id", enq2["dataset_id"]], provider=bad) == 1            # FAILED result → non-zero
    assert store.rd_get_dataset(enq2["dataset_id"]).status == "FAILED"
    monkeypatch.delenv("ATP_BACKFILL_ENABLED")
    assert w.main(["--dataset-id", "anything"]) == 0                                  # disabled no-op → 0 (skipped)


# --------------------------------------------------------------------- R3.0A.2 provider parsing hardening
def test_malformed_port_next_url_is_unsafe_not_valueerror(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(
        json.dumps({"status": "OK", "adjusted": True, "results": [],
                    "next_url": "https://api.polygon.io:notaport/v2/aggs/next"}).encode()))
    p = PolygonAggregatesProvider("secret", delay_s=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03", max_pages=5)
    assert e.value.code == "PROVIDER_UNSAFE_PAGE_URL"


def test_invalid_json_is_deterministic_and_body_not_echoed(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b"<html>secret-ish body</html>"))
    p = PolygonAggregatesProvider("secret", delay_s=0, max_retries=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03")
    assert e.value.code == "PROVIDER_INVALID_JSON"
    assert "secret-ish" not in str(e.value)                        # the body is never echoed


def test_malformed_row_is_deterministic_provider_error(monkeypatch):
    import urllib.request
    body = json.dumps({"status": "OK", "adjusted": True,
                       "results": [{"t": "not-a-number", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(body))
    p = PolygonAggregatesProvider("secret", delay_s=0, max_retries=0)
    with pytest.raises(ProviderError) as e:
        p.fetch_minutes("NVDA", "2023-01-03", "2023-01-03")
    assert e.value.code == "PROVIDER_MALFORMED_ROW"


def test_malformed_provider_data_fails_dataset_not_stuck_running():
    store = _store()
    req = build_request(["NVDA"], "1D", "2023-01-03", "2023-01-04", now=NOW)

    class _Broken(MockAggregatesProvider):
        def fetch_minutes(self, *a, **k):
            raise ProviderError("bad row", code="PROVIDER_MALFORMED_ROW")

    res = run_backfill(store, req, _Broken({}, adjusted=True), owner="op", now=NOW)
    assert res["status"] == "FAILED" and res["failure_code"] == "PROVIDER_MALFORMED_ROW"
    assert store.rd_get_dataset(res["dataset_id"]).status == "FAILED"   # deterministically FAILED, not RUNNING


# --------------------------------------------------------------------- R3.0A.2 empty-session-range rejection
def test_weekend_only_and_holiday_only_ranges_rejected():
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA"], "1D", "2023-01-07", "2023-01-08", now=NOW)   # Sat+Sun → zero sessions
    with pytest.raises(DatasetRequestError):
        build_request(["NVDA"], "1D", "2023-01-16", "2023-01-16", now=NOW)   # MLK holiday → zero sessions
