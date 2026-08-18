"""THINK — pure, deterministic belief formation for the research-only Trader Brain.

THINK converts admitted SENSE evidence into *competing* beliefs.  It never selects a winner: every
hypothesis the caller supplies comes back with its own bounded confidence, its own supporting and
counter-evidence ids and its own falsifiable scenario.  No order, allocation, sizing, leverage or
execution authority is represented, and nothing here may be promoted to trading authority.

The boundary is untrusted.  A ``SenseResult`` is a plain dataclass, so a caller can hand-build one
holding future, stale, duplicated or mutually inconsistent evidence, and a frozen dataclass can
still be tampered with through ``object.__setattr__``.  THINK therefore never trusts the declared
type: it revalidates every field, re-proves each evidence item's own invariants, and re-runs
canonical SENSE admission over the union of the represented usable and rejected evidence using the
result's own ``as_of`` and ``freshness_limit``.  The supplied partitions, reasons, ordering and
contradiction groups must match that reconstruction exactly.  Anything else returns one
deterministic non-admitted result carrying ``INVALID_SENSE_RESULT`` with no beliefs and no
scenarios — never a partial read of a failed boundary.

Confidence is a documented, versioned heuristic, not an empirically calibrated probability::

    score = (prior + weighted_support) / (1 + weighted_support + weighted_counter)

Each admitted item contributes a fixed weight by evidence quality (``VERIFIED`` 1.0,
``OBSERVED_ONLY`` 0.6, ``UNKNOWN`` 0.3).  The form is bounded in ``[0, 1]`` and returns the prior
when no admitted evidence bears on the claim, so it can never be read as a frequency or an edge.
The weights and the calibration label are literals inside the evaluator: a module attribute is
rebindable by any importer, which would silently change scores and checksums for identical inputs.

Purity: no clock, no I/O, no provider, no side effect.  Every instant is normalised to UTC before
any comparison, so a DST fold cannot make an absolutely later observation look knowable and two
timezone spellings of one instant yield identical ordering, scores and checksums.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .contracts import (Assertion, Belief, Evidence, EvidenceQuality, Hypothesis, Scenario, Stance,
                        require_conditions, require_id_tuple, require_identifier,
                        require_unit_score, to_utc)
from .sense import ContradictionGroup, RejectedEvidence, SenseFailure, SenseResult, evaluate_sense


class ThinkFailure(str, Enum):
    """One reason for every boundary violation: describing *how* a forged result was built would
    invite callers to branch on it, and a partial diagnosis of untrusted input is itself a leak."""

    INVALID_SENSE_RESULT = "INVALID_SENSE_RESULT"


class _BoundaryError(Exception):
    """Internal only.  Raised wherever the SENSE boundary fails to re-prove itself."""


@dataclass(frozen=True, slots=True)
class Judgement:
    """One hypothesis, its bounded belief and its falsifiable scenario, kept together so a
    competing pair can never be collapsed into a single winner."""

    hypothesis_id: str
    claim_key: str
    stance: Stance
    belief: Belief
    scenario: Scenario

    def __post_init__(self) -> None:
        require_identifier("hypothesis_id", self.hypothesis_id)
        require_identifier("claim_key", self.claim_key)
        if not isinstance(self.stance, Stance):
            raise ValueError("stance must be an explicit Stance")
        if not isinstance(self.belief, Belief) or not isinstance(self.scenario, Scenario):
            raise ValueError("a judgement holds exactly one Belief and one Scenario")


@dataclass(frozen=True, slots=True)
class ThinkResult:
    """Admitted judgements, or a fail-closed refusal.  The two states are mutually exclusive."""

    admitted: bool
    judgements: tuple[Judgement, ...]
    reasons: tuple[ThinkFailure, ...]
    calibration: str

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise ValueError("admitted must be a bool")
        if not isinstance(self.judgements, tuple):
            raise ValueError("judgements must be a tuple")
        if any(not isinstance(item, Judgement) for item in self.judgements):
            raise ValueError("judgements must hold Judgement items only")
        if not isinstance(self.reasons, tuple):
            raise ValueError("reasons must be a tuple")
        if any(not isinstance(item, ThinkFailure) for item in self.reasons):
            raise ValueError("reasons must hold ThinkFailure items only")
        require_identifier("calibration", self.calibration)
        if self.admitted != (not self.reasons):
            raise ValueError("a result is admitted exactly when it carries no reason")
        if not self.admitted and self.judgements:
            raise ValueError("a non-admitted result cannot carry judgements")

    @property
    def beliefs(self) -> tuple[Belief, ...]:
        return tuple(judgement.belief for judgement in self.judgements)

    @property
    def scenarios(self) -> tuple[Scenario, ...]:
        return tuple(judgement.scenario for judgement in self.judgements)

    def checksum(self) -> str:
        payload = {
            "admitted": self.admitted,
            "calibration": self.calibration,
            "reasons": [reason.value for reason in self.reasons],
            "judgements": [_judgement_payload(j) for j in self.judgements],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _condition_payload(condition) -> dict:
    return {"condition_id": condition.condition_id, "claim_key": condition.claim_key,
            "trigger_stance": condition.trigger_stance.value}


def _judgement_payload(judgement: Judgement) -> dict:
    """Canonical form: UTC instants and sorted collections, so equivalent inputs hash alike."""
    belief, scenario = judgement.belief, judgement.scenario
    return {
        "hypothesis_id": judgement.hypothesis_id,
        "claim_key": judgement.claim_key,
        "stance": judgement.stance.value,
        "belief": {
            "claim": belief.claim,
            "score": belief.score,
            "evidence_ids": sorted(belief.evidence_ids),
            "counter_evidence_ids": sorted(belief.counter_evidence_ids),
            "valid_until": to_utc("valid_until", belief.valid_until).isoformat(),
        },
        "scenario": {
            "scenario_id": scenario.scenario_id,
            "thesis": scenario.thesis,
            "plausibility_score": scenario.plausibility_score,
            "invalidation_conditions": sorted(
                (_condition_payload(c) for c in scenario.invalidation_conditions),
                key=lambda item: (item["condition_id"], item["claim_key"],
                                  item["trigger_stance"])),
        },
    }


def _require_tuple(field: str, value: object) -> tuple:
    if not isinstance(value, tuple):
        raise _BoundaryError(f"{field} must be a tuple")
    return value


def _validate_evidence(item: object) -> None:
    """Re-prove one item's constructor invariants; a correct type proves nothing about content."""
    if not isinstance(item, Evidence):
        raise _BoundaryError("usable and rejected entries must hold Evidence")
    require_identifier("evidence_id", item.evidence_id)
    require_identifier("source", item.source)
    require_identifier("checksum", item.checksum)
    if not isinstance(item.quality, EvidenceQuality):
        raise _BoundaryError("quality must be an explicit EvidenceQuality")
    event = to_utc("event_time", item.event_time)
    available = to_utc("available_time", item.available_time)
    observed = to_utc("observed_time", item.observed_time)
    if available < event or observed < available:
        raise _BoundaryError("evidence timestamps are out of order")
    _require_tuple("assertions", item.assertions)
    keys = []
    for assertion in item.assertions:
        if not isinstance(assertion, Assertion):
            raise _BoundaryError("assertions must hold Assertion items only")
        require_identifier("claim_key", assertion.claim_key)
        if not isinstance(assertion.stance, Stance):
            raise _BoundaryError("stance must be an explicit Stance")
        keys.append(assertion.claim_key)
    if len(set(keys)) != len(keys):
        raise _BoundaryError("one evidence item cannot assert the same claim key twice")


