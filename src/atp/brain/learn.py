"""LEARN — pure, research-only drift, champion-challenger evidence and reversible transitions.

LEARN answers three narrow questions and nothing else: has the regime a model was built for drifted,
does complete walk-forward proof prefer the challenger or the champion, and is a retirement
evidence-backed and exactly reversible?  It represents no order, allocation, sizing, execution,
deployment, promotion or risk-relaxation authority.  Every evaluator is pure: no clock, no I/O, no
provider, no persistence, no shared mutable module state and no side effect.

Every boundary object is untrusted.  ``ModelRecord``, ``SenseResult``, ``ProveResult`` and every LEARN
result are public frozen dataclasses, and a frozen dataclass is still mutable through
``object.__setattr__``.  Exact types and complete shapes are therefore proven before any field is
read, and timestamps, freshness, duplicate ids, partitions, contradictions, proof integrity,
proof-to-model binding, roles and point-in-time knowability are all re-proved before evidence can
reach a confidence or a transition.

LEARN uses value integrity, not an in-process claim about who constructed an object.  Every accepted
result rebinds its complete canonical inputs, recomputes every derived output and compares the stored
value exactly before equality, checksumming or downstream consumption.  A canonical copy is
therefore the same research evidence; a malformed shell or any mutation that no longer reconciles is
refused.  This is the strongest honest boundary available to a pure Python value layer: code with
arbitrary reflective execution in the same process can rewrite any Python-held registry or secret,
so no such registry is treated as an authentication mechanism.

Equality and ``checksum()`` are derived from one complete canonical state, so equivalent inputs
produce equal results and identical checksums while every distinct accepted value stays distinct.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from .contracts import Assertion, Evidence, EvidenceQuality, Stance
from .prove import ProveResult
from .sense import ContradictionGroup, RejectedEvidence, SenseFailure, SenseResult, evaluate_sense

# ----------------------------------------------------------------------------- stable vocabulary

class ModelRole(str, Enum):
    """The only roles a research model may hold.  There is no deployed or live role."""

    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"
    RETIRED = "RETIRED"


class ComparisonPreference(str, Enum):
    """Which side the proof favours.  A preference is research evidence and confers no authority."""

    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"
    INCONCLUSIVE = "INCONCLUSIVE"


class RetirementGround(str, Enum):
    """The only admissible reasons a model may be retired."""

    DRIFT_ABSTENTION = "DRIFT_ABSTENTION"
    INFERIOR_COMPARISON = "INFERIOR_COMPARISON"


class DriftFailure(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_MODEL = "INVALID_MODEL"
    INVALID_SENSE_RESULT = "INVALID_SENSE_RESULT"
    UNKNOWABLE_MODEL = "UNKNOWABLE_MODEL"


class ComparisonFailure(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_MODEL = "INVALID_MODEL"
    INVALID_PROOF = "INVALID_PROOF"
    PROOF_NOT_PROVEN = "PROOF_NOT_PROVEN"
    PROOF_MODEL_MISMATCH = "PROOF_MODEL_MISMATCH"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    SELF_COMPARISON = "SELF_COMPARISON"
    UNKNOWABLE_EVIDENCE = "UNKNOWABLE_EVIDENCE"


class TransitionFailure(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_MODEL = "INVALID_MODEL"
    MODEL_ALREADY_RETIRED = "MODEL_ALREADY_RETIRED"
    INVALID_DRIFT = "INVALID_DRIFT"
    INVALID_COMPARISON = "INVALID_COMPARISON"
    EVIDENCE_MODEL_MISMATCH = "EVIDENCE_MODEL_MISMATCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALID_RETIREMENT = "INVALID_RETIREMENT"
    RETIREMENT_NOT_PRIOR = "RETIREMENT_NOT_PRIOR"


__all__ = (
    "ComparisonFailure", "ComparisonInputs", "ComparisonPreference", "ComparisonResult",
    "DriftFailure", "DriftInputs", "DriftResult", "ModelRecord", "ModelRole", "ProofSummary",
    "ReinstatementInputs", "ReinstatementResult", "RetirementGround", "RetirementInputs",
    "RetirementResult", "TransitionFailure", "evaluate_comparison", "evaluate_drift",
    "evaluate_reinstatement", "evaluate_retirement",
)


class _LearnError(ValueError):
    """A refusal carrying the stable reason of the layer that raised it.

    It subclasses ``ValueError`` so direct construction of a malformed public object fails the
    ordinary way, while each evaluator can still map its own reasons onto a deterministic result.
    A reason belonging to another layer's enum is never copied into a caller's refusal.
    """

    def __init__(self, reason, detail: str) -> None:
        super().__init__(f"{getattr(reason, 'value', 'REFUSED')}: {detail}")
        self.reason = reason


# ---------------------------------------------------------------------- exact-type primitives
# Only exact built-in and schema types are accepted.  A subclass can override attribute access,
# comparison or serialization, so it is refused rather than duck-typed into an accepted record.

def _text(reason, field: str, value: object) -> str:
    if type(value) is not str or not value:
        raise _LearnError(reason, f"{field} must be a non-empty str")
    return value


def _flag(reason, field: str, value: object) -> bool:
    if type(value) is not bool:
        raise _LearnError(reason, f"{field} must be a bool")
    return value


def _unit(reason, field: str, value: object) -> float:
    """A bounded score: real, finite and within ``[0, 1]``.  ``NaN`` satisfies no ordering yet
    slips through a naive range check, so it fails closed here."""
    if type(value) is bool or type(value) not in (int, float):
        raise _LearnError(reason, f"{field} must be a real number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _LearnError(reason, f"{field} is not a representable real number") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise _LearnError(reason, f"{field} must be a finite score between 0 and 1")
    return number


def _count(reason, field: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise _LearnError(reason, f"{field} must be a non-negative int")
    return value


def _number(reason, field: str, value: object) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise _LearnError(reason, f"{field} must be a finite float")
    return value


def _instant(reason, field: str, value: object) -> datetime:
    """Return ``value`` as a UTC instant so every comparison is absolute.

    Two aware datetimes sharing one ``tzinfo`` compare by wall time, so during a DST fold an
    absolutely later instant can compare equal or earlier.  Normalising first removes that.
    """
    if type(value) is not datetime:
        raise _LearnError(reason, f"{field} must be a datetime")
    try:
        if value.utcoffset() is None:
            raise _LearnError(reason, f"{field} must be timezone-aware")
        return value.astimezone(UTC)
    except _LearnError:
        raise
    except Exception as exc:
        raise _LearnError(reason, f"{field} must be a valid timezone-aware datetime") from exc


def _items(reason, field: str, value: object, kind: type) -> tuple:
    if type(value) is not tuple:
        raise _LearnError(reason, f"{field} must be a tuple")
    for item in value:
        if type(item) is not kind:
            raise _LearnError(reason, f"{field} must hold exact {kind.__name__} items")
    return value


# ------------------------------------------------------------------- canonical serialization
# One canonical form drives ordering, equality and checksums together, so two spellings of one
# input can never disagree between what is compared and what is signed.

def _dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest()


def _float(reason, field: str, value: object) -> str:
    """Lossless float encoding with equal signed zeros normalised, so ``0.0`` and ``-0.0`` are one
    value in both equality and the checksum while adjacent floats stay distinct."""
    if type(value) is bool or type(value) not in (int, float):
        raise _LearnError(reason, f"{field} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise _LearnError(reason, f"{field} must be finite")
    if number == 0.0:
        number = 0.0
    return number.hex()


def _iso(reason, field: str, value: object) -> str:
    return _instant(reason, field, value).isoformat()


def _delta(reason, field: str, value: object) -> list:
    if type(value) is not timedelta:
        raise _LearnError(reason, f"{field} must be a timedelta")
    return [value.days, value.seconds, value.microseconds]


def _span(reason, field: str, value: object) -> timedelta:
    if type(value) is not timedelta or value < timedelta(0):
        raise _LearnError(reason, f"{field} must be a non-negative timedelta")
    return value


class _Sealed:
    """Equality and the checksum both come from one revalidated canonical state.

    ``__slots__`` is empty so every concrete result stays slotted: an extra attribute cannot be
    attached.  Construction is not authority; coherent reconstruction is intentionally equivalent.
    """

    __slots__ = ()

    def _state(self) -> dict:
        raise NotImplementedError

    def __post_init__(self) -> None:
        self._state()

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return _dumps(self._state()) == _dumps(other._state())

    def checksum(self) -> str:
        """Re-prove the whole record and hash the complete canonical value."""
        return _digest(self._state())


# ------------------------------------------------------------------------------- schema types

@dataclass(frozen=True, slots=True)
class ModelRecord:
    """One research model: its id, the proposal it grades, its role and when it became knowable."""

    model_id: str
    proposal_id: str
    role: ModelRole
    registered_at: datetime

    def __post_init__(self) -> None:
        _validate_model(self, DriftFailure.INVALID_MODEL)


def _validate_model(record: object, reason) -> datetime:
    """Prove the exact type and the complete shape before any field is used.

    An exact but reflectively built shell can have unset slots, so every read is guarded: a missing
    field becomes the caller's own stable reason instead of a leaked ``AttributeError``.
    """
    if type(record) is not ModelRecord:
        raise _LearnError(reason, "an exact ModelRecord is required")
    try:
        model_id = record.model_id
        proposal_id = record.proposal_id
        role = record.role
        registered_at = record.registered_at
    except AttributeError as exc:
        raise _LearnError(reason, "ModelRecord is incompletely formed") from exc
    _text(reason, "model_id", model_id)
    _text(reason, "proposal_id", proposal_id)
    if type(role) is not ModelRole:
        raise _LearnError(reason, "role must be an explicit ModelRole")
    return _instant(reason, "registered_at", registered_at)


def _model_state(record: object, reason) -> dict:
    _validate_model(record, reason)
    return {"model_id": record.model_id, "proposal_id": record.proposal_id,
            "role": record.role.value, "registered_at": _iso(reason, "registered_at",
                                                             record.registered_at)}


@dataclass(frozen=True, slots=True)
class ProofSummary:
    """The canonical, immutable snapshot LEARN keeps of one accepted ``ProveResult``.

    Storing the caller's own object would let two proofs with identical checksums but differently
    spelled inputs produce unequal comparisons, so the snapshot is canonical by construction.
    """

    proposal_id: str
    proposal_identity: str
    input_identity: str
    proof_checksum: str
    as_of: datetime
    expected_count: int
    graded_count: int
    abstention_count: int
    gross_return: float
    costs: float
    net_return: float
    total_delay: timedelta

    def __post_init__(self) -> None:
        _summary_state(self, ComparisonFailure.INVALID_PROOF)


def _summary_state(summary: object, reason) -> dict:
    if type(summary) is not ProofSummary:
        raise _LearnError(reason, "an exact ProofSummary is required")
    try:
        expected = _count(reason, "expected_count", summary.expected_count)
        graded = _count(reason, "graded_count", summary.graded_count)
        abstentions = _count(reason, "abstention_count", summary.abstention_count)
        if graded + abstentions != expected:
            raise _LearnError(reason, "graded and abstained counts must exhaust expected outcomes")
        gross = _number(reason, "gross_return", summary.gross_return)
        costs = _number(reason, "costs", summary.costs)
        net = _number(reason, "net_return", summary.net_return)
        delay = _span(reason, "total_delay", summary.total_delay)
        if costs < 0.0 or net != gross - costs:
            raise _LearnError(reason, "proof summary returns and costs do not reconcile")
        if graded == 0 and (gross != 0.0 or costs != 0.0 or delay != timedelta(0)):
            raise _LearnError(reason, "an ungraded proof summary cannot carry metrics")
        return {
            "proposal_id": _text(reason, "proposal_id", summary.proposal_id),
            "proposal_identity": _text(reason, "proposal_identity", summary.proposal_identity),
            "input_identity": _text(reason, "input_identity", summary.input_identity),
            "proof_checksum": _text(reason, "proof_checksum", summary.proof_checksum),
            "as_of": _iso(reason, "as_of", summary.as_of),
            "expected_count": str(expected),
            "graded_count": str(graded),
            "abstention_count": str(abstentions),
            "gross_return": _float(reason, "gross_return", gross),
            "costs": _float(reason, "costs", costs),
            "net_return": _float(reason, "net_return", net),
            "total_delay": _delta(reason, "total_delay", delay),
        }
    except AttributeError as exc:
        raise _LearnError(reason, "ProofSummary is incompletely formed") from exc


def _proof_checksum(proof: object, reason) -> str:
    """Re-prove one exact proof without deciding whether its outcome was accepted."""
    if type(proof) is not ProveResult:
        raise _LearnError(reason, "an exact ProveResult is required")
    try:
        return proof.checksum()
    except Exception as exc:
        raise _LearnError(reason, "the proof does not re-prove itself") from exc


def _summarise(proof: ProveResult, checksum: str, reason) -> ProofSummary:
    """Reduce one already re-proved, accepted proof to its canonical LEARN facts."""
    try:
        inputs = proof.inputs
        aggregate = proof.aggregate
        summary = ProofSummary(inputs.proposal.proposal_id, proof.proposal_identity,
                               proof.input_identity, checksum, inputs.as_of,
                               aggregate.expected_count, aggregate.graded_count,
                               aggregate.abstention_count, aggregate.gross_return,
                               aggregate.costs, aggregate.net_return, aggregate.total_delay)
    except _LearnError:
        raise
    except Exception as exc:
        raise _LearnError(reason, "the proof carries no complete accepted record") from exc
    return summary


# ------------------------------------------------------------------------- SENSE boundary

def _evidence_state(item: object, reason) -> dict:
    """Re-prove one evidence item and canonicalise it: UTC instants and sorted assertions, so a
    permuted but equivalent item is one value in both equality and the checksum."""
    if type(item) is not Evidence:
        raise _LearnError(reason, "usable and rejected entries must hold exact Evidence")
    _text(reason, "evidence_id", item.evidence_id)
    _text(reason, "source", item.source)
    _text(reason, "checksum", item.checksum)
    if type(item.quality) is not EvidenceQuality:
        raise _LearnError(reason, "quality must be an explicit EvidenceQuality")
    event = _instant(reason, "event_time", item.event_time)
    available = _instant(reason, "available_time", item.available_time)
    observed = _instant(reason, "observed_time", item.observed_time)
    if available < event or observed < available:
        raise _LearnError(reason, "evidence timestamps are out of order")
    assertions = _items(reason, "assertions", item.assertions, Assertion)
    keys = []
    encoded = []
    for assertion in assertions:
        _text(reason, "claim_key", assertion.claim_key)
        if type(assertion.stance) is not Stance:
            raise _LearnError(reason, "stance must be an explicit Stance")
        keys.append(assertion.claim_key)
        encoded.append([assertion.claim_key, assertion.stance.value])
    if len(set(keys)) != len(keys):
        raise _LearnError(reason, "one evidence item cannot assert the same claim key twice")
    return {"evidence_id": item.evidence_id, "source": item.source, "checksum": item.checksum,
            "quality": item.quality.value, "event_time": event.isoformat(),
            "available_time": available.isoformat(), "observed_time": observed.isoformat(),
            "assertions": sorted(encoded)}


def _sense_state(result: object, reason) -> dict:
    groups = []
    for group in result.contradictions:
        _text(reason, "claim_key", group.claim_key)
        supporting = _items(reason, "supporting_evidence_ids",
                            group.supporting_evidence_ids, str)
        refuting = _items(reason, "refuting_evidence_ids", group.refuting_evidence_ids, str)
        groups.append({"claim_key": group.claim_key,
                       "supporting_evidence_ids": sorted(supporting),
                       "refuting_evidence_ids": sorted(refuting)})
    return {
        "as_of": _iso(reason, "as_of", result.as_of),
        "freshness_limit": _delta(reason, "freshness_limit", result.freshness_limit),
        "usable": [_evidence_state(item, reason) for item in result.usable],
        "rejected": [{"evidence": _evidence_state(entry.evidence, reason),
                      "reason": entry.reason.value} for entry in result.rejected],
        "contradictions": groups,
    }


def _require_sense(result: object, as_of: datetime, reason) -> None:
    """Rebuild the represented SENSE admission and require the exact canonical value."""
    if type(result) is not SenseResult:
        raise _LearnError(reason, "an exact SenseResult is required")
    try:
        limit = result.freshness_limit
        if type(limit) is not timedelta or limit < timedelta(0):
            raise _LearnError(reason, "freshness_limit must be a non-negative timedelta")
        sense_as_of = _instant(reason, "as_of", result.as_of)
        usable = _items(reason, "usable", result.usable, Evidence)
        rejected = _items(reason, "rejected", result.rejected, RejectedEvidence)
        for entry in rejected:
            if type(entry.reason) is not SenseFailure:
                raise _LearnError(reason, "a rejection reason must be an explicit SenseFailure")
        _items(reason, "contradictions", result.contradictions, ContradictionGroup)
        represented = _sense_state(result, reason)
        canonical = evaluate_sense(usable + tuple(entry.evidence for entry in rejected),
                                   as_of=sense_as_of, freshness_limit=limit)
        if _sense_state(canonical, reason) != represented:
            raise _LearnError(reason, "the SenseResult does not match canonical SENSE admission")
    except _LearnError:
        raise
    except Exception as exc:
        raise _LearnError(reason, f"malformed SenseResult: {exc}") from exc
    if sense_as_of != as_of:
        raise _LearnError(reason, "the drift as-of must equal the admission as-of")


# ------------------------------------------------------------------------------------- DRIFT

@dataclass(frozen=True, slots=True)
class DriftInputs:
    """Every explicit argument a drift assessment consumed, kept together for regrading."""

    model: ModelRecord
    evidence: SenseResult
    claim_key: str
    prior_confidence: float
    abstention_threshold: float
    as_of: datetime

    def __post_init__(self) -> None:
        _bind_drift(self)


def _bind_drift(inputs: object) -> datetime:
    """Fixed phases: inputs shell, model shape, explicit scalars, SENSE boundary, knowability."""
    if type(inputs) is not DriftInputs:
        raise _LearnError(DriftFailure.INVALID_INPUT, "an exact DriftInputs is required")
    registered = _validate_model(inputs.model, DriftFailure.INVALID_MODEL)
    reason = DriftFailure.INVALID_INPUT
    _text(reason, "claim_key", inputs.claim_key)
    _unit(reason, "prior_confidence", inputs.prior_confidence)
    _unit(reason, "abstention_threshold", inputs.abstention_threshold)
    as_of = _instant(reason, "as_of", inputs.as_of)
    _require_sense(inputs.evidence, as_of, DriftFailure.INVALID_SENSE_RESULT)
    if registered > as_of:
        raise _LearnError(DriftFailure.UNKNOWABLE_MODEL,
                          "a model registered after the as-of instant cannot produce drift")
    return as_of


@dataclass(frozen=True, slots=True, eq=False)
class DriftResult(_Sealed):
    """An accepted drift assessment, or a fail-closed refusal.  The states are exclusive."""

    accepted: bool
    reasons: tuple[DriftFailure, ...]
    inputs: DriftInputs | None
    drift_score: float
    posterior_confidence: float
    abstain: bool
    supporting_evidence_ids: tuple[str, ...]
    refuting_evidence_ids: tuple[str, ...]
    calibration: str

    def _state(self) -> dict:
        reason = DriftFailure.INVALID_INPUT
        accepted = _flag(reason, "accepted", self.accepted)
        reasons = _items(reason, "reasons", self.reasons, DriftFailure)
        # A literal, as in THINK and PROVE: a module attribute is rebindable by any importer, which
        # would move the recorded label and every checksum for identical explicit inputs.
        if _text(reason, "calibration", self.calibration) != "LEARN_DRIFT_V1":
            raise _LearnError(reason, "calibration must be this module's own LEARN calibration")
        if accepted != (not reasons):
            raise _LearnError(reason, "a result is accepted exactly when it carries no reason")
        payload = {"schema": "LEARN_DRIFT_RESULT_V1", "calibration": self.calibration,
                   "accepted": accepted, "reasons": [item.value for item in reasons]}
        if not accepted:
            supporting = _items(reason, "supporting_evidence_ids",
                                self.supporting_evidence_ids, str)
            refuting = _items(reason, "refuting_evidence_ids", self.refuting_evidence_ids, str)
            if len(reasons) != 1:
                raise _LearnError(reason, "a refusal reports exactly one stable reason")
            if (self.inputs is not None or supporting or refuting or self.abstain is not False
                    or _number(reason, "drift_score", self.drift_score) != 0.0
                    or _number(reason, "posterior_confidence",
                               self.posterior_confidence) != 0.0):
                raise _LearnError(reason, "a refusal carries no evidence and no metrics")
            return payload
        _bind_drift(self.inputs)
        expected = _assess(self.inputs)
        supporting = _items(reason, "supporting_evidence_ids", self.supporting_evidence_ids, str)
        refuting = _items(reason, "refuting_evidence_ids", self.refuting_evidence_ids, str)
        if (_number(reason, "drift_score", self.drift_score) != expected[0]
                or _number(reason, "posterior_confidence", self.posterior_confidence) != expected[1]
                or _flag(reason, "abstain", self.abstain) is not expected[2]
                or supporting != expected[3] or refuting != expected[4]):
            raise _LearnError(reason, "the drift result does not reconcile with its bound inputs")
        payload["inputs"] = {
            "model": _model_state(self.inputs.model, DriftFailure.INVALID_MODEL),
            "claim_key": self.inputs.claim_key,
            "prior_confidence": _float(reason, "prior_confidence", self.inputs.prior_confidence),
            "abstention_threshold": _float(reason, "abstention_threshold",
                                           self.inputs.abstention_threshold),
            "as_of": _iso(reason, "as_of", self.inputs.as_of),
            "evidence": _sense_state(self.inputs.evidence, DriftFailure.INVALID_SENSE_RESULT),
        }
        payload["drift_score"] = _float(reason, "drift_score", expected[0])
        payload["posterior_confidence"] = _float(reason, "posterior_confidence", expected[1])
        payload["abstain"] = expected[2]
        payload["supporting_evidence_ids"] = list(expected[3])
        payload["refuting_evidence_ids"] = list(expected[4])
        return payload


def _assess(inputs: DriftInputs) -> tuple:
    """Score drift from usable, fresh, knowable evidence only.

    Iteration follows canonical SENSE order, so the float additions happen in the same sequence for
    every caller permutation and the score is bit-identical.
    """
    quality_weight = {EvidenceQuality.VERIFIED: 1.0,
                      EvidenceQuality.OBSERVED_ONLY: 0.6,
                      EvidenceQuality.UNKNOWN: 0.3}
    supporting: list[str] = []
    refuting: list[str] = []
    support = 0.0
    counter = 0.0
    for item in inputs.evidence.usable:
        for assertion in item.assertions:
            if assertion.claim_key != inputs.claim_key:
                continue
            if assertion.stance is Stance.REFUTES:
                counter += quality_weight[item.quality]
                refuting.append(item.evidence_id)
            else:
                support += quality_weight[item.quality]
                supporting.append(item.evidence_id)
    bearing = bool(supporting or refuting)
    # Empty usable evidence cannot manufacture drift: no bearing item means no score movement and
    # no abstention, however low the prior or however permissive the threshold.
    score = counter / (support + counter) if bearing else 0.0
    prior = float(inputs.prior_confidence)
    if not bearing or score == 0.0:
        posterior = prior
    else:
        posterior = max(0.0, min(prior, prior * (1.0 - score)))
    abstain = bearing and posterior <= float(inputs.abstention_threshold)
    return score, posterior, abstain, tuple(sorted(supporting)), tuple(sorted(refuting))


def evaluate_drift(model: ModelRecord, evidence: SenseResult, *, claim_key: str,
                   prior_confidence: float, abstention_threshold: float,
                   as_of: datetime) -> DriftResult:
    """Assess regime drift for `model` from canonically revalidated SENSE evidence, or fail closed.

    Pure: no clock, no I/O, no provider, no side effect.  Drift can only lower confidence or force
    abstention; it can never raise confidence and it never represents an action.
    """
    calibration = "LEARN_DRIFT_V1"
    try:
        inputs = DriftInputs(model, evidence, claim_key, prior_confidence, abstention_threshold,
                             as_of)
        score, posterior, abstain, supporting, refuting = _assess(inputs)
        return DriftResult(True, (), inputs, score, posterior, abstain, supporting, refuting,
                           calibration)
    except _LearnError as exc:
        reason = exc.reason if type(exc.reason) is DriftFailure else DriftFailure.INVALID_INPUT
        return DriftResult(False, (reason,), None, 0.0, 0.0, False, (), (), calibration)
    except Exception:  # noqa: BLE001 - hostile explicit input must fail closed
        return DriftResult(False, (DriftFailure.INVALID_INPUT,), None, 0.0, 0.0, False, (), (),
                           calibration)


# -------------------------------------------------------------------------------- COMPARISON

@dataclass(frozen=True, slots=True)
class ComparisonInputs:
    """The two records and the two proofs a comparison consumed."""

    champion: ModelRecord
    challenger: ModelRecord
    champion_proof: ProveResult
    challenger_proof: ProveResult
    as_of: datetime

    def __post_init__(self) -> None:
        _bind_comparison(self)


def _bind_comparison(inputs: object) -> tuple:
    """Fixed, side-symmetric phases.

    Shells first, then proof integrity, proven status and proof-to-model binding, and only then
    roles, identities and point-in-time knowability.  Binding precedes knowability so a
    future-registered model holding a wrong-proposal proof is reported as the mismatch it is.
    """
    if type(inputs) is not ComparisonInputs:
        raise _LearnError(ComparisonFailure.INVALID_INPUT, "an exact ComparisonInputs is required")
    as_of = _instant(ComparisonFailure.INVALID_INPUT, "as_of", inputs.as_of)
    champion_registered = _validate_model(inputs.champion, ComparisonFailure.INVALID_MODEL)
    challenger_registered = _validate_model(inputs.challenger, ComparisonFailure.INVALID_MODEL)
    champion_checksum = _proof_checksum(inputs.champion_proof, ComparisonFailure.INVALID_PROOF)
    challenger_checksum = _proof_checksum(inputs.challenger_proof, ComparisonFailure.INVALID_PROOF)
    if inputs.champion_proof.proven is not True or inputs.challenger_proof.proven is not True:
        raise _LearnError(ComparisonFailure.PROOF_NOT_PROVEN,
                          "both proofs must be accepted PROVE records")
    champion_proof = _summarise(inputs.champion_proof, champion_checksum,
                                ComparisonFailure.INVALID_PROOF)
    challenger_proof = _summarise(inputs.challenger_proof, challenger_checksum,
                                  ComparisonFailure.INVALID_PROOF)
    for record, summary in ((inputs.champion, champion_proof),
                            (inputs.challenger, challenger_proof)):
        if summary.proposal_id != record.proposal_id:
            raise _LearnError(ComparisonFailure.PROOF_MODEL_MISMATCH,
                              "a proof must grade exactly the proposal its model names")
    if (inputs.champion.role is not ModelRole.CHAMPION
            or inputs.challenger.role is not ModelRole.CHALLENGER):
        raise _LearnError(ComparisonFailure.ROLE_MISMATCH,
                          "a comparison needs one CHAMPION and one CHALLENGER")
    if (inputs.champion.model_id == inputs.challenger.model_id
            or inputs.champion.proposal_id == inputs.challenger.proposal_id):
        raise _LearnError(ComparisonFailure.SELF_COMPARISON,
                          "a model cannot be compared with itself")
    for moment in (champion_registered, challenger_registered,
                   _instant(ComparisonFailure.UNKNOWABLE_EVIDENCE, "as_of", champion_proof.as_of),
                   _instant(ComparisonFailure.UNKNOWABLE_EVIDENCE, "as_of",
                            challenger_proof.as_of)):
        if moment > as_of:
            raise _LearnError(ComparisonFailure.UNKNOWABLE_EVIDENCE,
                              "comparison evidence must be knowable at the as-of instant")
    return champion_proof, challenger_proof, as_of


@dataclass(frozen=True, slots=True, eq=False)
class ComparisonResult(_Sealed):
    """Which side the proof favours, as evidence only.  It carries no promotion capability."""

    accepted: bool
    reasons: tuple[ComparisonFailure, ...]
    inputs: ComparisonInputs | None
    preference: ComparisonPreference | None
    champion_proof: ProofSummary | None
    challenger_proof: ProofSummary | None
    calibration: str

    def _state(self) -> dict:
        reason = ComparisonFailure.INVALID_INPUT
        accepted = _flag(reason, "accepted", self.accepted)
        reasons = _items(reason, "reasons", self.reasons, ComparisonFailure)
        if _text(reason, "calibration", self.calibration) != "LEARN_COMPARISON_V1":
            raise _LearnError(reason, "calibration must be this module's own LEARN calibration")
        if accepted != (not reasons):
            raise _LearnError(reason, "a result is accepted exactly when it carries no reason")
        payload = {"schema": "LEARN_COMPARISON_RESULT_V1", "calibration": self.calibration,
                   "accepted": accepted, "reasons": [item.value for item in reasons]}
        if not accepted:
            if len(reasons) != 1:
                raise _LearnError(reason, "a refusal reports exactly one stable reason")
            if (self.inputs is not None or self.preference is not None
                    or self.champion_proof is not None or self.challenger_proof is not None):
                raise _LearnError(reason, "a refusal carries no evidence and no preference")
            return payload
        champion_proof, challenger_proof, as_of = _bind_comparison(self.inputs)
        if type(self.preference) is not ComparisonPreference:
            raise _LearnError(reason, "preference must be an explicit ComparisonPreference")
        if (_summary_state(self.champion_proof, ComparisonFailure.INVALID_PROOF)
                != _summary_state(champion_proof, ComparisonFailure.INVALID_PROOF)
                or _summary_state(self.challenger_proof, ComparisonFailure.INVALID_PROOF)
                != _summary_state(challenger_proof, ComparisonFailure.INVALID_PROOF)):
            raise _LearnError(reason, "the stored proof snapshots do not match the bound inputs")
        if self.preference is not _prefer(champion_proof, challenger_proof):
            raise _LearnError(reason, "the preference does not reconcile with the bound proofs")
        payload["inputs"] = {
            "champion": _model_state(self.inputs.champion, ComparisonFailure.INVALID_MODEL),
            "challenger": _model_state(self.inputs.challenger, ComparisonFailure.INVALID_MODEL),
            "as_of": as_of.isoformat(),
        }
        payload["preference"] = self.preference.value
        payload["champion_proof"] = _summary_state(self.champion_proof,
                                                   ComparisonFailure.INVALID_PROOF)
        payload["challenger_proof"] = _summary_state(self.challenger_proof,
                                                     ComparisonFailure.INVALID_PROOF)
        return payload

def _prefer(champion: ProofSummary, challenger: ProofSummary) -> ComparisonPreference:
    """Evidence only: the side whose complete proof nets more.  Ties stay inconclusive."""
    if challenger.net_return > champion.net_return:
        return ComparisonPreference.CHALLENGER
    if champion.net_return > challenger.net_return:
        return ComparisonPreference.CHAMPION
    return ComparisonPreference.INCONCLUSIVE


def evaluate_comparison(champion: ModelRecord, challenger: ModelRecord, *,
                        champion_proof: ProveResult, challenger_proof: ProveResult,
                        as_of: datetime) -> ComparisonResult:
    """Compare a champion with a challenger on complete walk-forward proof alone.

    Pure and evidence-only: the result confers no promotion, deployment, sizing or allocation
    authority, and nothing here can change a model's role.
    """
    calibration = "LEARN_COMPARISON_V1"
    try:
        inputs = ComparisonInputs(champion, challenger, champion_proof, challenger_proof, as_of)
        champion_summary, challenger_summary, _ = _bind_comparison(inputs)
        return ComparisonResult(True, (), inputs, _prefer(champion_summary, challenger_summary),
                                champion_summary, challenger_summary, calibration)
    except _LearnError as exc:
        reason = (exc.reason if type(exc.reason) is ComparisonFailure
                  else ComparisonFailure.INVALID_INPUT)
        return ComparisonResult(False, (reason,), None, None, None, None, calibration)
    except Exception:  # noqa: BLE001 - hostile explicit input must fail closed
        return ComparisonResult(False, (ComparisonFailure.INVALID_INPUT,), None, None, None, None,
                                calibration)


# -------------------------------------------------------------------------------- TRANSITIONS

def _require_accepted(result: object, kind: type, reason) -> None:
    """Translate any failed, malformed or non-reconciling nested result to the caller's reason.

    Another layer's enum is never copied into this refusal, and building the refusal never raises.
    """
    if type(result) is not kind:
        raise _LearnError(reason, f"an exact accepted {kind.__name__} is required")
    try:
        if result.accepted is not True:
            raise _LearnError(reason, "the nested result is a refusal")
        result.checksum()
    except _LearnError as exc:
        if exc.reason is reason:
            raise
        raise _LearnError(reason, "the nested result is not an accepted LEARN result") from exc
    except Exception as exc:
        raise _LearnError(reason, "the nested result is not an accepted LEARN result") from exc


@dataclass(frozen=True, slots=True)
class RetirementInputs:
    """The target record, the evidence behind the transition and the caller's explicit instant."""

    model: ModelRecord
    drift: DriftResult | None
    comparison: ComparisonResult | None
    retirement_id: str
    as_of: datetime

    def __post_init__(self) -> None:
        _bind_retirement(self)


