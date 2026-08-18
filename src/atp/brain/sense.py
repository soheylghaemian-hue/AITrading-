"""SENSE — pure, point-in-time evidence admission for the research-only Trader Brain.

Every input is explicit: the evidence set, the caller's ``as_of`` instant and a non-negative
freshness limit.  The evaluator never reads the clock, performs I/O or calls a provider, so
equivalent inputs always produce an identical result.

It fails closed.  Anything that cannot be proven knowable at ``as_of`` is rejected with one
stable reason code and kept for audit rather than repaired, averaged away or silently dropped:

  * ``DUPLICATE_EVIDENCE_ID`` — an id appears more than once; every occurrence is rejected
    instead of implicitly preferring one of them;
  * ``EVENT_TIME_AFTER_AS_OF`` / ``AVAILABLE_TIME_AFTER_AS_OF`` / ``OBSERVED_TIME_AFTER_AS_OF``
    — future leakage on that specific timestamp;
  * ``STALE_BEYOND_FRESHNESS_LIMIT`` — the evidence is older than the caller allows.

Freshness is *observation* age, ``as_of - observed_time``, matching the provenance convention
in ``atp.research.intel.provenance``.  The boundary is inclusive: an age exactly equal to the
limit is still usable.

Contradictions are reported, never resolved: opposing stances on the same exact claim key stay
individually visible with every contributing evidence id retained.  No order, allocation or
execution authority is represented here.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .contracts import Assertion, Evidence, Stance, require_aware


class SenseFailure(str, Enum):
    DUPLICATE_EVIDENCE_ID = "DUPLICATE_EVIDENCE_ID"
    EVENT_TIME_AFTER_AS_OF = "EVENT_TIME_AFTER_AS_OF"
    AVAILABLE_TIME_AFTER_AS_OF = "AVAILABLE_TIME_AFTER_AS_OF"
    OBSERVED_TIME_AFTER_AS_OF = "OBSERVED_TIME_AFTER_AS_OF"
    STALE_BEYOND_FRESHNESS_LIMIT = "STALE_BEYOND_FRESHNESS_LIMIT"


@dataclass(frozen=True, slots=True)
class RejectedEvidence:
    """The excluded item itself, so an audit never has to re-derive what was dropped."""

    evidence: Evidence
    reason: SenseFailure


@dataclass(frozen=True, slots=True)
class ContradictionGroup:
    """Both sides of one exact claim key; every contributing evidence id is retained."""

    claim_key: str
    supporting_evidence_ids: tuple[str, ...]
    refuting_evidence_ids: tuple[str, ...]


def _utc(value: datetime) -> str:
    """Canonical instant form, so two spellings of one instant serialize identically."""
    return value.astimezone(timezone.utc).isoformat()


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _assertion_payload(assertion: Assertion) -> dict:
    return {"claim_key": assertion.claim_key, "stance": assertion.stance.value}


def _evidence_payload(evidence: Evidence) -> dict:
    return {
        "evidence_id": evidence.evidence_id,
        "source": evidence.source,
        "event_time": _utc(evidence.event_time),
        "available_time": _utc(evidence.available_time),
        "observed_time": _utc(evidence.observed_time),
        "quality": evidence.quality.value,
        "checksum": evidence.checksum,
        "assertions": [_assertion_payload(a) for a in
                       sorted(evidence.assertions, key=lambda a: (a.claim_key, a.stance.value))],
    }


def _order_key(evidence: Evidence) -> tuple:
    """Content-derived total order: distinct items can never tie on caller input order."""
    return (evidence.event_time, evidence.available_time, evidence.observed_time,
            evidence.evidence_id, _canonical(_evidence_payload(evidence)))


@dataclass(frozen=True, slots=True)
class SenseResult:
    as_of: datetime
    freshness_limit: timedelta
    usable: tuple[Evidence, ...]
    rejected: tuple[RejectedEvidence, ...]
    contradictions: tuple[ContradictionGroup, ...]

    @property
    def fully_usable(self) -> bool:
        """False whenever anything was rejected, so a partial read is never silent success."""
        return not self.rejected

    def checksum(self) -> str:
        payload = {
            "as_of": _utc(self.as_of),
            "freshness_limit_seconds": self.freshness_limit.total_seconds(),
            "usable": [_evidence_payload(e) for e in self.usable],
            "rejected": [{"evidence": _evidence_payload(r.evidence), "reason": r.reason.value}
                         for r in self.rejected],
            "contradictions": [{"claim_key": g.claim_key,
                                "supporting_evidence_ids": list(g.supporting_evidence_ids),
                                "refuting_evidence_ids": list(g.refuting_evidence_ids)}
                               for g in self.contradictions],
        }
        return "sha256:" + hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _rejection(evidence: Evidence, as_of: datetime, limit: timedelta,
               duplicated: bool) -> SenseFailure | None:
    """Fixed precedence, so the reported reason stays deterministic when several apply."""
    if duplicated:
        return SenseFailure.DUPLICATE_EVIDENCE_ID
    if evidence.event_time > as_of:
        return SenseFailure.EVENT_TIME_AFTER_AS_OF
    if evidence.available_time > as_of:
        return SenseFailure.AVAILABLE_TIME_AFTER_AS_OF
    if evidence.observed_time > as_of:
        return SenseFailure.OBSERVED_TIME_AFTER_AS_OF
    if as_of - evidence.observed_time > limit:
        return SenseFailure.STALE_BEYOND_FRESHNESS_LIMIT
    return None


def _contradictions(usable: list[Evidence]) -> tuple[ContradictionGroup, ...]:
    by_key: dict[str, dict[Stance, list[str]]] = {}
    for evidence in usable:
        for assertion in evidence.assertions:
            stances = by_key.setdefault(assertion.claim_key, {})
            stances.setdefault(assertion.stance, []).append(evidence.evidence_id)
    groups = []
    for claim_key in sorted(by_key):
        stances = by_key[claim_key]
        if Stance.SUPPORTS not in stances or Stance.REFUTES not in stances:
            continue
        groups.append(ContradictionGroup(claim_key, tuple(sorted(stances[Stance.SUPPORTS])),
                                         tuple(sorted(stances[Stance.REFUTES]))))
    return tuple(groups)


def evaluate_sense(evidence: Iterable[Evidence], *, as_of: datetime,
                   freshness_limit: timedelta) -> SenseResult:
    """Admit the evidence knowable at `as_of`, and report what was excluded and what conflicts.

    Pure: no clock, no I/O, no provider.  `as_of` must be timezone-aware and `freshness_limit`
    a non-negative `timedelta`; anything else fails closed instead of degrading the result.
    """
    as_of = require_aware("as_of", as_of)
    if not isinstance(freshness_limit, timedelta):
        raise ValueError("freshness_limit must be a timedelta")
    if freshness_limit < timedelta(0):
        raise ValueError("freshness_limit must be non-negative")
    items = tuple(evidence)
    if any(not isinstance(item, Evidence) for item in items):
        raise ValueError("evaluate_sense accepts Evidence items only")

    seen = Counter(item.evidence_id for item in items)
    usable: list[Evidence] = []
    rejected: list[RejectedEvidence] = []
    for item in items:
        reason = _rejection(item, as_of, freshness_limit, seen[item.evidence_id] > 1)
        if reason is None:
            usable.append(item)
        else:
            rejected.append(RejectedEvidence(item, reason))
    usable.sort(key=_order_key)
    rejected.sort(key=lambda r: (_order_key(r.evidence), r.reason.value))
    return SenseResult(as_of, freshness_limit, tuple(usable), tuple(rejected),
                       _contradictions(usable))
