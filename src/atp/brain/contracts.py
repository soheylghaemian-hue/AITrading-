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


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    source: str
    event_time: datetime
    published_time: datetime | None
    received_time: datetime
    quality: EvidenceQuality
    checksum: str

    def __post_init__(self) -> None:
        if self.received_time < self.event_time:
            raise ValueError("received_time cannot precede event_time")
        if self.published_time and self.received_time < self.published_time:
            raise ValueError("received_time cannot precede published_time")


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