def _bind_retirement(inputs: object) -> tuple:
    """Fixed phases: shell, model shape, explicit scalars, already-retired, evidence, binding, grounds.

    ``MODEL_ALREADY_RETIRED`` is proven before any evidence is bound, so it stays reachable for
    every already-retired target regardless of what accompanies it.
    """
    if type(inputs) is not RetirementInputs:
        raise _LearnError(TransitionFailure.INVALID_INPUT, "an exact RetirementInputs is required")
    _validate_model(inputs.model, TransitionFailure.INVALID_MODEL)
    reason = TransitionFailure.INVALID_INPUT
    _text(reason, "retirement_id", inputs.retirement_id)
    as_of = _instant(reason, "as_of", inputs.as_of)
    if inputs.model.role is ModelRole.RETIRED:
        raise _LearnError(TransitionFailure.MODEL_ALREADY_RETIRED,
                          "an already retired model cannot be retired again")
    target = _model_state(inputs.model, TransitionFailure.INVALID_MODEL)
    grounds: list[RetirementGround] = []
    if inputs.drift is not None:
        _require_accepted(inputs.drift, DriftResult, TransitionFailure.INVALID_DRIFT)
    if inputs.comparison is not None:
        _require_accepted(inputs.comparison, ComparisonResult,
                          TransitionFailure.INVALID_COMPARISON)
    if inputs.drift is not None:
        if _model_state(inputs.drift.inputs.model, TransitionFailure.INVALID_MODEL) != target:
            raise _LearnError(TransitionFailure.EVIDENCE_MODEL_MISMATCH,
                              "drift evidence must name the exact model being retired")
        if (_instant(reason, "as_of", inputs.drift.inputs.as_of) <= as_of
                and inputs.drift.abstain is True):
            grounds.append(RetirementGround.DRIFT_ABSTENTION)
    if inputs.comparison is not None:
        if (_model_state(inputs.comparison.inputs.champion, TransitionFailure.INVALID_MODEL)
                != target):
            raise _LearnError(TransitionFailure.EVIDENCE_MODEL_MISMATCH,
                              "comparison evidence must name the exact model being retired")
        if (_instant(reason, "as_of", inputs.comparison.inputs.as_of) <= as_of
                and inputs.comparison.preference is ComparisonPreference.CHALLENGER):
            grounds.append(RetirementGround.INFERIOR_COMPARISON)
    if not grounds:
        raise _LearnError(TransitionFailure.INSUFFICIENT_EVIDENCE,
                          "a retirement needs drift abstention or inferior comparison evidence")
    return tuple(sorted(grounds, key=lambda ground: ground.value)), as_of, target


