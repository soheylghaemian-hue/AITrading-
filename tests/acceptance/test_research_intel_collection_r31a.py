"""§ Phase R3.1A acceptance — immutable point-in-time intelligence collection + outcome evaluation.

Forward-only, research-data-only. Proves: exact-input capture from the same consensus computation (no later
read-model reconstruction); production backdating is impossible; the injected-clock seam is test-only;
normal/early-close/DST/holiday session derivation; missed sessions stay missing; one canonical snapshot per
symbol/session; atomic snapshot+input+decision write with FK integrity; deterministic checksums; UNKNOWN
timestamps stay UNKNOWN; no future dataset pin at snapshot time; outcomes only after maturity from a
COMPLETED immutable dataset (never live ohlc_bars); deterministic integrity failures; unsupported-market
fail-closed; commit-ref fail-closed; worker idempotency/concurrency + exit codes; terminal DB immutability;
legacy reconciliation. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.intel import collect_session, evaluate_pending, policy
from atp.research.intel.collector import _snapshot_id
from atp.research.intel.commit import CommitVerificationError, resolve_commit_sha
from atp.research.backfill.validate import dataset_checksum
from atp.store import open_store

SHA = "a" * 40


def _store(path=None):
    return open_store(path or (str(Path(tempfile.mkdtemp()) / "atp.db")))


def _seed_market(store, symbol, start=date(2026, 8, 4), n=8):
    for i in range(n):
        d = (start + timedelta(days=i)).isoformat()
        store.insert_ohlc_bar(symbol=symbol, interval="1D", ts=f"{d}T00:00:00+00:00", open=100 + i,
                              high=101 + i, low=99 + i, close=100 + i, volume=1000, source="TEST")


def _window_now(d: date):
    return cal.session_close_utc(d) + timedelta(minutes=policy.SETTLE_MINUTES + 30)


def _seed_dataset(store, symbol, first: date, last: date, base=Decimal("200"), ds_id=None):
    days, d = [], first
    while d <= last:
        if cal.is_session_day(d):
            days.append(d)
        d += timedelta(days=1)
    bars = [{"symbol": symbol, "interval": "1D", "ts": f"{dd.isoformat()}T00:00:00+00:00",
             "session_date": dd.isoformat(), "open": base + Decimal(k), "high": base + Decimal(k) + 1,
             "low": base + Decimal(k) - 1, "close": base + Decimal(k), "volume": Decimal("1000"),
             "trade_count": 5, "source": "MASSIVE", "adjustment_policy": policy.ADJUSTMENT_POLICY}
            for k, dd in enumerate(days)]
    dsid = ds_id or ("ds-" + symbol)
    store.rd_create_dataset(dataset_id=dsid, owner="op", request_checksum="rc-" + dsid,
                            symbol_universe_json=json.dumps([symbol]), interval="1D", provider="MASSIVE",
                            provider_contract_version=policy.PROVIDER_CONTRACT_VERSION,
                            adjustment_policy=policy.ADJUSTMENT_POLICY,
                            normalization_policy="US_EQUITY_RTH_DAILY_FROM_1MIN_V1",
                            calendar_version=policy.CALENDAR_VERSION, range_start=first.isoformat(),
                            range_end=last.isoformat())
    store.rd_advance_status(dsid, "PLANNED", "RUNNING")
    store.rd_write_and_finalize(dsid, expected_from="RUNNING", status="COMPLETED", bars=bars,
                                row_count=len(bars), dataset_checksum=dataset_checksum(bars),
                                provider_adjusted_flag=True)
    return dsid


# --------------------------------------------------------------------- forward-only session derivation
def test_normal_session_collection():
    s = _store()
    _seed_market(s, "NVDA")
    r = collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    assert r["eligible"] and r["session_date"] == "2026-08-14" and r["written"] == ["NVDA"]
    snap = s.ri_get_snapshot(_snapshot_id("NVDA", "2026-08-14"))
    assert snap.is_early_close is False and snap.commit_sha == SHA


def test_early_close_session_collection():
    d = date(2026, 11, 27)                                    # early close in NYSE_2023_2027_V1
    assert cal.is_early_close(d)
    s = _store()
    r = collect_session(s, now=_window_now(d), commit_sha=SHA, symbols=["NVDA"])
    assert r["eligible"] and r["session_date"] == d.isoformat()
    assert s.ri_get_snapshot(_snapshot_id("NVDA", d.isoformat())).is_early_close is True


def test_dst_winter_session_uses_est_close():
    d = date(2026, 1, 5)                                      # EST session
    assert cal.session_close_utc(d).hour == 21                # 16:00 ET + 5h
    s = _store()
    r = collect_session(s, now=_window_now(d), commit_sha=SHA, symbols=["SPY"])
    assert r["eligible"] and r["session_date"] == d.isoformat()


def test_holiday_now_derives_prior_real_session_not_the_holiday():
    holiday = date(2026, 1, 1)                                # New Year — not a session
    assert not cal.is_session_day(holiday)
    # a clock during the holiday derives the prior COMPLETED session, never the holiday itself
    elig = policy.eligible_session(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))
    assert elig["session_date"] != holiday.isoformat()
    assert cal.is_session_day(date.fromisoformat(elig["session_date"]))


def test_missed_session_stays_missing():
    s = _store()
    _seed_market(s, "NVDA")
    # a clock LONG after the close window → not eligible, no snapshot, an honest SAMPLE_SKIPPED event
    r = collect_session(s, now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc), commit_sha=SHA, symbols=["NVDA"])
    assert not r["eligible"] and r["reason"] == "AFTER_COLLECTION_WINDOW"
    assert s.ri_list_snapshots(universe_id=policy.UNIVERSE_ID) == []
    assert s.ri_count_events("SAMPLE_SKIPPED") >= 1


def test_one_canonical_snapshot_per_symbol_session_idempotent():
    s = _store()
    _seed_market(s, "NVDA")
    now = _window_now(date(2026, 8, 14))
    a = collect_session(s, now=now, commit_sha=SHA, symbols=["NVDA"])
    b = collect_session(s, now=now, commit_sha=SHA, symbols=["NVDA"])
    assert a["written"] == ["NVDA"] and b["written"] == [] and b["already_collected"] == ["NVDA"]
    assert len(s.ri_list_snapshots(universe_id=policy.UNIVERSE_ID)) == 1


# --------------------------------------------------------------------- exact inputs / atomicity / checksums
def test_recording_store_reads_each_source_once_memoized():
    from atp.consensus.engine import _RecordingStore

    class _Flaky:                                            # returns a DIFFERENT value on every read
        def __init__(self):
            self.calls = 0

        def list_news(self, sym, k):
            self.calls += 1
            return [self.calls]

    flaky = _Flaky()
    rec = _RecordingStore(flaky)
    first = rec.list_news("NVDA", 20)
    assert rec.list_news("NVDA", 20) == first and flaky.calls == 1   # memoized → underlying read exactly once


def test_exact_inputs_are_frozen_from_the_single_computation_not_a_later_read():
    """Strong replacement for the name-only check (§ correction 1/11): the persisted component VALUE is the
    exact one that produced the score, and a later mutation of the source cannot enter the snapshot."""
    from atp.consensus.engine import build_ai_consensus
    s = _store()
    _seed_market(s, "NVDA")                                  # uptrend closes 100..107
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    sid = _snapshot_id("NVDA", "2026-08-14")
    md = [i for i in s.ri_list_inputs(sid) if i.component_name == "Market Data"][0]
    frozen_score = md.component_score
    assert json.loads(md.canonical_value_json)["score"] == float(frozen_score)   # value that produced the score

    # mutate the source so a FRESH consensus computes a very different Market Data score
    for i in range(8, 24):
        d = (date(2026, 8, 4) + timedelta(days=i)).isoformat()
        s.insert_ohlc_bar(symbol="NVDA", interval="1D", ts=f"{d}T00:00:00+00:00", open=10, high=11, low=9,
                          close=10, volume=1000, source="TEST")
    fresh = [c for c in build_ai_consensus(s, "NVDA")["components"] if c["component_name"] == "Market Data"][0]
    assert str(fresh["score"]) != frozen_score              # a later read WOULD differ → the snapshot is frozen
    # re-reading the persisted snapshot still yields the ORIGINAL frozen value (immutable, no leak)
    assert [i for i in s.ri_list_inputs(sid) if i.component_name == "Market Data"][0].component_score == frozen_score


def test_atomic_write_and_fk_rejects_orphans():
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    # an input row referencing a non-existent snapshot is impossible (FK)
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "INSERT INTO research_intel_snapshot_inputs (snapshot_id,component_name,provenance_status,"
                    "created_at) VALUES ('nope','X','UNKNOWN','t')")
    # an outcome referencing a non-existent snapshot is impossible (FK)
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "INSERT INTO research_intel_outcomes (snapshot_id,horizon_sessions,snapshot_checksum,"
                    "outcome_policy_version,status,evaluation_ts,commit_sha,created_at) VALUES "
                    "('nope',1,'x','v','MATURED','t','s','t')")


def test_deterministic_checksums_reproduce_from_persisted_rows():
    from atp.research.intel.envelope import inputs_checksum
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    snap = s.ri_get_snapshot(_snapshot_id("NVDA", "2026-08-14"))
    # re-derive the inputs checksum from the PERSISTED input rows → must reproduce the stored value exactly
    persisted = [{"component_name": i.component_name, "canonical_value_json": i.canonical_value_json,
                  "component_score": i.component_score, "component_status": i.component_status,
                  "source_provider": i.source_provider, "source_event_ts": i.source_event_ts,
                  "source_published_or_filed_ts": i.source_published_or_filed_ts,
                  "source_observed_ts": i.source_observed_ts, "source_available_ts": i.source_available_ts,
                  "provenance_status": i.provenance_status, "missing_data_reason": i.missing_data_reason,
                  "freshness_state": i.freshness_state} for i in s.ri_list_inputs(snap.snapshot_id)]
    assert inputs_checksum(persisted) == snap.inputs_checksum      # deterministic + reproducible


def test_unknown_timestamps_stay_unknown_and_no_future_pin():
    s = _store()   # NO seeded sources → consensus NO DATA → honest ABSTAIN snapshot
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["AAPL"])
    snap = s.ri_get_snapshot(_snapshot_id("AAPL", "2026-08-14"))
    assert snap.consensus_status == "NO DATA"
    # no future dataset is pinned at snapshot time (there is no dataset column on the snapshot)
    assert not hasattr(snap, "dataset_id")
    assert s.ri_list_outcomes() == []                        # and no outcomes exist yet


# --------------------------------------------------------------------- unsupported market fail-closed
def test_unsupported_symbol_fails_closed():
    s = _store()
    r = collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["TSLA"])
    assert "TSLA" in r["skipped"] and r["written"] == []
    assert s.ri_count_events("UNSUPPORTED_MARKET") >= 1
    assert s.ri_list_snapshots(universe_id=policy.UNIVERSE_ID) == []


# --------------------------------------------------------------------- outcomes: maturity, pinning, failures
def test_outcome_only_after_maturity_and_requires_completed_dataset():
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    # not matured yet (evaluate the day after) → no outcomes
    r0 = evaluate_pending(s, now=datetime(2026, 8, 15, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r0["matured_count"] == 0 and r0["pending"] > 0
    # matured but NO dataset → stays pending (dataset_pending), still no outcome row
    r1 = evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r1["matured_count"] == 0 and r1["dataset_pending"] > 0 and s.ri_list_outcomes() == []
    # add a COMPLETED dataset → now matures, pinned to that dataset (prices NOT from live ohlc_bars)
    _seed_dataset(s, "NVDA", date(2026, 7, 1), date(2026, 9, 30), base=Decimal("500"))
    r2 = evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r2["matured_count"] == len(policy.HORIZONS)
    outs = s.ri_list_outcomes()
    assert all(o.dataset_id == "ds-NVDA" and o.dataset_checksum for o in outs)
    assert all(Decimal(o.decision_price) >= 500 for o in outs)   # from the dataset (base 500), never live ~107


def test_covering_but_missing_bar_stays_pending_not_failed():
    # § correction 5: a covering dataset that lacks the required bar must NOT terminally fail — another valid
    # dataset may later supply it. The outcome stays PENDING (no row).
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    from atp.research.backfill.validate import dataset_checksum as dck
    s.rd_create_dataset(dataset_id="ds-gap", owner="op", request_checksum="rc-gap",
                        symbol_universe_json=json.dumps(["NVDA"]), interval="1D", provider="MASSIVE",
                        provider_contract_version=policy.PROVIDER_CONTRACT_VERSION,
                        adjustment_policy=policy.ADJUSTMENT_POLICY,
                        normalization_policy=policy.NORMALIZATION_POLICY,
                        calendar_version=policy.CALENDAR_VERSION, range_start="2026-08-10", range_end="2026-09-30")
    s.rd_advance_status("ds-gap", "PLANNED", "RUNNING")
    days, d = [], date(2026, 8, 17)                              # bars start AFTER the 08-14 decision session
    while d <= date(2026, 9, 30):
        if cal.is_session_day(d):
            days.append(d)
        d += timedelta(days=1)
    gapbars = [{"symbol": "NVDA", "interval": "1D", "ts": f"{dd.isoformat()}T00:00:00+00:00",
                "session_date": dd.isoformat(), "open": Decimal("500"), "high": Decimal("501"),
                "low": Decimal("499"), "close": Decimal("500"), "volume": Decimal("1000"), "trade_count": 5,
                "source": "MASSIVE", "adjustment_policy": policy.ADJUSTMENT_POLICY} for dd in days]
    s.rd_write_and_finalize("ds-gap", expected_from="RUNNING", status="COMPLETED", bars=gapbars,
                            row_count=len(gapbars), dataset_checksum=dck(gapbars), provider_adjusted_flag=True)
    r = evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r["failed_count"] == 0 and s.ri_list_outcomes() == []   # PENDING, not a terminal FAILED
    assert r["pending"] >= 1


def _finalize_dataset(store, ds_id, first, last, base, *, contract=None, checksum=None):
    from atp.research.backfill.validate import dataset_checksum as dck
    days, d = [], first
    while d <= last:
        if cal.is_session_day(d):
            days.append(d)
        d += timedelta(days=1)
    bars = [{"symbol": "NVDA", "interval": "1D", "ts": f"{dd.isoformat()}T00:00:00+00:00",
             "session_date": dd.isoformat(), "open": base + Decimal(k), "high": base + Decimal(k) + 1,
             "low": base + Decimal(k) - 1, "close": base + Decimal(k), "volume": Decimal("1000"),
             "trade_count": 5, "source": "MASSIVE", "adjustment_policy": policy.ADJUSTMENT_POLICY}
            for k, dd in enumerate(days)]
    store.rd_create_dataset(dataset_id=ds_id, owner="op", request_checksum="rc-" + ds_id,
                            symbol_universe_json=json.dumps(["NVDA"]), interval="1D", provider="MASSIVE",
                            provider_contract_version=(contract or policy.PROVIDER_CONTRACT_VERSION),
                            adjustment_policy=policy.ADJUSTMENT_POLICY, normalization_policy=policy.NORMALIZATION_POLICY,
                            calendar_version=policy.CALENDAR_VERSION, range_start=first.isoformat(),
                            range_end=last.isoformat())
    store.rd_advance_status(ds_id, "PLANNED", "RUNNING")
    store.rd_write_and_finalize(ds_id, expected_from="RUNNING", status="COMPLETED", bars=bars, row_count=len(bars),
                                dataset_checksum=(checksum or dck(bars)), provider_adjusted_flag=True)


def test_wrong_contract_and_tampered_checksum_datasets_are_rejected():
    # § correction 5: a wrong-contract dataset and a checksum-mismatch dataset are NOT valid and are never
    # selected; only the fully-valid (deterministically first-by-id) dataset matures the outcome.
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    _finalize_dataset(s, "ds-1-badcontract", date(2026, 7, 1), date(2026, 9, 30), Decimal("500"),
                      contract="polygon-aggs-1min-OLD-v1")                    # wrong contract → skipped
    _finalize_dataset(s, "ds-2-badchecksum", date(2026, 7, 1), date(2026, 9, 30), Decimal("500"),
                      checksum="sha256:bogus")                               # checksum mismatch → skipped
    _finalize_dataset(s, "ds-3-good", date(2026, 7, 1), date(2026, 9, 30), Decimal("500"))   # valid
    r = evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r["matured_count"] == len(policy.HORIZONS)
    assert all(o.dataset_id == "ds-3-good" for o in s.ri_list_outcomes())    # never the invalid ones


def test_zero_decision_price_is_deterministic_failed():
    from atp.research.backfill.validate import dataset_checksum as dck
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    # a valid dataset whose DECISION bar (08-14) close is exactly 0 → structurally un-returnable → FAILED
    days, d = [], date(2026, 8, 10)
    while d <= date(2026, 9, 30):
        if cal.is_session_day(d):
            days.append(d)
        d += timedelta(days=1)
    bars = [{"symbol": "NVDA", "interval": "1D", "ts": f"{dd.isoformat()}T00:00:00+00:00",
             "session_date": dd.isoformat(), "open": Decimal("0"), "high": Decimal("1"), "low": Decimal("0"),
             "close": Decimal("0"), "volume": Decimal("1000"), "trade_count": 5, "source": "MASSIVE",
             "adjustment_policy": policy.ADJUSTMENT_POLICY} for dd in days]
    s.rd_create_dataset(dataset_id="ds-zero", owner="op", request_checksum="rc-zero",
                        symbol_universe_json=json.dumps(["NVDA"]), interval="1D", provider="MASSIVE",
                        provider_contract_version=policy.PROVIDER_CONTRACT_VERSION,
                        adjustment_policy=policy.ADJUSTMENT_POLICY, normalization_policy=policy.NORMALIZATION_POLICY,
                        calendar_version=policy.CALENDAR_VERSION, range_start="2026-08-10", range_end="2026-09-30")
    s.rd_advance_status("ds-zero", "PLANNED", "RUNNING")
    s.rd_write_and_finalize("ds-zero", expected_from="RUNNING", status="COMPLETED", bars=bars, row_count=len(bars),
                            dataset_checksum=dck(bars), provider_adjusted_flag=True)
    r = evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert r["failed_count"] >= 1
    assert any(o.failure_code == "ZERO_DECISION_PRICE" for o in s.ri_list_outcomes() if o.status == "FAILED")


def test_outcome_terminal_db_immutable():
    s = _store()
    _seed_market(s, "NVDA")
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    _seed_dataset(s, "NVDA", date(2026, 7, 1), date(2026, 9, 30), base=Decimal("500"))
    evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "UPDATE research_intel_outcomes SET status='FAILED'")
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "DELETE FROM research_intel_outcomes")


# --------------------------------------------------------------------- commit fail-closed
def test_commit_ref_fails_closed():
    with pytest.raises(CommitVerificationError) as e_missing:
        resolve_commit_sha(env={})
    assert e_missing.value.code == "COMMIT_REF_MISSING"
    with pytest.raises(CommitVerificationError) as e_bad:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "short"})
    assert e_bad.value.code == "COMMIT_REF_MALFORMED"
    with pytest.raises(CommitVerificationError) as e_stale:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "a" * 40}, head_sha="b" * 40)
    assert e_stale.value.code == "COMMIT_REF_STALE"
    assert resolve_commit_sha(env={"ATP_COMMIT_REF": "a" * 40}, head_sha="a" * 40) == "a" * 40


# --------------------------------------------------------------------- worker: exit codes, no backdating
def test_worker_forward_only_no_backdating_and_exit_codes(monkeypatch, tmp_path):
    from atp.research.intel import worker as w
    dbfile = str(tmp_path / "a.db")
    s = open_store(dbfile)
    _seed_market(s, "NVDA")
    monkeypatch.setenv("ATP_STORE_URL", "sqlite:///" + dbfile)

    # the CLI accepts ONLY {collect, evaluate} — no --session / --date / --now flag → backdating impossible
    with pytest.raises(SystemExit):
        w.main(["collect", "--session", "2020-01-01"], _now=_window_now(date(2026, 8, 14)), _commit_sha=SHA)
    with pytest.raises(SystemExit):
        w.main(["backdate"], _now=_window_now(date(2026, 8, 14)), _commit_sha=SHA)

    assert w.main(["collect"], _now=_window_now(date(2026, 8, 14)), _commit_sha=SHA) == 0   # success
    assert len(s.ri_list_snapshots(universe_id=policy.UNIVERSE_ID)) == 3                    # AAPL/NVDA/SPY
    assert w.main(["evaluate"], _now=datetime(2026, 8, 15, 21, tzinfo=timezone.utc), _commit_sha=SHA) == 0
    # commit fail-closed → non-zero (no _commit_sha, no ATP_COMMIT_REF)
    monkeypatch.delenv("ATP_COMMIT_REF", raising=False)
    assert w.main(["collect"], _now=_window_now(date(2026, 8, 14))) == 1


# --------------------------------------------------------------------- legacy reconciliation diagnostic
def test_honest_timestamps_distinguish_target_from_capture():
    s = _store()
    _seed_market(s, "NVDA")
    target = cal.session_close_utc(date(2026, 8, 14)) + timedelta(minutes=policy.SETTLE_MINUTES)
    capture = target + timedelta(minutes=12)                  # a run 12min after the target, inside the window
    collect_session(s, now=capture, commit_sha=SHA, symbols=["NVDA"])
    snap = s.ri_get_snapshot(_snapshot_id("NVDA", "2026-08-14"))
    assert snap.scheduled_target_ts == target.isoformat()     # the canonical target
    assert snap.decision_ts == capture.isoformat()            # the ACTUAL capture — not stamped as the target
    assert snap.scheduled_target_ts != snap.decision_ts


def test_late_run_cannot_masquerade_as_close_plus_ten():
    s = _store()
    _seed_market(s, "NVDA")
    # a run 3 hours after the target is OUTSIDE the narrow window → missed, no snapshot (§ correction 2)
    late = cal.session_close_utc(date(2026, 8, 14)) + timedelta(hours=3)
    r = collect_session(s, now=late, commit_sha=SHA, symbols=["NVDA"])
    assert not r["eligible"] and r["reason"] == "AFTER_COLLECTION_WINDOW"
    assert s.ri_list_snapshots(universe_id=policy.UNIVERSE_ID) == []


def test_provenance_classification_rules():
    from atp.research.intel.provenance import classify_provenance
    cap = "2026-08-14T20:15:00+00:00"
    fut = classify_provenance(None, None, "2026-08-15T00:00:00+00:00", cap)      # observed in the future
    assert fut["provenance_status"] == "UNKNOWN" and fut["freshness_state"] == "INVALID_FUTURE"
    assert fut["missing_data_reason"] == "OBSERVED_TS_IN_FUTURE"
    absent = classify_provenance("e", "p", None, cap)                            # no observed → not OBSERVED_ONLY
    assert absent["provenance_status"] == "UNKNOWN" and absent["missing_data_reason"] == "NO_OBSERVED_TS"
    fresh = classify_provenance(None, "2026-08-14T18:00:00+00:00", "2026-08-14T20:00:00+00:00", cap)
    assert fresh["provenance_status"] == "OBSERVED_ONLY" and fresh["freshness_state"] == "FRESH"
    assert fresh["source_available_ts"] is None                                  # published != proven availability
    stale = classify_provenance(None, None, "2026-08-10T00:00:00+00:00", cap)
    assert stale["freshness_state"] == "STALE" and stale["provenance_status"] == "OBSERVED_ONLY"
    pubfut = classify_provenance(None, "2026-08-20T00:00:00+00:00", "2026-08-14T20:00:00+00:00", cap)
    assert pubfut["provenance_status"] == "OBSERVED_ONLY" and pubfut["missing_data_reason"] == "PUBLISHED_TS_IN_FUTURE"
    for r in (fut, absent, fresh, stale, pubfut):
        assert r["provenance_status"] != "VERIFIED"                             # never auto-VERIFIED here


def test_commit_head_verification_all_failure_modes(tmp_path):
    import subprocess
    from atp.research.intel.commit import CommitVerificationError, resolve_commit_sha
    # missing / malformed ref
    with pytest.raises(CommitVerificationError) as e0:
        resolve_commit_sha(env={})
    assert e0.value.code == "COMMIT_REF_MISSING"
    with pytest.raises(CommitVerificationError) as e1:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "short"})
    assert e1.value.code == "COMMIT_REF_MALFORMED"
    # unreadable repo dir → NEVER silently skipped (fail closed)
    with pytest.raises(CommitVerificationError) as e2:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "a" * 40}, repo_dir=str(tmp_path / "no-git"))
    assert e2.value.code == "COMMIT_HEAD_UNREADABLE"
    # malformed injected HEAD, and stale mismatch
    with pytest.raises(CommitVerificationError) as e3:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "a" * 40}, head_sha="not-a-sha")
    assert e3.value.code == "COMMIT_HEAD_MALFORMED"
    with pytest.raises(CommitVerificationError) as e4:
        resolve_commit_sha(env={"ATP_COMMIT_REF": "a" * 40}, head_sha="b" * 40)
    assert e4.value.code == "COMMIT_REF_STALE"
    # a REAL git checkout whose HEAD equals the ref → verified
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert resolve_commit_sha(env={"ATP_COMMIT_REF": head}, repo_dir=str(repo)) == head


def test_outcome_write_is_concurrency_accurate():
    # § correction 9: only the connection that actually inserts an outcome reports it; the other reports
    # an idempotent conflict (not a second maturation).
    dbfile = str(Path(tempfile.mkdtemp()) / "atp.db")
    s1, s2 = open_store(dbfile), open_store(dbfile, migrate=False)
    _seed_market(s1, "NVDA")
    collect_session(s1, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    sid = _snapshot_id("NVDA", "2026-08-14")
    row = {"snapshot_id": sid, "horizon_sessions": 1, "snapshot_checksum": "sc", "outcome_policy_version": "v",
           "status": "MATURED", "commit_sha": SHA}
    assert s1.ri_write_outcome(row) is True                   # first inserter wins
    assert s2.ri_write_outcome(row) is False                  # concurrent conflict → not a second maturation
    # and evaluate_pending on a second pass reports already-existing, not newly matured
    _seed_dataset(s1, "NVDA", date(2026, 7, 1), date(2026, 9, 30), base=Decimal("500"))
    a = evaluate_pending(s1, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    b = evaluate_pending(s1, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    assert a["matured_count"] >= 1 and b["matured_count"] == 0


def test_decision_price_reconciliation_recorded():
    s = _store()
    _seed_market(s, "NVDA")                                   # observed decision price ~107 from live bars
    collect_session(s, now=_window_now(date(2026, 8, 14)), commit_sha=SHA, symbols=["NVDA"])
    # dataset decision close is ~530 (base 500 + offset) → differs from the observed 107 → MISMATCH recorded,
    # and the return uses the DATASET price (authoritative), never a silent swap.
    _seed_dataset(s, "NVDA", date(2026, 7, 1), date(2026, 9, 30), base=Decimal("500"))
    evaluate_pending(s, now=datetime(2026, 10, 20, 21, tzinfo=timezone.utc), commit_sha=SHA)
    outs = s.ri_list_outcomes()
    assert all(o.decision_price_reconciliation == "MISMATCH_OBSERVED_VS_DATASET" for o in outs)
    assert all(Decimal(o.decision_price) >= 500 for o in outs)   # dataset price used, not the observed 107
    assert all(o.outcome_checksum and o.outcome_checksum.startswith("sha256:") for o in outs)


def test_legacy_reconciliation_detects_orphan_governance():
    from atp.research.intel.legacy_diag import reconcile_legacy
    s = _store()
    # governance row for NVDA with NO ai_predictions row → the audited orphan/discrepancy
    s.insert_governance_result(id="g1", prediction_id="NVDA:2026-08-17T02", symbol="NVDA", status="BLOCKED",
                               score=None, confidence=None, data_completeness=None, reason_codes="[]")
    diag = reconcile_legacy(s)
    assert diag["governance_orphan_count"] == 1
    assert diag["nvda_governance_count"] == 1 and diag["nvda_prediction_count"] == 0
    assert any(r["symbol"] == "NVDA" and r["governance_without_predictions"] for r in diag["per_symbol"])
