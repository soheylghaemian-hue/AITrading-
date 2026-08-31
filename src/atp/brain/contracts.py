"""Small, auditable vocabulary for SENSE → THINK → PROVE → LEARN.

No execution action is represented.  A proposal is research evidence, never an
order.  Production trading remains disabled until separately governed phases.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum


class EvidenceQuality(str, Enum):
    VERIFIED = "VERIFIED"
    OBSERVED_ONLY = "OBSERVED_ONLY"
    UNKNOWN = "UNKNOWN"


class ProposalAction(str, Enum):
    STUDY = "STUDY"
    SHADOW = "SHADOW"
    ABSTAIN = "ABSTAIN"
    REJECT = "REJECT"


class Stance(str, Enum):
    """How one evidence item stands on one exact claim key.  There is no neutral value:
    an assertion is either explicitly made or absent."""

    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"


def require_aware(field: str, value: object) -> datetime:
    """Return `value` when it is a timezone-aware datetime, otherwise fail closed.

    A missing or naive timestamp is never repaired and never assumed to be UTC: point-in-time
    integrity cannot be proven against an ambiguous instant.
    """
    if not isinstance(value, datetime):
        raise ValueError(f"{field} is required and must be a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def to_utc(field: str, value: object) -> datetime:
    """Return `value` as a UTC instant, so every comparison is absolute.

    Python compares two aware datetimes that share one ``tzinfo`` by wall time, so during a DST
    fold an absolutely later instant can compare equal or earlier.  Ordering, future, freshness
    and validity checks therefore normalise first, and two spellings of one instant always
    produce the same answer.
    """
    return require_aware(field, value).astimezone(timezone.utc)


def require_identifier(field: str, value: object) -> str:
    """An identifier is a non-empty ``str``; a truthy lookalike is rejected, never coerced."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_unit_score(field: str, value: object) -> float:
    """A score must be real, finite and within ``[0, 1]``.

    ``NaN`` satisfies no ordering yet slips through a naive range check, so it fails closed here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a real number")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError(f"{field} must be a finite score between 0 and 1")
    return score


def require_id_tuple(field: str, value: object) -> tuple[str, ...]:
    """A tuple of distinct, non-empty ids: a list is mutable and a repeat is double counting."""
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    for item in value:
        require_identifier(field, item)
    if len(set(value)) != len(value):
        raise ValueError(f"{field} cannot repeat an id")
    return value


@dataclass(frozen=True, slots=True)
class Assertion:
    """An exact, un-normalised claim key plus the stance this evidence takes on it."""

    claim_key: str
    stance: Stance

    def __post_init__(self) -> None:
        require_identifier("claim_key", self.claim_key)
        if not isinstance(self.stance, Stance):
            raise ValueError("stance must be an explicit Stance")


@dataclass(frozen=True, slots=True)
class InvalidationCondition:
    """A machine-checkable falsifier.

    It names the exact ``claim_key`` and the stance of admitted evidence that would break the
    scenario, so falsification is decidable by code rather than by reading prose.
    """

    condition_id: str
    claim_key: str
    trigger_stance: Stance

    def __post_init__(self) -> None:
        require_identifier("condition_id", self.condition_id)
        require_identifier("claim_key", self.claim_key)
        if not isinstance(self.trigger_stance, Stance):
            raise ValueError("trigger_stance must be an explicit Stance")


def require_conditions(field: str, value: object) -> tuple[InvalidationCondition, ...]:
    """At least one typed, distinct, fully formed condition, revalidated field by field.

    A frozen dataclass can still be tampered with through ``object.__setattr__``, so every
    boundary re-proves these invariants instead of trusting the declared type.
    """
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    if not value:
        raise ValueError(f"{field} must hold at least one machine-checkable condition")
    for condition in value:
        if not isinstance(condition, InvalidationCondition):
            raise ValueError(f"{field} must hold InvalidationCondition items only")
        require_identifier("condition_id", condition.condition_id)
        require_identifier("claim_key", condition.claim_key)
        if not isinstance(condition.trigger_stance, Stance):
            raise ValueError("trigger_stance must be an explicit Stance")
    require_id_tuple(field, tuple(condition.condition_id for condition in value))
    return value


@dataclass(frozen=True, slots=True)
class Evidence:
    """One point-in-time observation and what it asserts.

    * ``event_time`` — when the fact itself happened in the world.
    * ``available_time`` — when the fact first became externally knowable (published or filed).
    * ``observed_time`` — when this system actually observed the fact.

    All three are required, timezone-aware and ordered
    ``event_time <= available_time <= observed_time``.  No timestamp is inferred from another.
    """

    evidence_id: str
    source: str
    event_time: datetime
    available_time: datetime
    observed_time: datetime
    quality: EvidenceQuality
    checksum: str
    assertions: tuple[Assertion, ...] = ()

    def __post_init__(self) -> None:
        require_identifier("evidence_id", self.evidence_id)
        require_identifier("source", self.source)
        require_identifier("checksum", self.checksum)
        if not isinstance(self.quality, EvidenceQuality):
            raise ValueError("quality must be an explicit EvidenceQuality")
        event = to_utc("event_time", self.event_time)
        available = to_utc("available_time", self.available_time)
        observed = to_utc("observed_time", self.observed_time)
        if available < event:
            raise ValueError("available_time cannot precede event_time")
        if observed < available:
            raise ValueError("observed_time cannot precede available_time")
        if not isinstance(self.assertions, tuple):
            raise ValueError("assertions must be a tuple")
        if any(not isinstance(item, Assertion) for item in self.assertions):
            raise ValueError("assertions must hold Assertion items only")
        keys = [assertion.claim_key for assertion in self.assertions]
        if len(set(keys)) != len(keys):
            raise ValueError("one evidence item cannot assert the same claim key twice")


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One explicit, competing explanation the caller wants tested.

    ``stance`` is the position this hypothesis takes on ``claim_key``: two hypotheses over one
    key with opposing stances stay competitors and are never collapsed into a winner.  ``prior``
    is the caller's own bounded starting confidence, and every hypothesis must already be
    falsifiable.
    """

    hypothesis_id: str
    claim_key: str
    stance: Stance
    thesis: str
    prior: float
    invalidation_conditions: tuple[InvalidationCondition, ...]

    def __post_init__(self) -> None:
        require_identifier("hypothesis_id", self.hypothesis_id)
        require_identifier("claim_key", self.claim_key)
        require_identifier("thesis", self.thesis)
        if not isinstance(self.stance, Stance):
            raise ValueError("stance must be an explicit Stance")
        require_unit_score("prior", self.prior)
        require_conditions("invalidation_conditions", self.invalidation_conditions)


