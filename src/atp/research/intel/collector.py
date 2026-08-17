"""§ R3.1A — forward-only immutable intelligence collector (RESEARCH DATA ONLY).

For the eligible just-completed NYSE session (derived from the verified clock, inside a bounded post-close
window), compute each pilot symbol's consensus + governance ONCE using the existing pure execution-free
functions, capture the exact input envelope + provenance, and write the snapshot + inputs + a collection
event ATOMICALLY in one transaction. Idempotent (one canonical snapshot per symbol per session). It never
pins a future dataset, never reads live prices for outcomes, never trades, and captures even NO-DATA/ABSTAIN
states honestly. `now`/`commit_sha` are function parameters (test seam); the production worker supplies the
real clock and verified commit — the CLI exposes NO way to target an arbitrary historical date.
"""
from __future__ import annotations

import hashlib

from ...consensus.engine import build_ai_consensus
from ...aigov.engine import evaluate_governance
from . import policy
from .envelope import canonical_json, inputs_checksum, snapshot_checksum
from .provenance import build_input_envelope


def _snapshot_id(symbol: str, session_date: str) -> str:
    key = f"{symbol.upper()}|{session_date}|{policy.SAMPLING_POLICY_VERSION}"
    return "snap-" + hashlib.sha256(key.encode()).hexdigest()[:28]


def _decision_price(store, symbol: str):
    try:
        bars = store.list_ohlc_bars(symbol.upper(), "1D", 3) or store.list_ohlc_bars(symbol.upper(), "1m", 3)
    except Exception:  # noqa: BLE001
        bars = None
    if bars:
        return str(float(bars[-1].close)), (getattr(bars[-1], "source", None) or "market_data"), "OBSERVED_ONLY"
    return None, None, "UNKNOWN"


def _fnum(v):
    return None if v is None else str(v)


def collect_session(store, *, now, commit_sha: str, symbols=policy.PILOT_SYMBOLS) -> dict:
    """Collect the eligible session's canonical snapshots. `now` is verified UTC; `commit_sha` is a
    pre-verified 40-hex SHA. Outside the bounded post-close window, nothing is written (honest skip)."""
    elig = policy.eligible_session(now)
    if not elig["eligible"]:
        store.ri_add_event({"event_type": "SAMPLE_SKIPPED", "severity": "INFO",
                            "session_date": elig.get("session_date"), "commit_sha": commit_sha,
                            "details": {"reason": elig["reason"]}})
        return {"eligible": False, "reason": elig["reason"], "session_date": elig.get("session_date"),
                "written": [], "skipped": [s.upper() for s in symbols]}

    session_date, decision_ts, is_early = elig["session_date"], elig["decision_ts"], elig["is_early_close"]
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

        assessment = build_ai_consensus(store, sym)                # the EXACT operational computation
        gov = evaluate_governance(assessment)                      # readiness verdict (pure)
        inputs = build_input_envelope(store, sym, assessment, decision_ts)   # exact inputs + provenance
        dprice, dsrc, dprov = _decision_price(store, sym)

        core = {
            "symbol": sym, "universe_id": policy.UNIVERSE_ID, "universe_version": policy.UNIVERSE_VERSION,
            "sampling_policy_version": policy.SAMPLING_POLICY_VERSION,
            "outcome_policy_version": policy.OUTCOME_POLICY_VERSION,
            "asset_class": policy.ASSET_CLASS, "exchange": policy.EXCHANGE, "currency": policy.CURRENCY,
            "exchange_tz": policy.EXCHANGE_TZ, "calendar_id": policy.CALENDAR_ID,
            "calendar_version": policy.CALENDAR_VERSION, "decision_ts": decision_ts,
            "decision_session_date": session_date, "is_early_close": bool(is_early),
            "decision_price": dprice, "decision_price_source": dsrc, "decision_price_provenance_status": dprov,
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
        snapshot = {**core, "snapshot_id": sid, "inputs_checksum": inputs_ck, "snapshot_checksum": snap_ck,
                    "commit_sha": commit_sha, "status": "COLLECTED", "supersedes_snapshot_id": None}
        event = {"event_type": "SNAPSHOT_WRITTEN", "snapshot_id": sid, "symbol": sym,
                 "session_date": session_date, "severity": "INFO", "ts": decision_ts, "commit_sha": commit_sha,
                 "details": {"consensus_status": assessment.get("status"), "governance_status": gov.get("status"),
                             "component_count": len(inputs)}}
        if store.ri_write_snapshot(snapshot=snapshot, inputs=inputs, event=event):
            written.append(sym)
        else:
            existed.append(sym)                                    # idempotent no-op (already collected)

    return {"eligible": True, "session_date": session_date, "decision_ts": decision_ts,
            "written": written, "already_collected": existed, "skipped": skipped}
