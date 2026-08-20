"""PROVE — pure, deterministic walk-forward evaluation for the research-only Trader Brain.

PROVE grades one ``BrainProposal`` against outcomes that were *declared before* the evaluation
windows opened.  It is an offline harness: no persistence, no provider, no live data, no risk or
execution integration, no clock and no shared state.  It represents no order, allocation, sizing,
leverage or routing authority, and nothing here may be promoted to trading authority.

The question it answers is narrow on purpose: *does this predeclared, complete, embargoed and
cost-charged record support the proposal?*  Either it returns a proven audit record, or it returns
one stable failure reason with no metrics whatsoever.  A partially graded refusal is never emitted.

What makes the answer trustworthy:

* **Closed windows.**  ``[start, end]`` includes both bounds, so adjacent windows must be strictly
  separated.  Overlapping *and* abutting schedules both fail: sharing an endpoint shares an instant.
* **Rolling folds.**  Each ``EVALUATION`` window is validated against the nearest ``TRAINING`` fold
  that ends before it starts — never against every training fold — so ``train-1 / eval-1 / train-2 /
  eval-2`` is the normal case rather than a contradiction.  Every pair must clear the embargo.
* **Prior declaration.**  Both the proposal and the bound manifest must be timestamped strictly
  earlier than every evaluation start.  An instant equal to the first graded instant proves nothing.
* **Completeness.**  The manifest binds the exact proposal identity and names every expected
  outcome and every evaluation window.  Exactly one observation per expected id: no missing, extra,
  duplicate or unknown-window entry.  A cherry-picked or retrospective outcome set cannot be proven.
* **Explicit economics.**  Gross return, non-negative cost, non-negative delay and abstention are
  each stated.  An abstention is a complete declared outcome that contributes no return and no cost.

The boundary is untrusted.  Public frozen dataclasses stay mutable through ``object.__setattr__``,
so every audit-consuming method revalidates instead of trusting construction, and ``ProveResult``
carries its own accepted inputs so it can regrade itself.  ``proposal_identity`` and
``input_identity`` are derived properties, never stored fields: an unrelated caller-supplied digest
can never be recorded as a trusted fact.  ``type(proposal) is BrainProposal`` is checked before any
attribute access, so a subclass is refused before an overridden attribute or checksum can run.  The
protocol metadata is revalidated with the same suspicion as the numbers: ``calibration`` must be
this module's own label and a refusal must name exactly one stable reason, so neither a forged label
nor a padded reason list can ever be signed.

One exhaustive canonical serializer covers the supported schema and rejects everything else: aware
datetimes normalise to UTC, ``timedelta`` encodes as its exact ``(days, seconds, microseconds)``
triple, finite floats encode losslessly via ``float.hex()``, integers encode as exact decimal text,
and every value is tagged with its type so ``1``, ``"1"`` and ``True`` cannot collide.  Semantic
sets — including the invalidation conditions inside a scenario — are sorted by canonical form before
both evaluation and hashing, so permuting them cannot move the identity a manifest is bound to.
Nothing is rounded, nothing collapses to a token and there is no ``repr``/``str`` fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .contracts import BrainProposal, InvalidationCondition, ProposalAction, Scenario, Stance


class ProveFailure(str, Enum):
    """Stable reason codes.  Exactly one is reported, chosen by a fixed precedence, so the answer
    for a given input never depends on which check happened to run first."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    INELIGIBLE_ACTION = "INELIGIBLE_ACTION"
    INVALID_WINDOW = "INVALID_WINDOW"
    DUPLICATE_WINDOW_ID = "DUPLICATE_WINDOW_ID"
    WINDOW_OVERLAP = "WINDOW_OVERLAP"
    MISSING_EVALUATION_WINDOW = "MISSING_EVALUATION_WINDOW"
    MISSING_TRAINING_FOLD = "MISSING_TRAINING_FOLD"
    INSUFFICIENT_EMBARGO = "INSUFFICIENT_EMBARGO"
    PROPOSAL_NOT_PRIOR = "PROPOSAL_NOT_PRIOR"
    EMPTY_MANIFEST = "EMPTY_MANIFEST"
    MANIFEST_PROPOSAL_MISMATCH = "MANIFEST_PROPOSAL_MISMATCH"
    MANIFEST_NOT_PRIOR = "MANIFEST_NOT_PRIOR"
    DUPLICATE_EXPECTED_OUTCOME = "DUPLICATE_EXPECTED_OUTCOME"
    UNKNOWN_WINDOW_REFERENCE = "UNKNOWN_WINDOW_REFERENCE"
    UNDECLARED_EVALUATION_WINDOW = "UNDECLARED_EVALUATION_WINDOW"
    MISSING_OUTCOME = "MISSING_OUTCOME"
    UNDECLARED_OUTCOME = "UNDECLARED_OUTCOME"
    DUPLICATE_OUTCOME_ID = "DUPLICATE_OUTCOME_ID"
    OUTCOME_WINDOW_MISMATCH = "OUTCOME_WINDOW_MISMATCH"
    OUTCOME_OUTSIDE_WINDOW = "OUTCOME_OUTSIDE_WINDOW"
    OUTCOME_TIMESTAMPS_OUT_OF_ORDER = "OUTCOME_TIMESTAMPS_OUT_OF_ORDER"
    OUTCOME_NOT_AVAILABLE_AT_AS_OF = "OUTCOME_NOT_AVAILABLE_AT_AS_OF"
    INCONSISTENT_ABSTENTION = "INCONSISTENT_ABSTENTION"
    INCONSISTENT_METRICS = "INCONSISTENT_METRICS"


