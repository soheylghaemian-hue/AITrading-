"""§ R3.1A / R3.1A.1 — forward-only immutable intelligence collector (RESEARCH DATA ONLY).

For the eligible just-completed NYSE session (derived from the verified clock inside a NARROW post-target
window), compute each pilot symbol's consensus + governance ONCE via the trace-capable pure interface,
build the snapshot's input envelope SOLELY from that single-computation trace (no second source query),
and write snapshot + inputs + a collection event ATOMICALLY. Timestamps are honest and distinct:
scheduled_target_ts (close+settle), computation_started_ts, decision_ts (= actual capture), created_at
(persist). Idempotent (one canonical snapshot per symbol/session). Never pins a future dataset, never reads
live prices for outcomes, never trades; captures NO-DATA/ABSTAIN honestly. `now`/`commit_sha` are function
parameters (test seam); the CLI exposes NO way to target an arbitrary historical date.
"""
from __future__ import annotations

import hashlib
from datetime import timezone

from ...consensus.engine import build_ai_consensus_traced
from ...aigov.engine import evaluate_governance
from . import policy
from .envelope import canonical_json, inputs_checksum, snapshot_checksum
from .provenance import build_envelope_from_trace


def _snapshot_id(symbol: str, session_date: str) -> str:
    key = f"{symbol.upper()}|{session_date}|{policy.SAMPLING_POLICY_VERSION}"
    return "snap-" + hashlib.sha256(key.encode()).hexdigest()[:28]


def _fnum(v):
    return None if v is None else str(v)


def collect_session(store, *, now, commit_sha: str, symbols=policy.PILOT_SYMBOLS) -> dict:
    """Collect the eligible session's canonical snapshots. `now` is verified UTC; `commit_sha` is a
    pre-verified 40-hex SHA. Outside the narrow post-target window, nothing is written (honest skip)."""
    now = now.astimezone(timezone.utc)
    elig = policy.eligible_session(now)
    if not elig["eligible"]:
        store.ri_add_event({"event_type": "SAMPLE_SKIPPED", "severity": "INFO",
                            "session_date": elig.get("session_date"), "commit_sha": commit_sha,
                            "details": {"reason": elig["reason"]}})
        return {"eligible": False, "reason": elig["reason"], "session_date": elig.get("session_date"),
                "written": [], "skipped": [s.upper() for s in symbols]}

    session_date = elig["session_date"]
    scheduled_target_ts = elig["scheduled_target_ts"]
    is_early = elig["is_early_close"]
    computation_started_ts = now.isoformat()          # actual computation start (honest, not the target)
    written, skipped, existed = [], [], []

    for raw in symbols:
        sym = raw.upper()
        if not (policy.is_supported_symbol(sym) and policy.is_supported_market(
                policy.ASSET_CLASS, policy.EXCHANGE, policy.CALENDAR_VERSION, policy.EXCHANGE_TZ, policy.CURRENCY)):
            store.ri_add_event({"event_type": "UNSUPPORTED_MARKET", "severity": "ERROR", "symbol": sym,
                                "session_date": session_date, "commit_sha": commit_sha,
                                "details": {"symbol": sym}})       # fail closed — no snapshot
            skipped.append(sym)
            continue

        # ONE computation: assessment (== build_ai_consensus), the exact trace, and the decision-price meta.
        assessment, trace, meta = build_ai_consensus_traced(store, sym)
        gov = evaluate_governance(assessment)
        capture_ts = now.isoformat()                               # actual capture/decision time (honest)
        inputs = build_envelope_from_trace(trace, capture_ts)      # SOLELY from the trace — no second query

        core = {
            "symbol": sym, "universe_id": policy.UNIVERSE_ID, "universe_version": policy.UNIVERSE_VERSION,
            "sampling_policy_version": policy.SAMPLING_POLICY_VERSION,
            "outcome_policy_version": policy.OUTCOME_POLICY_VERSION,
            "asset_class": policy.ASSET_CLASS, "exchange": policy.EXCHANGE, "currency": policy.CURRENCY,
            "exchange_tz": policy.EXCHANGE_TZ, "calendar_id": policy.CALENDAR_ID,
            "calendar_version": policy.CALENDAR_VERSION, "scheduled_target_ts": scheduled_target_ts,
            "decision_ts": capture_ts, "decision_session_date": session_date, "is_early_close": bool(is_early),
            "decision_price": meta.get("decision_price"), "decision_price_source": meta.get("decision_price_source"),
            "decision_price_provenance_status": meta.get("decision_price_provenance_status"),
            "decision_price_bar_ts": meta.get("decision_price_bar_ts"),
            "consensus_score": _fnum(assessment.get("score")), "consensus_direction": assessment.get("direction"),
            "consensus_confidence": _fnum(assessment.get("confidence")),
            "consensus_status": assessment.get("status"), "governance_status": gov.get("status"),
            "governance_reasons_json": canonical_json(gov.get("reasons") or []),
            "data_completeness": _fnum(gov.get("data_completeness")),
            "expected_outcome_contract_json": canonical_json(policy.expected_outcome_contract()),
            "adjustment_policy": policy.ADJUSTMENT_POLICY,
            "horizons_json": canonical_json(list(policy.HORIZONS)),
        }
        inputs_ck = inputs_checksum(inputs)
        snap_ck = snapshot_checksum(core, inputs_ck)
        sid = _snapshot_id(sym, session_date)
        snapshot = {**core, "snapshot_id": sid, "computation_started_ts": computation_started_ts,
                    "inputs_checksum": inputs_ck, "snapshot_checksum": snap_ck, "commit_sha": commit_sha,
                    "status": "COLLECTED", "supersedes_snapshot_id": None}
        event = {"event_type": "SNAPSHOT_WRITTEN", "snapshot_id": sid, "symbol": sym,
                 "session_date": session_date, "severity": "INFO", "ts": capture_ts, "commit_sha": commit_sha,
                 "details": {"consensus_status": assessment.get("status"), "governance_status": gov.get("status"),
                             "component_count": len(inputs), "scheduled_target_ts": scheduled_target_ts}}
        if store.ri_write_snapshot(snapshot=snapshot, inputs=inputs, event=event):
            written.append(sym)
        else:
            existed.append(sym)                                    # idempotent no-op (already collected)

    return {"eligible": True, "session_date": session_date, "scheduled_target_ts": scheduled_target_ts,
            "written": written, "already_collected": existed, "skipped": skipped}