@dataclass(frozen=True, slots=True, eq=False)
class RetirementResult(_Sealed):
    """An evidence-backed, exactly reversible retirement, or a fail-closed refusal."""

    accepted: bool
    reasons: tuple[TransitionFailure, ...]
    inputs: RetirementInputs | None
    model_id: str | None
    previous_role: ModelRole | None
    grounds: tuple[RetirementGround, ...]
    retirement_id: str | None
    retired_at: datetime | None
    calibration: str

    def _state(self) -> dict:
        reason = TransitionFailure.INVALID_INPUT
        accepted = _flag(reason, "accepted", self.accepted)
        reasons = _items(reason, "reasons", self.reasons, TransitionFailure)
        if _text(reason, "calibration", self.calibration) != "LEARN_TRANSITION_V1":
            raise _LearnError(reason, "calibration must be this module's own LEARN calibration")
        if accepted != (not reasons):
            raise _LearnError(reason, "a result is accepted exactly when it carries no reason")
        payload = {"schema": "LEARN_RETIREMENT_RESULT_V1", "calibration": self.calibration,
                   "accepted": accepted, "reasons": [item.value for item in reasons]}
        if not accepted:
            grounds = _items(reason, "grounds", self.grounds, RetirementGround)
            if len(reasons) != 1:
                raise _LearnError(reason, "a refusal reports exactly one stable reason")
            if (self.inputs is not None or self.model_id is not None
                    or self.previous_role is not None or grounds
                    or self.retirement_id is not None or self.retired_at is not None):
                raise _LearnError(reason, "a refusal carries no evidence and no transition")
            return payload
        grounds, as_of, target = _bind_retirement(self.inputs)
        model_id = _text(reason, "model_id", self.model_id)
        retirement_id = _text(reason, "retirement_id", self.retirement_id)
        if (model_id != self.inputs.model.model_id
                or self.previous_role is not self.inputs.model.role
                or retirement_id != self.inputs.retirement_id
                or tuple(_items(reason, "grounds", self.grounds, RetirementGround)) != grounds
                or _instant(reason, "retired_at", self.retired_at) != as_of):
            raise _LearnError(reason, "the transition does not reconcile with the bound inputs")
        payload["model"] = target
        payload["previous_role"] = self.previous_role.value
        payload["grounds"] = [ground.value for ground in grounds]
        payload["retirement_id"] = self.retirement_id
        payload["retired_at"] = as_of.isoformat()
        payload["drift"] = None if self.inputs.drift is None else self.inputs.drift._state()
        payload["comparison"] = (None if self.inputs.comparison is None
                                 else self.inputs.comparison._state())
        return payload

