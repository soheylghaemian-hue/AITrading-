"""§ R3.1A — deterministic validation run over a FROZEN matured-outcome set (RESEARCH ONLY).

Freezes the immutable snapshots + matured outcomes, evaluates the preregistered evidence gate
(`VALIDATION_GATE_US_EQUITY_PILOT_V1`), and — ONLY if the gate passes — computes prediction-quality metrics
+ naive benchmarks. If the gate is not met the run is honestly `INSUFFICIENT` (no fabricated metrics). Every
run records the deployed commit SHA, snapshot/outcome set checksums and a deterministic result checksum, and
is DB-immutable once terminal. Regime is derived from the immutable dataset (never live data). No trading,
no optimization, no parameter search.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ...store.base import new_id
from .. import calendars as cal
from ..intel import policy
from ..intel.envelope import canonical_json
from . import benchmarks as bm
from . import calibration as calib
from . import metrics as mx


def _sha(obj) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _nth_session_before(d: date, n: int) -> date:
    cur, count = d, 0
    while count < n:
        cur = cur - timedelta(days=1)
        if cal.is_session_day(cur):
            count += 1
    return cur


def _regime_for(store, outcome, snap, cache: dict) -> str:
    key = (outcome.dataset_id, snap.symbol, snap.decision_session_date)
    if key in cache:
        return cache[key]
    regime = "UNKNOWN"
    try:
        if outcome.dataset_id:
            dd = date.fromisoformat(snap.decision_session_date)
            prior = _nth_session_before(dd, policy.REGIME_LOOKBACK)
            dec_ts = f"{dd.isoformat()}T00:00:00+00:00"
            prior_ts = f"{prior.isoformat()}T00:00:00+00:00"
            bars = {b.ts: b for b in store.rd_list_bars_range(outcome.dataset_id, snap.symbol,
                                                              policy.DATASET_INTERVAL, prior_ts, dec_ts)}
            db, pb = bars.get(dec_ts), bars.get(prior_ts)
            if db and pb and Decimal(str(pb.close)) != 0:
                r = (Decimal(str(db.close)) - Decimal(str(pb.close))) / Decimal(str(pb.close)) * Decimal(100)
                regime = policy.classify_regime(r)
    except Exception:  # noqa: BLE001 — regime is best-effort; never fabricated
        regime = "UNKNOWN"
    cache[key] = regime
    return regime


def _provenance_quality(store, snaps) -> dict:
    total = unknown = missing = 0
    for s in snaps:
        for inp in store.ri_list_inputs(s.snapshot_id):
            total += 1
            if inp.provenance_status == "UNKNOWN":
                unknown += 1
            if inp.missing_data_reason:
                missing += 1
    if total == 0:                     # § correction 6: zero input rows → quality UNAVAILABLE (not a false 0%)
        return {"total_inputs": 0, "available": False, "unknown_fraction": None, "missing_fraction": None}
    return {"total_inputs": total, "available": True,
            "unknown_fraction": round(unknown / total, 4), "missing_fraction": round(missing / total, 4)}


def _evaluate_gate(store, snaps, outcomes, samples, cov) -> dict:
    g = policy.GATE
    sessions = sorted({s.decision_session_date for s in snaps})
    months = 0.0
    if len(sessions) >= 2:
        months = (date.fromisoformat(sessions[-1]) - date.fromisoformat(sessions[0])).days / 30.44
    regimes = {s.get("regime") for s in samples if s.get("regime") and s.get("regime") != "UNKNOWN"}
    prov = _provenance_quality(store, snaps)
    per_h = cov["by_horizon"]

    def crit(name, ok, actual, threshold):
        return {name: {"ok": bool(ok), "actual": actual, "threshold": threshold}}

    criteria = {}
    criteria |= crit("unique_sessions", cov["effective_canonical_sessions"] >= g["min_unique_sessions"],
                     cov["effective_canonical_sessions"], g["min_unique_sessions"])
    criteria |= crit("unique_symbols", cov["unique_symbols"] >= g["min_symbols"],
                     cov["unique_symbols"], g["min_symbols"])
    # § correction 6: the gate requires GRADED prediction outcomes (NO-DATA/ABSTAIN never satisfy it).
    graded_ok = all(per_h[str(h)]["graded"] >= g["min_matured_outcomes_per_horizon"] for h in policy.HORIZONS)
    criteria |= crit("graded_per_horizon", graded_ok,
                     {str(h): per_h[str(h)]["graded"] for h in policy.HORIZONS},
                     g["min_matured_outcomes_per_horizon"])
    eff_ok = all(per_h[str(h)]["effective_graded_sessions"] >= g["min_effective_samples_per_horizon"]
                 for h in policy.HORIZONS)
    criteria |= crit("effective_graded_sessions_per_horizon", eff_ok,
                     {str(h): per_h[str(h)]["effective_graded_sessions"] for h in policy.HORIZONS},
                     g["min_effective_samples_per_horizon"])
    criteria |= crit("wall_clock_months", months >= g["min_wall_clock_months"], round(months, 2),
                     g["min_wall_clock_months"])
    criteria |= crit("full_20_session_graded_maturity",
                     per_h["20"]["graded"] >= g["min_matured_outcomes_per_horizon"],
                     per_h["20"]["graded"], g["min_matured_outcomes_per_horizon"])
    criteria |= crit("distinct_regimes", len(regimes) >= g["min_distinct_regimes"], sorted(regimes),
                     g["min_distinct_regimes"])
    prov_ok = (prov["available"] and prov["unknown_fraction"] <= g["max_unknown_provenance_fraction"])
    criteria |= crit("unknown_provenance_fraction", prov_ok,
                     ("UNAVAILABLE" if not prov["available"] else prov["unknown_fraction"]),
                     g["max_unknown_provenance_fraction"])
    miss_ok = (prov["available"] and prov["missing_fraction"] <= g["max_missing_data_fraction"])
    criteria |= crit("missing_data_fraction", miss_ok,
                     ("UNAVAILABLE" if not prov["available"] else prov["missing_fraction"]),
                     g["max_missing_data_fraction"])
    passed = all(c["ok"] for c in criteria.values())
    return {"gate_id": policy.GATE_ID, "passed": passed, "criteria": criteria,
            "regime_policy_version": policy.REGIME_POLICY_VERSION, "provenance_quality": prov}


def run_validation(store, *, commit_sha: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    snaps = store.ri_list_snapshots(universe_id=policy.UNIVERSE_ID)
    # § correction 7 — universe isolation: only outcomes whose snapshot belongs to THIS universe/policy
    # influence coverage / gate / checksums / metrics / benchmarks. Other (future) universes are excluded.
    universe_snapshot_ids = {s.snapshot_id for s in snaps}
    outcomes = [o for o in store.ri_list_outcomes() if o.snapshot_id in universe_snapshot_ids]
    matured = [o for o in outcomes if o.status == "MATURED"]
    snap_by_id = {s.snapshot_id: s for s in snaps}

    cache: dict = {}
    samples: list[dict] = []
    dataset_ids: set = set()
    for o in matured:
        s = snap_by_id.get(o.snapshot_id)
        if not s:
            continue
        if o.dataset_id:
            dataset_ids.add(o.dataset_id)
        conf = None
        if s.consensus_confidence not in (None, ""):
            try:
                conf = float(s.consensus_confidence)
            except (ValueError, TypeError):
                conf = None
        samples.append({"symbol": s.symbol, "session_date": s.decision_session_date,
                        "horizon": o.horizon_sessions, "expected": o.direction_expected,
                        "actual": o.direction_actual, "correct": o.direction_correct, "confidence": conf,
                        "regime": _regime_for(store, o, s, cache),
                        "return_pct": (float(o.return_pct) if o.return_pct else None)})

    cov = mx.coverage(snaps, outcomes)
    gate = _evaluate_gate(store, snaps, outcomes, samples, cov)
    snap_ck = _sha(sorted(s.snapshot_checksum for s in snaps))
    # § correction 8 — bind the COMPLETE canonical outcome records (each `outcome_checksum` already binds
    # snapshot/dataset ids+checksums, prices, directions, classification, threshold, policy, commit) plus the
    # selected dataset ids/checksums and policy versions. Changing any decisive field flips this checksum.
    out_ck = _sha({
        "outcomes": sorted(o.outcome_checksum or (o.snapshot_id + ":" + str(o.horizon_sessions)) for o in matured),
        "datasets": sorted({(o.dataset_id, o.dataset_checksum) for o in matured if o.dataset_id}),
        "outcome_policy_version": policy.OUTCOME_POLICY_VERSION,
        "sampling_policy_version": policy.SAMPLING_POLICY_VERSION,
        "validation_policy_version": policy.VALIDATION_POLICY_VERSION})

    run_id = new_id()
    store.rv_create_run(run_id=run_id, universe_id=policy.UNIVERSE_ID, universe_version=policy.UNIVERSE_VERSION,
                        validation_policy_version=policy.VALIDATION_POLICY_VERSION,
                        outcome_policy_version=policy.OUTCOME_POLICY_VERSION,
                        sampling_policy_version=policy.SAMPLING_POLICY_VERSION, gate_id=policy.GATE_ID,
                        commit_sha=commit_sha)

    if gate["passed"]:
        result = {"coverage": cov, "gate": gate, "metrics": mx.compute(samples),
                  "benchmarks": bm.compute(samples), "calibration": calib.probabilistic_calibration()}
        status = "COMPLETED"
    else:
        result = {"coverage": cov, "gate": gate, "calibration": calib.probabilistic_calibration(),
                  "note": "INSUFFICIENT — evidence gate not met; prediction-quality metrics withheld "
                          "(no fabricated result)"}
        status = "INSUFFICIENT"

    result_ck = _sha({"snapshots": snap_ck, "outcomes": out_ck, "result": result})
    metric_rows = [{"metric_group": k, "metrics_json": canonical_json(v)} for k, v in result.items()]
    store.rv_finalize_run(run_id, expected_from="RUNNING", status=status, snapshot_set_checksum=snap_ck,
                          outcome_set_checksum=out_ck, dataset_ids_json=canonical_json(sorted(dataset_ids)),
                          result_checksum=result_ck, gate_report_json=canonical_json(gate),
                          metrics=metric_rows)
    return {"run_id": run_id, "status": status, "result_checksum": result_ck, "gate_passed": gate["passed"],
            "coverage": cov}
