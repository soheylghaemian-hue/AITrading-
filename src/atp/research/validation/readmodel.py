"""§ R3.1A — read-models for the research AI-validation view (JSON for API/frontend). Read-only; no trading."""
from __future__ import annotations

import json

from ..intel import policy
from ..intel.legacy_diag import reconcile_legacy
from ..intel.readmodel import gate_passed
from . import metrics as mx

_SAFETY = {"research_only": True, "autonomous": "DISABLED", "execution": "DISABLED", "ibkr_orders": 0}


def _loads(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def coverage_view(store) -> dict:
    snaps = store.ri_list_snapshots(universe_id=policy.UNIVERSE_ID)
    outcomes = store.ri_list_outcomes()
    cov = mx.coverage(snaps, outcomes)
    raw_operational = len(store.list_ai_predictions(None, 5000))   # legacy hourly (NOT independent samples)
    return {
        "universe": {"id": policy.UNIVERSE_ID, "version": policy.UNIVERSE_VERSION,
                     "symbols": list(policy.PILOT_SYMBOLS), "asset_class": policy.ASSET_CLASS,
                     "exchange": policy.EXCHANGE, "calendar_version": policy.CALENDAR_VERSION,
                     "currency": policy.CURRENCY},
        "policies": {"sampling": policy.SAMPLING_POLICY_VERSION, "outcome": policy.OUTCOME_POLICY_VERSION,
                     "validation": policy.VALIDATION_POLICY_VERSION, "regime": policy.REGIME_POLICY_VERSION,
                     "gate": policy.GATE_ID},
        "coverage": cov,
        "raw_operational_prediction_count": raw_operational,
        "effective_canonical_sample_note": "one independent sample per symbol per session; hourly legacy "
                                           "predictions are NOT counted",
        "confidence": {"is_probability": False, "note": "heuristic 0-100 score",
                       "probability_calibration": "NOT APPLICABLE"},
        "legacy_reconciliation": reconcile_legacy(store),
        "gate": policy.GATE, "gate_id": policy.GATE_ID,
        "safety": _SAFETY,
    }


def _run_dict(r) -> dict:
    # `gate_passed` is carried on the SUMMARY too (not just the detail) so a consumer can gate a verdict
    # without a second fetch. None = no/malformed gate report = NOT validated (never optimistic).
    return {"run_id": r.run_id, "status": r.status, "gate_passed": gate_passed(r.gate_report_json),
            "universe_id": r.universe_id,
            "validation_policy_version": r.validation_policy_version,
            "outcome_policy_version": r.outcome_policy_version, "gate_id": r.gate_id,
            "snapshot_set_checksum": r.snapshot_set_checksum, "outcome_set_checksum": r.outcome_set_checksum,
            "result_checksum": r.result_checksum, "commit_sha": r.commit_sha,
            "created_at": r.created_at, "ended_at": r.ended_at, "safety": _SAFETY}


def runs_view(rows) -> dict:
    return {"count": len(rows), "runs": [_run_dict(r) for r in rows]}


def run_detail(store, r) -> dict:
    d = _run_dict(r)
    d["gate_report"] = _loads(r.gate_report_json, {})
    d["metrics"] = {group: _loads(mjson, {}) for group, mjson in store.rv_list_metrics(r.run_id)}
    d["dataset_ids"] = _loads(r.dataset_ids_json, [])
    return d
