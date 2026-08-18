"""Small, auditable vocabulary for SENSE → THINK → PROVE → LEARN.

No execution action is represented.  A proposal is research evidence, never an
order.  Production trading remains disabled until separately governed phases.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
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


@dataclass(frozen=True, slots=True)
class Assertion:
    """An exact, un-normalised claim key plus the stance this evidence takes on it."""

    claim_key: str
    stance: Stance

    def __post_init__(self) -> None:
        if not self.claim_key:
            raise ValueError("claim_key is required")
        if not isinstance(self.stance, Stance):
            raise ValueError("stance must be an explicit Stance")


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
        if not self.evidence_id:
            raise ValueError("evidence_id is required")
        event = require_aware("event_time", self.event_time)
        available = require_aware("available_time", self.available_time)
        observed = require_aware("observed_time", self.observed_time)
        if available < event:
            raise ValueError("available_time cannot precede event_time")
        if observed < available:
            raise ValueError("observed_time cannot precede available_time")
        if not isinstance(self.assertions, tuple):
            raise ValueError("assertions must be a tuple")
        keys = [assertion.claim_key for assertion in self.assertions]
        if len(set(keys)) != len(keys):
            raise ValueError("one evidence item cannot assert the same claim key twice")


@dataclass(frozen=True, slots=True)
class Belief:
    claim: str
    score: float
    evidence_ids: tuple[str, ...]
    counter_evidence_ids: tuple[str, ...]
    valid_until: datetime

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1:
            raise ValueError("belief score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    thesis: str
    plausibility_score: float
    invalidation_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.plausibility_score <= 1:
            raise ValueError("scenario score must be between 0 and 1")
        if not self.invalidation_conditions:
            raise ValueError("a scenario must be falsifiable")


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

    def checksum(self) -> str:
        payload = asdict(self)
        payload["action"] = self.action.value
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