def _validate_group(group: object) -> None:
    if not isinstance(group, ContradictionGroup):
        raise _BoundaryError("contradictions must hold ContradictionGroup items only")
    require_identifier("claim_key", group.claim_key)
    require_id_tuple("supporting_evidence_ids", group.supporting_evidence_ids)
    require_id_tuple("refuting_evidence_ids", group.refuting_evidence_ids)


def _canonical_admission(sense_result: object) -> tuple[tuple[Evidence, ...], datetime, timedelta]:
    """Rebuild SENSE admission from the represented evidence and require an exact match.

    Reconstruction uses the result's own ``as_of`` and ``freshness_limit``, so a forged partition,
    a fabricated rejection reason, a duplicate id within or across partitions, a re-ordered set,
    or a missing or extraneous contradiction group cannot survive.
    """
    if not isinstance(sense_result, SenseResult):
        raise _BoundaryError("a SenseResult is required")
    try:
        as_of = to_utc("as_of", sense_result.as_of)
        limit = sense_result.freshness_limit
        if not isinstance(limit, timedelta) or limit < timedelta(0):
            raise _BoundaryError("freshness_limit must be a non-negative timedelta")
        usable = _require_tuple("usable", sense_result.usable)
        rejected = _require_tuple("rejected", sense_result.rejected)
        for item in usable:
            _validate_evidence(item)
        for entry in rejected:
            if not isinstance(entry, RejectedEvidence):
                raise _BoundaryError("rejected must hold RejectedEvidence items only")
            if not isinstance(entry.reason, SenseFailure):
                raise _BoundaryError("a rejection reason must be an explicit SenseFailure")
            _validate_evidence(entry.evidence)
        for group in _require_tuple("contradictions", sense_result.contradictions):
            _validate_group(group)
        canonical = evaluate_sense(usable + tuple(entry.evidence for entry in rejected),
                                   as_of=as_of, freshness_limit=limit)
        if canonical.checksum() != sense_result.checksum():
            raise _BoundaryError("SenseResult does not match canonical SENSE admission")
    except _BoundaryError:
        raise
    except Exception as exc:  # a malformed nested object fails closed, it never leaks upward
        raise _BoundaryError(f"malformed SenseResult: {exc}") from exc
    return canonical.usable, as_of, limit


