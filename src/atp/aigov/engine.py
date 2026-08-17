"""AI Decision Governance Engine (§ Phase G3.3) — deterministic. READ-ONLY.

Takes an AI assessment (the consensus read-model, or an immutable prediction snapshot reconstructed
from history) and returns one governance verdict:

    APPROVED  — satisfies every governance rule (trustworthy, ready).
    PARTIAL   — a score exists but important intelligence sources are missing / below bar.
    CONFLICT  — important sources disagree at high confidence.
    BLOCKED   — should not proceed (insufficient data, very low confidence, or a risk failure).

PURE functions of the assessment dict — no store writes, no network, no randomness. It does NOT
execute trades, generate orders, or touch Trading Core / Risk Engine / Broker / IBKR / Execution.
Nothing is fabricated: no assessment / no score → BLOCKED (INSUFFICIENT_DATA), never a guessed verdict.

Governance state precedence (most severe first): BLOCKED → CONFLICT → APPROVED → PARTIAL.
"""
from __future__ import annotations

import json

# ---- deterministic thresholds (§ G3.3 initial rules) ----
APPROVE_SCORE = 75.0            # AI score must be >= this to approve
APPROVE_CONFIDENCE = 70.0       # confidence must be >= this to approve
APPROVE_COMPLETENESS = 70.0     # data completeness (%) must be >= this to approve
BLOCK_CONFIDENCE = 50.0         # confidence < this → BLOCKED
BLOCK_COMPLETENESS = 40.0       # completeness < this → BLOCKED
CONFLICT_COMPONENT_SCORE = 80.0  # opposing components each above this → critical CONFLICT
RISK_BLOCK_SCORE = 20.0         # a Risk component this low = severe risk condition → BLOCKED

# Absence of any of these core intelligence sources means the view cannot be APPROVED (→ PARTIAL).
CRITICAL_SOURCES = ("Fundamentals", "News", "Options")

VALID_STATES = ("APPROVED", "PARTIAL", "CONFLICT", "BLOCKED")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_dir(d) -> str:
    s = str(d or "").upper()
    return "BULLISH" if s.startswith("BULL") else "BEARISH" if s.startswith("BEAR") else "NEUTRAL"


def _components(assessment: dict) -> list[dict]:
    c = assessment.get("components")
    return c if isinstance(c, list) else []


def _present_sources(assessment: dict) -> set[str]:
    return {c.get("component_name") for c in _components(assessment) if c.get("component_name")}


def _completeness(assessment: dict) -> float:
    """Data completeness (%) = the share of the consensus weighting that actually has data. Computed
    from the present components' weights; falls back to a supplied coverage (0..1) if no weights."""
    comps = _components(assessment)
    weights = [float(c.get("weight")) for c in comps if c.get("weight") is not None]
    if weights:
        return round(min(1.0, sum(weights)) * 100.0, 1)
    cov = assessment.get("coverage")
    return round((float(cov) if cov is not None else 0.0) * 100.0, 1)


def _critical_conflicts(assessment: dict) -> list[str]:
    """Opposing sources that are BOTH high-conviction (component score > 80) — e.g. Fundamentals
    bullish vs Options bearish. Low-conviction disagreement is not a governance CONFLICT."""
    comps = _components(assessment)
    def hi(c, d):
        return _norm_dir(c.get("direction")) == d and (c.get("score") or 0) > CONFLICT_COMPONENT_SCORE
    bull = [c["component_name"] for c in comps if hi(c, "BULLISH")]
    bear = [c["component_name"] for c in comps if hi(c, "BEARISH")]
    return [f"{b} bullish vs {s} bearish" for b in bull for s in bear][:4]


def _risk_blocked(assessment: dict) -> bool:
    """A severe risk condition: the Risk component is halted/killed or scored below the block floor."""
    for c in _components(assessment):
        if c.get("component_name") != "Risk":
            continue
        flags = c.get("risk_flags") or []
        txt = " ".join(str(x) for x in flags).lower()
        sc = c.get("score")
        if "halt" in txt or "kill" in txt:
            return True
        if sc is not None and float(sc) < RISK_BLOCK_SCORE:
            return True
    return False


