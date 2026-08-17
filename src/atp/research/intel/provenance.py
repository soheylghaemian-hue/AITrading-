"""§ R3.1A.1 — provenance classification + canonical input envelope, built from the consensus TRACE.

The envelope is constructed solely from the single-computation trace (the exact reads that produced the
score) — never a second query. Provenance is classified against the actual capture timestamp:

  * a source timestamp in the FUTURE relative to capture is invalid and never FRESH;
  * an absent observation timestamp cannot be OBSERVED_ONLY → it is UNKNOWN (with a reason);
  * Risk (and anything with no observation timestamp) is UNKNOWN;
  * a publication/filed time is NOT sufficient to claim VERIFIED (availability must be separately proven,
    which no current pilot source provides → VERIFIED is reserved);
  * missing/invalid provenance always carries an explicit reason;
  * freshness never treats a negative age (observation after capture) as FRESH.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .envelope import canonical_json

FRESHNESS_MAX_AGE_HOURS = 48.0


def _parse(v):
    if v in (None, ""):
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def classify_provenance(event_ts, published_ts, observed_ts, capture_ts) -> dict:
    """Return {source_available_ts, provenance_status, missing_data_reason, freshness_state}. Deterministic;
    never fabricates an availability timestamp; never labels a future observation FRESH."""
    cap, obs, pub = _parse(capture_ts), _parse(observed_ts), _parse(published_ts)
    if obs is None:
        return {"source_available_ts": None, "provenance_status": "UNKNOWN",
                "missing_data_reason": "NO_OBSERVED_TS", "freshness_state": "MISSING"}
    if cap is not None and obs > cap:
        return {"source_available_ts": None, "provenance_status": "UNKNOWN",
                "missing_data_reason": "OBSERVED_TS_IN_FUTURE", "freshness_state": "INVALID_FUTURE"}
    age_h = None if cap is None else (cap - obs).total_seconds() / 3600.0
    if age_h is None:
        freshness = "OBSERVED"
    elif age_h < 0:
        freshness = "INVALID_FUTURE"                     # defensive; caught above
    elif age_h <= FRESHNESS_MAX_AGE_HOURS:
        freshness = "FRESH"
    else:
        freshness = "STALE"
    reason = None
    if pub is not None and cap is not None and pub > cap:
        reason = "PUBLISHED_TS_IN_FUTURE"                # keep the published time but flag it, never FRESH-proof
    # OBSERVED_ONLY: we provably observed/ingested it, but external availability is NOT separately proven.
    return {"source_available_ts": None, "provenance_status": "OBSERVED_ONLY",
            "missing_data_reason": reason, "freshness_state": freshness}


def build_envelope_from_trace(trace: list[dict], capture_ts: str) -> list[dict]:
    """One canonical input row per traced component — exact value that produced the score + classified
    provenance (from the SAME reads). No store access here."""
    inputs = []
    for c in trace:
        prov = classify_provenance(c.get("source_event_ts"), c.get("source_published_or_filed_ts"),
                                   c.get("source_observed_ts"), capture_ts)
        inputs.append({
            "component_name": c["component_name"],
            "canonical_value_json": canonical_json(c.get("canonical_value")),
            "component_score": (None if c.get("component_score") is None else str(c["component_score"])),
            "component_status": c.get("component_status"),
            "source_provider": c.get("source_provider"),
            "source_event_ts": c.get("source_event_ts"),
            "source_published_or_filed_ts": c.get("source_published_or_filed_ts"),
            "source_observed_ts": c.get("source_observed_ts"),
            **prov,
        })
    return inputs