def evaluate_retirement(model: ModelRecord, *, retirement_id: str, as_of: datetime,
                        drift: DriftResult | None = None,
                        comparison: ComparisonResult | None = None) -> RetirementResult:
    """Retire `model` when accepted evidence establishes an admissible ground, or fail closed.

    The record is reversible by construction: it keeps the exact previous role and the complete
    canonical evidence behind the transition.  Nothing here deploys, promotes or sizes anything.
    """
    calibration = "LEARN_TRANSITION_V1"
    try:
        inputs = RetirementInputs(model, drift, comparison, retirement_id, as_of)
        grounds, moment, _ = _bind_retirement(inputs)
        return RetirementResult(True, (), inputs, inputs.model.model_id, inputs.model.role,
                                grounds, inputs.retirement_id, moment, calibration)
    except _LearnError as exc:
        reason = (exc.reason if type(exc.reason) is TransitionFailure
                  else TransitionFailure.INVALID_INPUT)
        return RetirementResult(False, (reason,), None, None, None, (), None, None, calibration)
    except Exception:  # noqa: BLE001 - hostile explicit input must fail closed
        return RetirementResult(False, (TransitionFailure.INVALID_INPUT,), None, None, None, (),
                                None, None, calibration)


@dataclass(frozen=True, slots=True)
class ReinstatementInputs:
    """The exact retirement being reversed, the reversal id and the explicit instant."""

    retirement: RetirementResult
    reversal_id: str
    as_of: datetime

    def __post_init__(self) -> None:
        _bind_reinstatement(self)


