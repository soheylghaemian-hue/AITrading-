"""§ Phase R3.1A acceptance — deterministic AI-prediction-quality validation (RESEARCH ONLY).

Proves: confusion / accuracy / precision-recall correctness; slices by horizon/symbol/regime/confidence
bucket; naive benchmarks; heuristic confidence is NEVER treated as a probability (Brier/log-loss/ECE NOT
APPLICABLE); raw vs effective sample counts; the frozen evidence gate yields INSUFFICIENT below threshold;
the validation run is deterministic (stable result checksum) and DB-immutable when terminal; the runner
worker fails closed on an unverifiable commit. Kept strictly separate from trading P&L.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
    for name in ("unique_sessions", "matured_per_horizon", "full_20_session_maturity", "distinct_regimes",
                 "unknown_provenance_fraction"):
        assert name in crit and "ok" in crit[name] and "threshold" in crit[name]
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