def evaluate_governance(assessment: dict, risk_status: str | None = None) -> dict:
    """Deterministic governance verdict for an AI assessment. Read-only; never executes anything.

    § R2.0: `risk_status` is the Risk Control Center state (READY/WARNING/BLOCKED/NO DATA) or None.
    Backward-compatible — when OMITTED (None) the behaviour is identical to before. Otherwise:
      * Risk BLOCKED  → forces Governance BLOCKED (RISK_BLOCK), intelligence assessment still visible.
      * Risk NO DATA  → prevents APPROVED / capital readiness (RISK_DATA_MISSING); does NOT force a false
                        BLOCKED — the intelligence verdict (PARTIAL/CONFLICT) stays visible.
      * Risk WARNING  → visible, non-blocking reason (RISK_WARNING).
      * Risk READY    → allows APPROVED (which still never implies execution readiness).
    """
    sym = str(assessment.get("symbol") or "").upper()
    score = assessment.get("score")
    confidence = assessment.get("confidence")
    status_in = assessment.get("status")
    completeness = _completeness(assessment)
    present = _present_sources(assessment)
    missing = [s for s in CRITICAL_SOURCES if s not in present]
    conflicts = _critical_conflicts(assessment)
    risk_blocked = _risk_blocked(assessment) or risk_status == "BLOCKED"

    def result(state: str, reasons: list[str]) -> dict:
        return {
            "symbol": sym, "status": state, "score": score, "confidence": confidence,
            "data_completeness": completeness, "reasons": _dedupe(reasons),
            "approved": state == "APPROVED", "direction": _norm_dir(assessment.get("direction")),
            "missing": list(missing), "conflicts": conflicts, "risk_status": risk_status,
        }

    # ---- HARD BLOCK (can't even judge): no data, a risk failure, or too little data ----
    if score is None or status_in == "NO DATA":
        return result("BLOCKED", ["INSUFFICIENT_DATA"] + (["RISK_BLOCK"] if risk_status == "BLOCKED" else []))
    hard: list[str] = []
    if risk_blocked:
        hard.append("RISK_BLOCK")
    if completeness < BLOCK_COMPLETENESS:
        hard.append("INSUFFICIENT_DATA")
    if hard:
        return result("BLOCKED", hard)

    # ---- CONFLICT: important sources disagree at high conviction. Surfaced ABOVE the low-confidence
    #      soft-block on purpose — the disagreement is usually WHY confidence is low, and naming it
    #      CONFLICT is more honest (and more actionable) than a generic BLOCKED. ----
    if conflicts:
        return result("CONFLICT", ["SOURCE_CONFLICT"])

    # ---- SOFT BLOCK: a weak, non-conflicting signal (very low confidence) ----
    if confidence is None or float(confidence) < BLOCK_CONFIDENCE:
        return result("BLOCKED", ["LOW_CONFIDENCE"])

    # ---- APPROVED: every rule satisfied AND risk permits capital readiness (READY/WARNING/omitted) ----
    risk_ok_for_approval = risk_status not in ("NO DATA", "BLOCKED")
    if (float(score) >= APPROVE_SCORE and float(confidence) >= APPROVE_CONFIDENCE
            and completeness >= APPROVE_COMPLETENESS and not missing and not risk_blocked
            and risk_ok_for_approval):
        return result("APPROVED", ["RISK_WARNING"] if risk_status == "WARNING" else [])

    # ---- PARTIAL: a score exists but it is not approvable (missing sources / below bar / risk) ----
    reasons = [f"MISSING_{s.upper().replace(' ', '_')}" for s in missing]
    if float(score) < APPROVE_SCORE:
        reasons.append("LOW_SCORE")
    if float(confidence) < APPROVE_CONFIDENCE:
        reasons.append("LOW_CONFIDENCE")
    if completeness < APPROVE_COMPLETENESS:
        reasons.append("LOW_COMPLETENESS")
    if risk_status == "NO DATA":
        reasons.append("RISK_DATA_MISSING")
    if risk_status == "WARNING":
        reasons.append("RISK_WARNING")
    return result("PARTIAL", reasons or ["INCOMPLETE"])


def assessment_from_prediction(pred) -> dict:
    """Reconstruct the assessment dict from an immutable ai_predictions row so governance can be
    computed against exactly what the AI saw at prediction time (never today's numbers)."""
    snap: dict = {}
    if getattr(pred, "components_snapshot", None):
        try:
            snap = json.loads(pred.components_snapshot)
        except (ValueError, TypeError):
            snap = {}
    return {
        "symbol": pred.symbol, "score": pred.score, "direction": pred.direction,
        "confidence": pred.confidence, "status": pred.status,
        "components": snap.get("components") or [], "conflicts": snap.get("conflicts") or [],
    }


def governance_for_prediction(pred) -> dict:
    """Governance verdict for one immutable prediction (adds prediction_id + timestamp)."""
    g = evaluate_governance(assessment_from_prediction(pred))
    g["prediction_id"] = pred.id
    g["timestamp"] = pred.timestamp
    return g


def _loads(raw) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def record_governance(store) -> int:
    """Persist a governance verdict for every prediction that doesn't have one yet. Immutable:
    existing verdicts are never rewritten (ON CONFLICT DO NOTHING). Returns the count newly recorded."""
    done = {g.id for g in store.list_governance_results(None, 2000)}
    recorded = 0
    for p in store.list_ai_predictions(None, 3000):
        if p.id in done:
            continue
        g = evaluate_governance(assessment_from_prediction(p))
        store.insert_governance_result(
            id=p.id, prediction_id=p.id, symbol=p.symbol, status=g["status"], score=g["score"],
            confidence=g["confidence"], data_completeness=g["data_completeness"],
            reason_codes=json.dumps(g["reasons"]))
        recorded += 1
    return recorded


def build_governance_feed(store, limit: int = 50) -> dict:
    """Recent governance decisions (newest first), each joined to its prediction direction and its
    5-day outcome (§ G3.2) so the UI can show Prediction → Governance → Outcome. Read-only."""
    n = max(1, min(500, int(limit)))
    items: list[dict] = []
    counts: dict[str, int] = {}
    for gr in store.list_governance_results(None, n):
        pred = store.get_ai_prediction(gr.prediction_id)
        o5 = None
        if pred is not None:
            outs = store.list_ai_prediction_outcomes(gr.prediction_id)
            o5 = next((o for o in outs if o.time_horizon == 5), None)
        counts[gr.status] = counts.get(gr.status, 0) + 1
        items.append({
            "prediction_id": gr.prediction_id, "symbol": gr.symbol, "status": gr.status,
            "score": gr.score, "confidence": gr.confidence, "data_completeness": gr.data_completeness,
            "reasons": _loads(gr.reason_codes), "approved": gr.status == "APPROVED",
            "direction": pred.direction if pred else None,
            "timestamp": pred.timestamp if pred else gr.created_at,
            "outcome": (None if o5 is None else {
                "time_horizon": o5.time_horizon, "return_percentage": o5.return_percentage,
                "direction_correct": o5.direction_correct, "status": o5.status or "EVALUATED"}),
        })
    return {"count": len(items), "decisions": items, "status_counts": counts}