def _bind_reinstatement(inputs: object) -> tuple:
    if type(inputs) is not ReinstatementInputs:
        raise _LearnError(TransitionFailure.INVALID_INPUT,
                          "an exact ReinstatementInputs is required")
    _require_accepted(inputs.retirement, RetirementResult, TransitionFailure.INVALID_RETIREMENT)
    reason = TransitionFailure.INVALID_INPUT
    _text(reason, "reversal_id", inputs.reversal_id)
    as_of = _instant(reason, "as_of", inputs.as_of)
    retired_at = _instant(TransitionFailure.INVALID_RETIREMENT, "retired_at",
                          inputs.retirement.retired_at)
    if as_of <= retired_at:
        raise _LearnError(TransitionFailure.RETIREMENT_NOT_PRIOR,
                          "a reinstatement must follow the retirement it reverses")
    role = inputs.retirement.previous_role
    if type(role) is not ModelRole or role is ModelRole.RETIRED:
        raise _LearnError(TransitionFailure.INVALID_RETIREMENT,
                          "only an original active role can be restored")
    return role, as_of, inputs.retirement.checksum()


@dataclass(frozen=True, slots=True, eq=False)
class ReinstatementResult(_Sealed):
    """The exact reversal of one retirement, or a fail-closed refusal.

    It restores the original role and nothing else: a challenger comes back a challenger.
    """

    accepted: bool
    reasons: tuple[TransitionFailure, ...]
    inputs: ReinstatementInputs | None
    model_id: str | None
    restored_role: ModelRole | None
    retirement_id: str | None
    retirement_checksum: str | None
    reversal_id: str | None
    reinstated_at: datetime | None
    calibration: str

    def _state(self) -> dict:
        reason = TransitionFailure.INVALID_INPUT
        accepted = _flag(reason, "accepted", self.accepted)
        reasons = _items(reason, "reasons", self.reasons, TransitionFailure)
        if _text(reason, "calibration", self.calibration) != "LEARN_TRANSITION_V1":
            raise _LearnError(reason, "calibration must be this module's own LEARN calibration")
        if accepted != (not reasons):
            raise _LearnError(reason, "a result is accepted exactly when it carries no reason")
        payload = {"schema": "LEARN_REINSTATEMENT_RESULT_V1", "calibration": self.calibration,
                   "accepted": accepted, "reasons": [item.value for item in reasons]}
        if not accepted:
            if len(reasons) != 1:
                raise _LearnError(reason, "a refusal reports exactly one stable reason")
            if (self.inputs is not None or self.model_id is not None
                    or self.restored_role is not None or self.retirement_id is not None
                    or self.retirement_checksum is not None or self.reversal_id is not None
                    or self.reinstated_at is not None):
                raise _LearnError(reason, "a refusal carries no evidence and no transition")
            return payload
        role, as_of, checksum = _bind_reinstatement(self.inputs)
        retirement = self.inputs.retirement
        model_id = _text(reason, "model_id", self.model_id)
        retirement_id = _text(reason, "retirement_id", self.retirement_id)
        retirement_checksum = _text(reason, "retirement_checksum", self.retirement_checksum)
        reversal_id = _text(reason, "reversal_id", self.reversal_id)
        if (self.restored_role is not role or retirement_checksum != checksum
                or model_id != retirement.model_id
                or retirement_id != retirement.retirement_id
                or reversal_id != self.inputs.reversal_id
                or _instant(reason, "reinstated_at", self.reinstated_at) != as_of):
            raise _LearnError(reason, "the reversal does not reconcile with the bound retirement")
        payload["model_id"] = self.model_id
        payload["restored_role"] = role.value
        payload["retirement_id"] = self.retirement_id
        payload["retirement_checksum"] = checksum
        payload["reversal_id"] = self.reversal_id
        payload["reinstated_at"] = as_of.isoformat()
        # The whole retirement state is embedded, so two retirements that differ only in the
        # evidence behind them can never share a reinstatement checksum.
        payload["retirement"] = retirement._state()
        return payload

def evaluate_reinstatement(retirement: RetirementResult, *, reversal_id: str,
                           as_of: datetime) -> ReinstatementResult:
    """Reverse the exact retirement supplied, restoring only the role the model held before it.

    Stateless: this records no consumption and makes no exactly-once promise.  Replaying identical
    inputs reproduces an equal result with an identical checksum.
    """
    calibration = "LEARN_TRANSITION_V1"
    try:
        inputs = ReinstatementInputs(retirement, reversal_id, as_of)
        role, moment, checksum = _bind_reinstatement(inputs)
        return ReinstatementResult(True, (), inputs, inputs.retirement.model_id, role,
                                   inputs.retirement.retirement_id, checksum,
                                   inputs.reversal_id, moment, calibration)
    except _LearnError as exc:
        reason = (exc.reason if type(exc.reason) is TransitionFailure
                  else TransitionFailure.INVALID_INPUT)
        return ReinstatementResult(False, (reason,), None, None, None, None, None, None, None,
                                   calibration)
    except Exception:  # noqa: BLE001 - hostile explicit input must fail closed
        return ReinstatementResult(False, (TransitionFailure.INVALID_INPUT,), None, None, None,
                                   None, None, None, None, calibration)