@dataclass(frozen=True, slots=True)
class Belief:
    """One bounded position on a claim, valid only until an evidence-derived horizon."""

    claim: str
    score: float
    evidence_ids: tuple[str, ...]
    counter_evidence_ids: tuple[str, ...]
    valid_until: datetime

    def __post_init__(self) -> None:
        require_identifier("claim", self.claim)
        require_unit_score("score", self.score)
        require_id_tuple("evidence_ids", self.evidence_ids)
        require_id_tuple("counter_evidence_ids", self.counter_evidence_ids)
        if set(self.evidence_ids) & set(self.counter_evidence_ids):
            raise ValueError("one evidence item cannot both support and counter a belief")
        to_utc("valid_until", self.valid_until)


@dataclass(frozen=True, slots=True)
class Scenario:
    """A falsifiable story: prose plus at least one machine-checkable invalidation condition."""

    scenario_id: str
    thesis: str
    plausibility_score: float
    invalidation_conditions: tuple[InvalidationCondition, ...]

    def __post_init__(self) -> None:
        require_identifier("scenario_id", self.scenario_id)
        require_identifier("thesis", self.thesis)
        require_unit_score("plausibility_score", self.plausibility_score)
        require_conditions("invalidation_conditions", self.invalidation_conditions)


@dataclass(frozen=True, slots=True)
class Constitution:
    version: str = "TRADER_BRAIN_RESEARCH_ONLY_V1"
    research_only: bool = True
    autonomous_execution: bool = False
    real_money: bool = False
    leverage_enabled: bool = False
    may_relax_own_limits: bool = False

    def __post_init__(self) -> None:
        if not self.research_only or any((self.autonomous_execution, self.real_money,
                                         self.leverage_enabled, self.may_relax_own_limits)):
            raise ValueError("V1 constitution is immutable and research-only")


@dataclass(frozen=True, slots=True)
class BrainProposal:
    proposal_id: str
    created_at: datetime
    action: ProposalAction
    thesis: str
    scenarios: tuple[Scenario, ...]
    required_evidence: tuple[str, ...]
    uncertainty: str
    constitution_version: str = "TRADER_BRAIN_RESEARCH_ONLY_V1"

    def __post_init__(self) -> None:
        require_identifier("proposal_id", self.proposal_id)
        require_identifier("thesis", self.thesis)
        require_identifier("uncertainty", self.uncertainty)
        require_identifier("constitution_version", self.constitution_version)
        to_utc("created_at", self.created_at)
        if not isinstance(self.action, ProposalAction):
            raise ValueError("action must be an explicit ProposalAction")
        if not isinstance(self.scenarios, tuple):
            raise ValueError("scenarios must be a tuple")
        if any(not isinstance(item, Scenario) for item in self.scenarios):
            raise ValueError("scenarios must hold Scenario items only")
        require_id_tuple("required_evidence", self.required_evidence)

    def checksum(self) -> str:
        payload = asdict(self)
        payload["created_at"] = to_utc("created_at", self.created_at).isoformat()
        payload["action"] = self.action.value
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
