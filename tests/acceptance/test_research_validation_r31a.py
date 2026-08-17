"""§ Phase R3.1A acceptance — deterministic AI-prediction-quality validation (RESEARCH ONLY).

Proves: confusion / accuracy / precision-recall correctness; slices by horizon/symbol/regime/confidence
bucket; naive benchmarks; heuristic confidence is NEVER treated as a probability (Brier/log-loss/ECE NOT
APPLICABLE); raw vs effective sample counts; the frozen evidence gate yields INSUFFICIENT below threshold;
the validation run is deterministic (stable result checksum) and DB-immutable when terminal; the runner
worker fails closed on an unverifiable commit. Kept strictly separate from trading P&L.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.intel import policy
from atp.research.validation import benchmarks as bm
from atp.research.validation import calibration as calib
from atp.research.validation import metrics as mx
from atp.research.validation import run_validation
from atp.store import open_store

SHA = "a" * 40


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _sample(sym, sess, h, exp, act, conf, regime="UPTREND"):
    return {"symbol": sym, "session_date": sess, "horizon": h, "expected": exp, "actual": act,
            "correct": (None if exp not in ("BULLISH", "BEARISH", "NEUTRAL") else exp == act),
            "confidence": conf, "regime": regime, "return_pct": 1.0}


def test_confusion_accuracy_precision_recall():
    samples = [
        _sample("NVDA", "2026-01-05", 5, "BULLISH", "BULLISH", 80),
        _sample("NVDA", "2026-01-06", 5, "BULLISH", "BEARISH", 60),
        _sample("NVDA", "2026-01-07", 5, "BEARISH", "BEARISH", 90),
        _sample("NVDA", "2026-01-08", 5, "BEARISH", "BULLISH", 55),
    ]
    m = mx.compute(samples)
    overall = m["overall"]
    assert overall["graded"] == 4 and overall["accuracy"] == 0.5
    bull = overall["per_class"]["BULLISH"]
    assert bull["tp"] == 1 and bull["fp"] == 1 and bull["fn"] == 1
    assert bull["precision"] == 0.5 and bull["recall"] == 0.5
    assert m["by_horizon"]["5"]["graded"] == 4
    assert "UPTREND" in m["by_regime"]


def test_heuristic_confidence_never_probabilistic():
    m = mx.compute([_sample("NVDA", "2026-01-05", 1, "BULLISH", "BULLISH", 80)])
    assert m["probabilistic_calibration"] == "NOT APPLICABLE"
    for bucket in m["by_confidence_bucket"].values():
        assert bucket["confidence_is_probability"] is False
    c = calib.probabilistic_calibration()
    assert c["brier_score"] == "NOT APPLICABLE" and c["log_loss"] == "NOT APPLICABLE"
    assert c["expected_calibration_error"] == "NOT APPLICABLE" and c["confidence_is_probability"] is False


def test_benchmarks_present():
    samples = [_sample("SPY", "2026-01-05", 1, "BULLISH", "BULLISH", 70),
               _sample("SPY", "2026-01-06", 1, "BEARISH", "BULLISH", 70)]
    b = bm.compute(samples)
    assert "always_bullish_accuracy" in b["1"] and "naive_persistence_accuracy" in b["1"]
    assert b["1"]["market_spy_direction_distribution"]["BULLISH"] == 1.0


def test_run_is_insufficient_and_deterministic_and_immutable():
    s = _store()   # no matured outcomes → far below the gate
    r1 = run_validation(s, commit_sha=SHA, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert r1["status"] == "INSUFFICIENT" and not r1["gate_passed"]
    run = s.rv_get_run(r1["run_id"])
    assert run.status == "INSUFFICIENT" and run.commit_sha == SHA and run.result_checksum
    # deterministic: a second run over the same frozen (empty) set yields the SAME result checksum
    r2 = run_validation(s, commit_sha=SHA, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert r2["result_checksum"] == r1["result_checksum"]
    # terminal run is DB-immutable
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "UPDATE research_validation_runs SET status='COMPLETED' WHERE run_id=?", (r1["run_id"],))
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "DELETE FROM research_validation_runs")
    # metrics rows are immutable too
    with pytest.raises(Exception):
        with s.tx() as c:
            s._exec(c, "UPDATE research_validation_metrics SET metrics_json='x'")


def test_gate_reports_each_criterion():
    s = _store()
    r = run_validation(s, commit_sha=SHA, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    run = s.rv_get_run(r["run_id"])
    from atp.research.validation import readmodel
    det = readmodel.run_detail(s, run)
    crit = det["gate_report"]["criteria"]
    for name in ("unique_sessions", "graded_per_horizon", "effective_graded_sessions_per_horizon",
                 "full_20_session_graded_maturity", "distinct_regimes", "unknown_provenance_fraction"):
        assert name in crit and "ok" in crit[name] and "threshold" in crit[name]
    # zero input rows → provenance quality UNAVAILABLE (a failing gate), never a false 0% unknown
    assert crit["unknown_provenance_fraction"]["ok"] is False
    assert crit["unknown_provenance_fraction"]["actual"] == "UNAVAILABLE"
    assert det["gate_report"]["gate_id"] == policy.GATE_ID


def test_coverage_view_raw_vs_effective_and_safety():
    from atp.research.validation import readmodel
    s = _store()
    cov = readmodel.coverage_view(s)
    assert cov["coverage"]["effective_canonical_sessions"] == 0        # no snapshots yet
    assert "raw_operational_prediction_count" in cov                   # legacy hourly count exposed separately
    assert cov["confidence"]["probability_calibration"] == "NOT APPLICABLE"
    assert cov["safety"] == {"research_only": True, "autonomous": "DISABLED", "execution": "DISABLED",
                             "ibkr_orders": 0}


def _write_snapshot(store, *, symbol, session, universe_id, direction, confidence="60", sampling=None):
    sid = f"snap-{universe_id}-{symbol}-{session}"
    snap = {"snapshot_id": sid, "universe_id": universe_id, "universe_version": "v1",
            "sampling_policy_version": (sampling or policy.SAMPLING_POLICY_VERSION),
            "outcome_policy_version": policy.OUTCOME_POLICY_VERSION, "symbol": symbol,
            "asset_class": "US_EQUITY", "exchange": "NYSE", "currency": "USD", "exchange_tz": "America/New_York",
            "calendar_id": "NYSE", "calendar_version": policy.CALENDAR_VERSION,
            "scheduled_target_ts": f"{session}T20:10:00+00:00", "computation_started_ts": f"{session}T20:12:00+00:00",
            "decision_ts": f"{session}T20:12:00+00:00", "decision_session_date": session, "is_early_close": False,
            "decision_price": "100", "decision_price_source": "market_data",
            "decision_price_provenance_status": "OBSERVED_ONLY", "decision_price_bar_ts": None,
            "consensus_score": "70", "consensus_direction": direction, "consensus_confidence": confidence,
            "consensus_status": ("NO DATA" if direction is None else "COMPLETE"),
            "governance_status": "PARTIAL", "governance_reasons_json": "[]", "data_completeness": "80",
            "expected_outcome_contract_json": "{}", "adjustment_policy": policy.ADJUSTMENT_POLICY,
            "horizons_json": "[1,3,5,20]", "inputs_checksum": "ic", "snapshot_checksum": "sc-" + sid,
            "commit_sha": SHA, "status": "COLLECTED"}
    inputs = ([] if direction is None else
              [{"component_name": "Market Data", "provenance_status": "OBSERVED_ONLY"}])
    store.ri_write_snapshot(snapshot=snap, inputs=inputs, event={"event_type": "SNAPSHOT_WRITTEN",
                                                                 "snapshot_id": sid, "commit_sha": SHA})
    return sid


def _write_outcome(store, *, snapshot_id, horizon, expected, actual):
    correct = None if expected is None else expected == actual
    classification = "ABSTAIN" if expected is None else ("CORRECT" if correct else "INCORRECT")
    store.ri_write_outcome({"snapshot_id": snapshot_id, "horizon_sessions": horizon, "snapshot_checksum": "sc",
                            "outcome_policy_version": policy.OUTCOME_POLICY_VERSION, "status": "MATURED",
                            "direction_expected": expected, "direction_actual": actual,
                            "direction_correct": correct, "classification": classification, "commit_sha": SHA,
                            "outcome_checksum": f"sha256:oc-{snapshot_id}-{horizon}"})


def test_no_data_abstain_snapshots_never_satisfy_the_gate():
    # § correction 6: 252 sessions of ONLY NO-DATA/ABSTAIN → many matured outcomes but ZERO graded → the gate
    # can never pass (graded_per_horizon = 0), and provenance quality is UNAVAILABLE (not a false 0%).
    from atp.research.intel import policy as p
    s = _store()
    sessions = []
    d = date(2023, 1, 3)
    while len(sessions) < 252:
        if cal.is_session_day(d):
            sessions.append(d.isoformat())
        d += timedelta(days=1)
    for sess in sessions:
        sid = _write_snapshot(s, symbol="NVDA", session=sess, universe_id=p.UNIVERSE_ID, direction=None)
        for h in p.HORIZONS:
            _write_outcome(s, snapshot_id=sid, horizon=h, expected=None, actual="BULLISH")
    r = run_validation(s, commit_sha=SHA, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert r["status"] == "INSUFFICIENT"
    run = s.rv_get_run(r["run_id"])
    from atp.research.validation import readmodel
    crit = readmodel.run_detail(s, run)["gate_report"]["criteria"]
    assert crit["unique_sessions"]["ok"] is True                        # 252 sessions present
    assert crit["graded_per_horizon"]["ok"] is False                    # but ZERO graded → gate fails
    assert all(v == 0 for v in crit["graded_per_horizon"]["actual"].values())


def test_universe_isolation_excludes_other_universe():
    from atp.research.intel import policy as p
    s = _store()
    pilot = _write_snapshot(s, symbol="NVDA", session="2026-08-14", universe_id=p.UNIVERSE_ID, direction="BULLISH")
    other = _write_snapshot(s, symbol="NVDA", session="2026-08-14", universe_id="FUTURE_EU_UNIVERSE_V1",
                            direction="BULLISH", sampling="EU_EQUITY_PILOT_V1")
    _write_outcome(s, snapshot_id=pilot, horizon=1, expected="BULLISH", actual="BULLISH")
    _write_outcome(s, snapshot_id=other, horizon=1, expected="BULLISH", actual="BEARISH")
    r = run_validation(s, commit_sha=SHA, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    from atp.research.validation import readmodel
    cov = readmodel.run_detail(s, s.rv_get_run(r["run_id"]))["metrics"]["coverage"]
    assert cov["matured_total"] == 1                                    # ONLY the pilot outcome counts
    assert cov["by_horizon"]["1"]["graded"] == 1


def test_outcome_checksum_is_sensitive_to_decisive_fields():
    from atp.research.intel.outcomes import _outcome_checksum
    base = {"snapshot_id": "s", "snapshot_checksum": "sc", "horizon_sessions": 5, "dataset_id": "d",
            "dataset_checksum": "dc", "provider_contract_version": "pc", "adjustment_policy": "ap",
            "decision_bar_ts": "t1", "decision_price": "100.00", "outcome_bar_ts": "t2", "outcome_price": "110.00",
            "return_pct": "10", "direction_expected": "BULLISH", "direction_actual": "BULLISH",
            "classification": "CORRECT", "neutral_threshold_pct": "1.0", "outcome_policy_version": "op",
            "commit_sha": "a" * 40}
    ref = _outcome_checksum(base)
    for field, newval in [("dataset_checksum", "dc2"), ("outcome_price", "999.00"),
                          ("direction_actual", "BEARISH"), ("outcome_policy_version", "op2")]:
        assert _outcome_checksum({**base, field: newval}) != ref, f"{field} must change the checksum"
    assert _outcome_checksum(dict(base)) == ref                        # deterministic


def test_validation_worker_exit_codes(monkeypatch, tmp_path):
    from atp.research.validation import worker as w
    dbfile = str(tmp_path / "a.db")
    open_store(dbfile)
    monkeypatch.setenv("ATP_STORE_URL", "sqlite:///" + dbfile)
    assert w.main(["run"], _commit_sha=SHA, _now=datetime(2026, 10, 20, tzinfo=timezone.utc)) == 0
    with pytest.raises(SystemExit):
        w.main(["--session", "2020-01-01"], _commit_sha=SHA)     # no backdating flag on the CLI
    monkeypatch.delenv("ATP_COMMIT_REF", raising=False)
    assert w.main(["run"]) == 1                                  # commit fail-closed → non-zero