def _require_hypotheses(hypotheses: Iterable[Hypothesis]) -> tuple[Hypothesis, ...]:
    """Hypotheses are the caller's own explicit argument, so a malformed one raises rather than
    silently shrinking the competing set.  Ordering is by id: no ranking is implied."""
    items = tuple(hypotheses)
    for hypothesis in items:
        if not isinstance(hypothesis, Hypothesis):
            raise ValueError("evaluate_think accepts Hypothesis items only")
        require_identifier("hypothesis_id", hypothesis.hypothesis_id)
        require_identifier("claim_key", hypothesis.claim_key)
        require_identifier("thesis", hypothesis.thesis)
        if not isinstance(hypothesis.stance, Stance):
            raise ValueError("stance must be an explicit Stance")
        require_unit_score("prior", hypothesis.prior)
        require_conditions("invalidation_conditions", hypothesis.invalidation_conditions)
    require_id_tuple("hypothesis_id", tuple(item.hypothesis_id for item in items))
    return tuple(sorted(items, key=lambda hypothesis: hypothesis.hypothesis_id))


def _weight(items: list, quality_weight: dict) -> float:
    total = 0.0
    for item in items:
        total += quality_weight[item.quality]
    return total


def _horizon(base: datetime, limit: timedelta) -> datetime:
    """Saturate instead of overflowing on a valid but enormous freshness limit.

    ``evaluate_sense`` admits any non-negative ``timedelta``, so a legitimate result can carry a
    limit whose sum with an admitted observation exceeds ``datetime.max``.  Raising there would
    make valid SENSE output unconsumable, so the horizon clamps to the largest representable UTC
    instant instead — still monotonic, still evidence-derived, never earlier than ``base``.  The
    bound is a literal for the same reason the calibration constants are: a module attribute is
    rebindable by any importer.
    """
    try:
        return base + limit
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _judge(hypothesis: Hypothesis, usable: tuple[Evidence, ...], as_of: datetime,
           limit: timedelta, quality_weight: dict) -> Judgement:
    """Score one hypothesis against admitted evidence only.

    Iteration follows canonical SENSE order, so the float additions happen in the same sequence
    for every caller permutation and the score is bit-identical.
    """
    supporting: list[Evidence] = []
    countering: list[Evidence] = []
    for item in usable:
        for assertion in item.assertions:
            if assertion.claim_key != hypothesis.claim_key:
                continue
            if assertion.stance is hypothesis.stance:
                supporting.append(item)
            else:
                countering.append(item)
    support = _weight(supporting, quality_weight)
    counter = _weight(countering, quality_weight)
    score = min(1.0, max(0.0, round((hypothesis.prior + support) / (1.0 + support + counter), 12)))
    contributors = supporting + countering
    # Validity comes only from admitted evidence and the caller's explicit SENSE horizon: the
    # earliest contributing observation expires first, so the belief expires with it.
    valid_until = as_of if not contributors else _horizon(
        min(to_utc("observed_time", item.observed_time) for item in contributors), limit)
    belief = Belief(hypothesis.claim_key, score,
                    tuple(sorted(item.evidence_id for item in supporting)),
                    tuple(sorted(item.evidence_id for item in countering)), valid_until)
    scenario = Scenario(hypothesis.hypothesis_id, hypothesis.thesis, score,
                        hypothesis.invalidation_conditions)
    return Judgement(hypothesis.hypothesis_id, hypothesis.claim_key, hypothesis.stance, belief,
                     scenario)


def evaluate_think(hypotheses: Iterable[Hypothesis], sense_result: SenseResult) -> ThinkResult:
    """Return one bounded belief and one falsifiable scenario per hypothesis, or fail closed.

    Pure: no clock, no I/O, no provider, no side effect.  A malformed or inconsistent
    ``sense_result`` yields the single deterministic ``INVALID_SENSE_RESULT`` refusal; a malformed
    hypothesis raises, because dropping one would quietly narrow the competing set.
    """
    # Calibration is defined here as literals on purpose.  A module-level weight table or version
    # string can be rebound by any importer, which would change scores, labels and checksums for
    # identical explicit inputs; nothing below reads replaceable module state.
    calibration = "THINK_HEURISTIC_V1"
    quality_weight = {EvidenceQuality.VERIFIED: 1.0,
                      EvidenceQuality.OBSERVED_ONLY: 0.6,
                      EvidenceQuality.UNKNOWN: 0.3}
    ordered = _require_hypotheses(hypotheses)
    try:
        usable, as_of, limit = _canonical_admission(sense_result)
    except _BoundaryError:
        return ThinkResult(False, (), (ThinkFailure.INVALID_SENSE_RESULT,), calibration)
    judgements = tuple(_judge(hypothesis, usable, as_of, limit, quality_weight)
                       for hypothesis in ordered)
    return ThinkResult(True, judgements, (), calibration)