class _ProveError(ValueError):
    """A refusal carrying its stable reason.

    It subclasses ``ValueError`` so that *direct* construction of a forged audit object fails the
    ordinary way, while ``evaluate_prove`` can still map the reason onto a deterministic result.
    """

    def __init__(self, reason: ProveFailure, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


# --------------------------------------------------------------------- exact-type primitives
# Only exact built-in and schema types are accepted.  A subclass can override attribute access,
# comparison or serialization, so it is refused rather than duck-typed into the audit record.

def _text(reason: ProveFailure, field: str, value: object) -> str:
    if type(value) is not str or not value:
        raise _ProveError(reason, f"{field} must be a non-empty str")
    return value


def _flag(reason: ProveFailure, field: str, value: object) -> bool:
    if type(value) is not bool:
        raise _ProveError(reason, f"{field} must be a bool")
    return value


def _count(reason: ProveFailure, field: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise _ProveError(reason, f"{field} must be a non-negative int")
    return value


def _number(reason: ProveFailure, field: str, value: object) -> float:
    """A real quantity is an exact, finite ``float``.

    An ``int`` is refused rather than widened: ``float(10 ** 5000)`` overflows, and silently
    accepting integers would make an enormous smuggled value decide whether the boundary raises.
    """
    if type(value) is not float or not math.isfinite(value):
        raise _ProveError(reason, f"{field} must be a finite float")
    return value


def _unit(reason: ProveFailure, field: str, value: object) -> float:
    """A bounded score, as the contracts layer already defines it: real, finite and in ``[0, 1]``."""
    if type(value) is bool or type(value) not in (int, float):
        raise _ProveError(reason, f"{field} must be a real number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _ProveError(reason, f"{field} is not a representable real number") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise _ProveError(reason, f"{field} must be a finite score between 0 and 1")
    return number


def _instant(reason: ProveFailure, field: str, value: object) -> datetime:
    """Return ``value`` as a UTC instant so every comparison is absolute.

    Two aware datetimes sharing one ``tzinfo`` compare by wall time, so during a DST fold an
    absolutely later instant can compare equal or earlier.  Normalising first removes that.
    A hostile ``tzinfo`` whose ``utcoffset`` raises propagates here and is caught at the public
    boundary, which turns it into a deterministic refusal rather than a leaked exception.
    """
    if type(value) is not datetime:
        raise _ProveError(reason, f"{field} must be a datetime")
    if value.utcoffset() is None:
        raise _ProveError(reason, f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _span(reason: ProveFailure, field: str, value: object) -> timedelta:
    if type(value) is not timedelta:
        raise _ProveError(reason, f"{field} must be a timedelta")
    if value < timedelta(0):
        raise _ProveError(reason, f"{field} must be non-negative")
    return value


def _items(reason: ProveFailure, field: str, value: object, kind: type) -> tuple:
    """An immutable tuple of exact ``kind`` items.

    A list is mutable and an iterator is consumable: accepting either would let a result depend on
    prior evaluation history rather than only on the supplied arguments.
    """
    if type(value) is not tuple:
        raise _ProveError(reason, f"{field} must be a tuple")
    for item in value:
        if type(item) is not kind:
            raise _ProveError(reason, f"{field} must hold exact {kind.__name__} items")
    return value


# ------------------------------------------------------------------------------- schema types

class WindowRole(str, Enum):
    TRAINING = "TRAINING"
    EVALUATION = "EVALUATION"


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    """A closed interval ``[start, end]`` with an explicit role.  Both bounds are included, so two
    windows sharing an endpoint share an instant of evidence and are treated as overlapping."""

    window_id: str
    role: WindowRole
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _validate_window(self)


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    """One outcome the manifest commits to grading, bound to the window it must fall in."""

    observation_id: str
    window_id: str

    def __post_init__(self) -> None:
        _text(ProveFailure.INVALID_INPUT, "observation_id", self.observation_id)
        _text(ProveFailure.INVALID_INPUT, "window_id", self.window_id)


@dataclass(frozen=True, slots=True)
class OutcomeManifest:
    """The predeclared outcome set, bound to one exact proposal identity and timestamped.

    ``proposal_identity`` is the canonical digest produced by this module's :func:`proposal_identity`
    — not the proposal's own overridable ``checksum()`` — so a manifest cannot be re-pointed at a
    different proposal after the fact.
    """

    manifest_id: str
    declared_at: datetime
    proposal_id: str
    proposal_identity: str
    expectations: tuple[ExpectedOutcome, ...]

    def __post_init__(self) -> None:
        reason = ProveFailure.INVALID_INPUT
        _text(reason, "manifest_id", self.manifest_id)
        _instant(reason, "declared_at", self.declared_at)
        _text(reason, "proposal_id", self.proposal_id)
        _text(reason, "proposal_identity", self.proposal_identity)
        _items(reason, "expectations", self.expectations, ExpectedOutcome)


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    """One realised, point-in-time outcome.

    * ``outcome_time`` — the instant the outcome belongs to; it must lie inside its closed window.
    * ``available_time`` — when the outcome first became knowable; it must not follow ``as_of``.

    ``gross_return``, ``cost``, ``delay`` and ``abstained`` are each stated explicitly; none is
    inferred from another.  An abstention is a complete declared outcome that contributes no graded
    return and no cost, so it must declare zero for both.
    """

    observation_id: str
    window_id: str
    outcome_time: datetime
    available_time: datetime
    gross_return: float
    cost: float
    delay: timedelta
    abstained: bool = False

    def __post_init__(self) -> None:
        _validate_observation(self)


@dataclass(frozen=True, slots=True)
class ProveInputs:
    """Every explicit argument the evaluation consumed, kept together so a result can regrade
    itself.  Construction runs the full binding, so a malformed set cannot be represented."""

    proposal: BrainProposal
    windows: tuple[EvaluationWindow, ...]
    manifest: OutcomeManifest
    observations: tuple[OutcomeObservation, ...]
    as_of: datetime
    embargo: timedelta

    def __post_init__(self) -> None:
        _bind(self)


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """Counts and returns for one evaluation window; every invariant is re-proved on construction."""

    window_id: str
    expected_count: int
    graded_count: int
    abstention_count: int
    gross_return: float
    costs: float
    net_return: float
    total_delay: timedelta

    def __post_init__(self) -> None:
        _validate_metrics(self)


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    """The same accounting across every evaluation window, reconciled with the per-window rows."""

    expected_count: int
    graded_count: int
    abstention_count: int
    gross_return: float
    costs: float
    net_return: float
    total_delay: timedelta

    def __post_init__(self) -> None:
        _validate_metrics(self)


@dataclass(frozen=True, slots=True)
class ProveResult:
    """A proven audit record, or a fail-closed refusal.  The two states are mutually exclusive.

    A proven result carries the accepted inputs, so it can rebind and regrade them and require the
    stored metrics to match exactly.  A failed result carries no inputs and no metrics at all, so a
    rejected boundary can never leave partial evidence behind.  ``proposal_identity`` and
    ``input_identity`` are derived, never stored: they cannot be supplied by a caller.

    The protocol metadata is held to the same standard as the numbers.  ``calibration`` must be this
    module's own pinned label, so an arbitrary one cannot be recorded beside otherwise valid
    metrics, and a refusal must name exactly one stable reason — never several, never the same one
    twice.  Rewriting either afterwards fails the same way, because :meth:`checksum` re-proves the
    whole record before it signs anything.
    """

    proven: bool
    reasons: tuple[ProveFailure, ...]
    inputs: ProveInputs | None
    windows: tuple[WindowMetrics, ...]
    aggregate: AggregateMetrics | None
    calibration: str

    def __post_init__(self) -> None:
        self._revalidate()

    def _revalidate(self) -> tuple | None:
        """Re-prove the whole record.  Construction alone proves nothing: a frozen dataclass is
        still mutable through ``object.__setattr__``, so every audit-consuming method runs this."""
        reason = ProveFailure.INCONSISTENT_METRICS
        _flag(reason, "proven", self.proven)
        reasons = _items(reason, "reasons", self.reasons, ProveFailure)
        # The label is spelled as a literal here as well as in ``evaluate_prove``: a shared module
        # attribute would be rebindable by any importer, which would move what is recorded and what
        # is checked together and let a rewritten protocol label pass as this module's own.
        if _text(reason, "calibration", self.calibration) != "PROVE_WALK_FORWARD_V1":
            raise _ProveError(reason, "calibration must be this module's own PROVE calibration")
        if self.proven != (not reasons):
            raise _ProveError(reason, "a result is proven exactly when it carries no reason")
        windows = _items(reason, "windows", self.windows, WindowMetrics)
        if not self.proven:
            if len(reasons) != 1:
                raise _ProveError(reason, "a refusal reports exactly one stable reason")
            if self.inputs is not None or windows or self.aggregate is not None:
                raise _ProveError(reason, "a failed result carries no inputs and no metrics")
            return None
        if type(self.aggregate) is not AggregateMetrics:
            raise _ProveError(reason, "a proven result must carry aggregate metrics")
        for metrics in windows:
            _validate_metrics(metrics)
        _validate_metrics(self.aggregate)
        bound = _bind(self.inputs)
        expected_windows, expected_aggregate = _grade(bound)
        if windows != expected_windows or self.aggregate != expected_aggregate:
            raise _ProveError(reason, "metrics do not reconcile with the bound inputs")
        return bound, expected_windows, expected_aggregate

    @property
    def proposal_identity(self) -> str | None:
        """Derived from the accepted proposal itself, so it cannot be forged or re-pointed."""
        if self.inputs is None:
            return None
        return proposal_identity(self.inputs.proposal)

    @property
    def input_identity(self) -> str | None:
        """Derived from the canonical form of every accepted input, never caller-supplied."""
        if self.inputs is None:
            return None
        return _bind(self.inputs).input_identity

    def checksum(self) -> str:
        computed = self._revalidate()
        payload: dict = {"schema": "PROVE_RESULT_V1", "calibration": self.calibration,
                         "proven": self.proven,
                         "reasons": [failure.value for failure in self.reasons]}
        if computed is not None:
            bound, windows, aggregate = computed
            payload["proposal_identity"] = bound.proposal_identity
            payload["input_identity"] = bound.input_identity
            payload["windows"] = [_metrics_payload(metrics) for metrics in windows]
            payload["aggregate"] = _metrics_payload(aggregate)
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class _Bound:
    """Internal only: the canonical, fully validated view of one accepted input set."""

    proposal_identity: str
    windows: tuple[EvaluationWindow, ...]
    evaluation_windows: tuple[EvaluationWindow, ...]
    observations: tuple[OutcomeObservation, ...]
    input_identity: str


# ------------------------------------------------------------------- canonical serialization
# One exhaustive encoder over the supported schema.  Every value is tagged with its type, so `1`,
# `"1"` and `True` cannot collide; nothing is rounded, collapsed to a token or `repr`-ed.

_MAX_DEPTH = 8

_ENUM_TYPES = (WindowRole, ProposalAction, Stance)

_FIELDS: dict[type, tuple[str, ...]] = {
    EvaluationWindow: ("window_id", "role", "start", "end"),
    ExpectedOutcome: ("observation_id", "window_id"),
    OutcomeObservation: ("observation_id", "window_id", "outcome_time", "available_time",
                         "gross_return", "cost", "delay", "abstained"),
    InvalidationCondition: ("condition_id", "claim_key", "trigger_stance"),
    Scenario: ("scenario_id", "thesis", "plausibility_score", "invalidation_conditions"),
}

# Fields whose tuple is a *semantic set*: the members carry the meaning and their order does not.
# A scenario's invalidation conditions must be distinct and are evaluated as a set, so listing the
# same two falsifiers in the other order must not move the proposal identity a manifest binds — and
# through it the input identity and the proof checksum.
_SET_FIELDS: dict[type, frozenset[str]] = {
    Scenario: frozenset({"invalidation_conditions"}),
}


def _dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest()


def _encode(value: object, depth: int = 0) -> object:
    """Encode one supported value losslessly, or fail closed.

    ``float.hex()`` keeps adjacent floats distinct, the ``timedelta`` triple keeps a one-microsecond
    difference distinct, and an ``int`` is written as exact decimal text.  Anything outside the
    schema — an opaque object, a list, a non-finite float, a structure nested deeper than the schema
    can be — is rejected rather than approximated.

    A tuple is order-bearing unless the field it fills is declared a semantic set in
    :data:`_SET_FIELDS`, in which case its members are sorted by canonical form and tagged ``set``,
    so an equivalent spelling encodes identically while a different membership still does not.
    """
    if depth > _MAX_DEPTH:
        raise _ProveError(ProveFailure.INVALID_INPUT,
                          "value nests deeper than the PROVE schema allows")
    kind = type(value)
    if kind is bool:
        return {"bool": value}
    if kind is int:
        return {"int": str(value)}
    if kind is str:
        return {"str": value}
    if kind is float:
        return {"float": _number(ProveFailure.INVALID_INPUT, "value", value).hex()}
    if kind is datetime:
        return {"datetime": _instant(ProveFailure.INVALID_INPUT, "value", value).isoformat()}
    if kind is timedelta:
        return {"timedelta": [value.days, value.seconds, value.microseconds]}
    if kind in _ENUM_TYPES:
        return {"enum": [kind.__name__, _text(ProveFailure.INVALID_INPUT, "enum", value.value)]}
    if kind is tuple:
        return {"tuple": [_encode(item, depth + 1) for item in value]}
    names = _FIELDS.get(kind)
    if names is None:
        raise _ProveError(ProveFailure.INVALID_INPUT,
                          f"{kind.__name__} is not part of the PROVE schema")
    unordered = _SET_FIELDS.get(kind, frozenset())
    fields: dict = {}
    for name in names:
        field = getattr(value, name)
        if name not in unordered:
            fields[name] = _encode(field, depth + 1)
            continue
        if type(field) is not tuple:
            raise _ProveError(ProveFailure.INVALID_INPUT, f"{name} must be a tuple")
        fields[name] = {"set": _encode_sorted(field, depth + 1)}
    return {kind.__name__: fields}


def _encode_sorted(items, depth: int = 0) -> list:
    """A semantic set: sorted by canonical form, so the caller's iteration order binds nothing."""
    return sorted((_encode(item, depth + 1) for item in items), key=_dumps)


def _metrics_payload(metrics) -> dict:
    payload = {
        "expected_count": metrics.expected_count,
        "graded_count": metrics.graded_count,
        "abstention_count": metrics.abstention_count,
        "gross_return": metrics.gross_return.hex(),
        "costs": metrics.costs.hex(),
        "net_return": metrics.net_return.hex(),
        "total_delay": [metrics.total_delay.days, metrics.total_delay.seconds,
                        metrics.total_delay.microseconds],
    }
    if type(metrics) is WindowMetrics:
        payload["window_id"] = metrics.window_id
    return payload


# ------------------------------------------------------------------------------- validation

def _validate_window(window: EvaluationWindow) -> tuple[datetime, datetime]:
    reason = ProveFailure.INVALID_WINDOW
    _text(reason, "window_id", window.window_id)
    if type(window.role) is not WindowRole:
        raise _ProveError(reason, "role must be an explicit WindowRole")
    start = _instant(reason, "start", window.start)
    end = _instant(reason, "end", window.end)
    if start >= end:
        raise _ProveError(reason, "a window must start strictly before it ends")
    return start, end


def _validate_observation(observation: OutcomeObservation) -> tuple[datetime, datetime]:
    reason = ProveFailure.INVALID_INPUT
    _text(reason, "observation_id", observation.observation_id)
    _text(reason, "window_id", observation.window_id)
    outcome = _instant(reason, "outcome_time", observation.outcome_time)
    available = _instant(reason, "available_time", observation.available_time)
    if available < outcome:
        raise _ProveError(ProveFailure.OUTCOME_TIMESTAMPS_OUT_OF_ORDER,
                          "available_time cannot precede outcome_time")
    gross = _number(reason, "gross_return", observation.gross_return)
    cost = _number(reason, "cost", observation.cost)
    if cost < 0.0:
        raise _ProveError(reason, "cost must be non-negative")
    _span(reason, "delay", observation.delay)
    if _flag(reason, "abstained", observation.abstained) and (gross != 0.0 or cost != 0.0):
        raise _ProveError(ProveFailure.INCONSISTENT_ABSTENTION,
                          "an abstention contributes no graded return and no cost")
    return outcome, available


def _validate_scenario(scenario: Scenario) -> None:
    """Re-prove one scenario's own constructor invariants: a correct type proves nothing about
    content once ``object.__setattr__`` is available."""
    reason = ProveFailure.INVALID_PROPOSAL
    _text(reason, "scenario_id", scenario.scenario_id)
    _text(reason, "thesis", scenario.thesis)
    _unit(reason, "plausibility_score", scenario.plausibility_score)
    conditions = _items(reason, "invalidation_conditions", scenario.invalidation_conditions,
                        InvalidationCondition)
    if not conditions:
        raise _ProveError(reason, "a scenario needs at least one invalidation condition")
    for condition in conditions:
        _text(reason, "condition_id", condition.condition_id)
        _text(reason, "claim_key", condition.claim_key)
        if type(condition.trigger_stance) is not Stance:
            raise _ProveError(reason, "trigger_stance must be an explicit Stance")
    identifiers = [condition.condition_id for condition in conditions]
    if len(set(identifiers)) != len(identifiers):
        raise _ProveError(reason, "invalidation condition ids must be distinct")


def _validate_metrics(metrics) -> None:
    reason = ProveFailure.INCONSISTENT_METRICS
    if type(metrics) is WindowMetrics:
        _text(reason, "window_id", metrics.window_id)
    elif type(metrics) is not AggregateMetrics:
        raise _ProveError(reason, "metrics must be WindowMetrics or AggregateMetrics")
    expected = _count(reason, "expected_count", metrics.expected_count)
    graded = _count(reason, "graded_count", metrics.graded_count)
    abstentions = _count(reason, "abstention_count", metrics.abstention_count)
    if graded + abstentions != expected:
        raise _ProveError(reason, "graded and abstained outcomes must exhaust the expected set")
    gross = _number(reason, "gross_return", metrics.gross_return)
    costs = _number(reason, "costs", metrics.costs)
    net = _number(reason, "net_return", metrics.net_return)
    if costs < 0.0:
        raise _ProveError(reason, "costs cannot be negative")
    if net != gross - costs:
        raise _ProveError(reason, "net return must equal gross return minus costs")
    _span(reason, "total_delay", metrics.total_delay)
    if graded == 0 and (gross != 0.0 or costs != 0.0 or metrics.total_delay != timedelta(0)):
        raise _ProveError(reason, "an ungraded set cannot carry return, cost or delay")


def proposal_identity(proposal: BrainProposal) -> str:
    """Canonical identity of an *exact* ``BrainProposal``, derived without calling its ``checksum``.

    ``type(proposal) is BrainProposal`` is checked before any attribute is read, so a subclass is
    refused before an overridden ``__getattribute__``, property or method can execute and change
    what the audit record binds.  Every field is then revalidated, so a proposal tampered with
    through ``object.__setattr__`` either fails closed or visibly changes this digest.

    Required evidence, scenarios and each scenario's invalidation conditions are semantic sets, so
    they are canonically ordered first: two spellings of the same proposal share one identity.
    """
    reason = ProveFailure.INVALID_PROPOSAL
    if type(proposal) is not BrainProposal:
        raise _ProveError(reason, "an exact BrainProposal is required")
    if type(proposal.action) is not ProposalAction:
        raise _ProveError(reason, "action must be an explicit ProposalAction")
    evidence = _items(reason, "required_evidence", proposal.required_evidence, str)
    for item in evidence:
        _text(reason, "required_evidence", item)
    if len(set(evidence)) != len(evidence):
        raise _ProveError(reason, "required_evidence cannot repeat an id")
    scenarios = _items(reason, "scenarios", proposal.scenarios, Scenario)
    for scenario in scenarios:
        _validate_scenario(scenario)
    payload = {
        "schema": "PROVE_PROPOSAL_V1",
        "proposal_id": _text(reason, "proposal_id", proposal.proposal_id),
        "created_at": _instant(reason, "created_at", proposal.created_at).isoformat(),
        "action": proposal.action.value,
        "thesis": _text(reason, "thesis", proposal.thesis),
        "uncertainty": _text(reason, "uncertainty", proposal.uncertainty),
        "constitution_version": _text(reason, "constitution_version",
                                      proposal.constitution_version),
        "required_evidence": sorted(evidence),
        "scenarios": _encode_sorted(scenarios),
    }
    return _digest(payload)


def _bind(inputs: ProveInputs) -> _Bound:
    """Prove every input invariant and return the canonical view, or fail closed with one reason.

    The precedence below is fixed, so the reported reason never depends on check order: proposal,
    eligibility, explicit scalars, window structure, schedule, prior declaration, manifest,
    observations.
    """
    if type(inputs) is not ProveInputs:
        raise _ProveError(ProveFailure.INVALID_INPUT, "an exact ProveInputs is required")
    identity = proposal_identity(inputs.proposal)
    if inputs.proposal.action not in (ProposalAction.STUDY, ProposalAction.SHADOW):
        raise _ProveError(ProveFailure.INELIGIBLE_ACTION,
                          "only STUDY and SHADOW proposals are eligible for research evaluation")
    as_of = _instant(ProveFailure.INVALID_INPUT, "as_of", inputs.as_of)
    embargo = _span(ProveFailure.INVALID_INPUT, "embargo", inputs.embargo)

    windows = _items(ProveFailure.INVALID_WINDOW, "windows", inputs.windows, EvaluationWindow)
    if not windows:
        raise _ProveError(ProveFailure.INVALID_WINDOW, "at least one window is required")
    identifiers = [window.window_id for window in windows]
    if len(set(identifiers)) != len(identifiers):
        raise _ProveError(ProveFailure.DUPLICATE_WINDOW_ID, "window ids must be distinct")
    bounds = {window.window_id: _validate_window(window) for window in windows}
    ordered = tuple(sorted(windows, key=lambda window: (bounds[window.window_id][0],
                                                        bounds[window.window_id][1],
                                                        window.window_id)))
    for previous, current in zip(ordered, ordered[1:]):
        if bounds[current.window_id][0] <= bounds[previous.window_id][1]:
            raise _ProveError(ProveFailure.WINDOW_OVERLAP,
                              "closed windows must be strictly separated, never abutting")

    evaluations = tuple(w for w in ordered if w.role is WindowRole.EVALUATION)
    trainings = tuple(w for w in ordered if w.role is WindowRole.TRAINING)
    if not evaluations:
        raise _ProveError(ProveFailure.MISSING_EVALUATION_WINDOW,
                          "at least one EVALUATION window is required")
    for window in evaluations:
        start = bounds[window.window_id][0]
        # Rolling folds: pair each evaluation with the nearest training fold that ends before it,
        # never with every training fold, so train-1/eval-1/train-2/eval-2 is a valid schedule.
        fold = None
        for training in trainings:
            if bounds[training.window_id][1] < start:
                fold = training
        if fold is None:
            raise _ProveError(ProveFailure.MISSING_TRAINING_FOLD,
                              "every evaluation needs a preceding training fold")
        if start - bounds[fold.window_id][1] < embargo:
            raise _ProveError(ProveFailure.INSUFFICIENT_EMBARGO,
                              "the configured embargo must separate every training/evaluation pair")

    first_start = bounds[evaluations[0].window_id][0]
    created = _instant(ProveFailure.INVALID_PROPOSAL, "created_at", inputs.proposal.created_at)
    if created >= first_start:
        raise _ProveError(ProveFailure.PROPOSAL_NOT_PRIOR,
                          "the proposal must exist strictly before every evaluation start")

    manifest = inputs.manifest
    if type(manifest) is not OutcomeManifest:
        raise _ProveError(ProveFailure.INVALID_INPUT, "an exact OutcomeManifest is required")
    _text(ProveFailure.INVALID_INPUT, "manifest_id", manifest.manifest_id)
    declared = _instant(ProveFailure.INVALID_INPUT, "declared_at", manifest.declared_at)
    if (_text(ProveFailure.INVALID_INPUT, "proposal_id", manifest.proposal_id)
            != inputs.proposal.proposal_id
            or _text(ProveFailure.INVALID_INPUT, "proposal_identity", manifest.proposal_identity)
            != identity):
        raise _ProveError(ProveFailure.MANIFEST_PROPOSAL_MISMATCH,
                          "the manifest must bind the exact proposal being evaluated")
    expectations = _items(ProveFailure.INVALID_INPUT, "expectations", manifest.expectations,
                          ExpectedOutcome)
    if not expectations:
        raise _ProveError(ProveFailure.EMPTY_MANIFEST,
                          "an empty outcome manifest proves nothing")
    expected_ids = [item.observation_id for item in expectations]
    if len(set(expected_ids)) != len(expected_ids):
        raise _ProveError(ProveFailure.DUPLICATE_EXPECTED_OUTCOME,
                          "an outcome cannot be declared twice")
    evaluation_ids = {window.window_id for window in evaluations}
    declared_windows = set()
    expected = {}
    for item in expectations:
        _text(ProveFailure.INVALID_INPUT, "observation_id", item.observation_id)
        window_id = _text(ProveFailure.INVALID_INPUT, "window_id", item.window_id)
        if window_id not in evaluation_ids:
            raise _ProveError(ProveFailure.UNKNOWN_WINDOW_REFERENCE,
                              "every expected outcome must name a declared evaluation window")
        declared_windows.add(window_id)
        expected[item.observation_id] = window_id
    if declared_windows != evaluation_ids:
        raise _ProveError(ProveFailure.UNDECLARED_EVALUATION_WINDOW,
                          "the manifest must name every evaluation window in the schedule")
    if declared >= first_start:
        raise _ProveError(ProveFailure.MANIFEST_NOT_PRIOR,
                          "the manifest must be declared strictly before every evaluation start")

    observations = _items(ProveFailure.INVALID_INPUT, "observations", inputs.observations,
                          OutcomeObservation)
    supplied: dict[str, OutcomeObservation] = {}
    for observation in observations:
        _validate_observation(observation)
        if observation.observation_id in supplied:
            raise _ProveError(ProveFailure.DUPLICATE_OUTCOME_ID,
                              "an outcome cannot be observed twice")
        supplied[observation.observation_id] = observation
    for observation_id in expected:
        if observation_id not in supplied:
            raise _ProveError(ProveFailure.MISSING_OUTCOME,
                              "every declared outcome must be observed")
    for observation_id in supplied:
        if observation_id not in expected:
            raise _ProveError(ProveFailure.UNDECLARED_OUTCOME,
                              "an undeclared outcome cannot be graded")
    order = {window.window_id: index for index, window in enumerate(ordered)}
    canonical = tuple(sorted(observations,
                             key=lambda item: (order[expected[item.observation_id]],
                                               item.observation_id)))
    for observation in canonical:
        window_id = expected[observation.observation_id]
        if observation.window_id != window_id:
            raise _ProveError(ProveFailure.OUTCOME_WINDOW_MISMATCH,
                              "an outcome must be graded in the window it was declared for")
        outcome, available = _validate_observation(observation)
        start, end = bounds[window_id]
        if not start <= outcome <= end:
            raise _ProveError(ProveFailure.OUTCOME_OUTSIDE_WINDOW,
                              "an outcome must fall inside its closed evaluation window")
        if available > as_of:
            raise _ProveError(ProveFailure.OUTCOME_NOT_AVAILABLE_AT_AS_OF,
                              "an outcome must be knowable at the explicit as-of instant")

    payload = {
        "schema": "PROVE_INPUT_V1",
        "proposal": identity,
        "as_of": as_of.isoformat(),
        "embargo": [embargo.days, embargo.seconds, embargo.microseconds],
        "windows": [_encode(window) for window in ordered],
        "manifest": {
            "manifest_id": manifest.manifest_id,
            "declared_at": declared.isoformat(),
            "proposal_id": manifest.proposal_id,
            "proposal_identity": manifest.proposal_identity,
            "expectations": _encode_sorted(expectations),
        },
        "observations": [_encode(observation) for observation in canonical],
    }
    return _Bound(identity, ordered, evaluations, canonical, _digest(payload))


def _grade(bound: _Bound) -> tuple[tuple[WindowMetrics, ...], AggregateMetrics]:
    """Compute metrics from the complete canonical observation set only.

    Iteration follows canonical window and observation order, so the float additions happen in the
    same sequence for every caller permutation and the totals are bit-identical.
    """
    reason = ProveFailure.INCONSISTENT_METRICS
    rows: list[WindowMetrics] = []
    for window in bound.evaluation_windows:
        expected = graded = 0
        gross = costs = 0.0
        delay = timedelta(0)
        for observation in bound.observations:
            if observation.window_id != window.window_id:
                continue
            expected += 1
            if observation.abstained:
                continue
            graded += 1
            gross += observation.gross_return
            costs += observation.cost
            delay += observation.delay
        _number(reason, "gross_return", gross)
        _number(reason, "costs", costs)
        rows.append(WindowMetrics(window.window_id, expected, graded, expected - graded, gross,
                                  costs, gross - costs, delay))
    total_expected = total_graded = 0
    total_gross = total_costs = 0.0
    total_delay = timedelta(0)
    for metrics in rows:
        total_expected += metrics.expected_count
        total_graded += metrics.graded_count
        total_gross += metrics.gross_return
        total_costs += metrics.costs
        total_delay += metrics.total_delay
    _number(reason, "gross_return", total_gross)
    _number(reason, "costs", total_costs)
    aggregate = AggregateMetrics(total_expected, total_graded, total_expected - total_graded,
                                 total_gross, total_costs, total_gross - total_costs, total_delay)
    return tuple(rows), aggregate


def evaluate_prove(proposal: BrainProposal, *, windows: tuple[EvaluationWindow, ...],
                   manifest: OutcomeManifest, observations: tuple[OutcomeObservation, ...],
                   as_of: datetime, embargo: timedelta) -> ProveResult:
    """Grade `proposal` against its predeclared, complete, embargoed outcome record.

    Pure and repeatable: no clock, no I/O, no provider, no rebindable module calibration, no shared
    state and no side effect.  Only immutable tuples of exact schema types are accepted, so a result
    can never depend on prior iterator consumption.

    Any refusal — a malformed or hostile explicit input, an overlapping or abutting schedule, an
    insufficient embargo, a retrospective or incomplete manifest, an outcome outside its window —
    returns a deterministic failed `ProveResult` carrying exactly one reason, no inputs and no
    metrics.  Ordinary exceptions raised while validating or binding untrusted input are caught here
    rather than escaping.
    """
    # A literal on purpose: a module attribute is rebindable by any importer, which would silently
    # change the recorded calibration and every checksum for identical explicit inputs.
    # `ProveResult._revalidate` spells the same literal, so the recorded and the required label
    # cannot be moved together.
    calibration = "PROVE_WALK_FORWARD_V1"
    try:
        inputs = ProveInputs(proposal, windows, manifest, observations, as_of, embargo)
        rows, aggregate = _grade(_bind(inputs))
        return ProveResult(True, (), inputs, rows, aggregate, calibration)
    except _ProveError as exc:
        return ProveResult(False, (exc.reason,), None, (), None, calibration)
    except Exception:  # hostile explicit input fails closed; it never escapes the boundary
        return ProveResult(False, (ProveFailure.INVALID_INPUT,), None, (), None, calibration)
