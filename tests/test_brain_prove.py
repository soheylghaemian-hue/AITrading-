"""PROVE regression tests.

Every instant here is historical and explicit: PROVE answers from its own `as_of`, never the wall
clock.  Nothing in this module reloads, deletes or otherwise mutates a shared `atp` module, so the
rest of the suite keeps observing exactly one `atp.governance.versioning.ModelStatus` class.
"""
import ast
import math
import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from atp.brain import (AggregateMetrics, BrainProposal, EvaluationWindow, ExpectedOutcome,
                       InvalidationCondition, OutcomeManifest, OutcomeObservation, ProposalAction,
                       ProveFailure, ProveInputs, ProveResult, Scenario, Stance, WindowMetrics,
                       WindowRole, evaluate_prove, proposal_identity)
from atp.brain import prove as prove_module

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
T = datetime(2021, 1, 4, tzinfo=UTC)

# A rolling walk-forward schedule: train-1 / eval-1 / train-2 / eval-2, each pair separated by the
# configured embargo and no two closed windows touching.
TRAIN1 = EvaluationWindow("train-1", WindowRole.TRAINING, T, T + 10 * DAY)
EVAL1 = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 13 * DAY, T + 17 * DAY)
TRAIN2 = EvaluationWindow("train-2", WindowRole.TRAINING, T + 20 * DAY, T + 30 * DAY)
EVAL2 = EvaluationWindow("eval-2", WindowRole.EVALUATION, T + 33 * DAY, T + 37 * DAY)

EMBARGO = 3 * DAY
CREATED = T - DAY
DECLARED = T + DAY
AS_OF = T + 40 * DAY

# An explicit sentinel: `None` is a *malformed value* these tests must be able to send through the
# public boundary, so it can never double as "use the helper default".
MISSING = object()


def _proposal(action=ProposalAction.STUDY, created_at=CREATED, proposal_id="p-1",
              thesis="drift persists after the print"):
    condition = InvalidationCondition("c1", "CPI_PRINT>CONSENSUS", Stance.REFUTES)
    scenario = Scenario("s1", "the drift decays within a session", 0.4, (condition,))
    return BrainProposal(proposal_id, created_at, action, thesis, (scenario,), ("e1", "e2"),
                         "sample is small")


def _outcome(observation_id, window, offset_days, gross, cost, *, delay=HOUR, abstained=False):
    at = window.start + offset_days * DAY
    return OutcomeObservation(observation_id, window.window_id, at, at + delay, gross, cost, delay,
                              abstained)


def _plan(train1=TRAIN1, eval1=EVAL1, train2=TRAIN2, eval2=EVAL2):
    """Windows and observations built together, so every observation always references a window
    this fixture actually declares."""
    windows = (train1, eval1, train2, eval2)
    observations = (
        _outcome("o1", eval1, 0, 0.02, 0.005),
        _outcome("o2", eval1, 1, 0.0, 0.0, abstained=True),
        _outcome("o3", eval2, 0, -0.01, 0.004),
        _outcome("o4", eval2, 2, 0.03, 0.001),
    )
    return windows, observations


def _manifest(observations, proposal, declared_at=DECLARED, manifest_id="m-1", proposal_id=MISSING,
              identity=MISSING, expectations=MISSING):
    if expectations is MISSING:
        expectations = tuple(ExpectedOutcome(item.observation_id, item.window_id)
                             for item in observations)
    return OutcomeManifest(manifest_id, declared_at,
                           proposal.proposal_id if proposal_id is MISSING else proposal_id,
                           proposal_identity(proposal) if identity is MISSING else identity,
                           expectations)


def _prove(*, proposal=MISSING, windows=MISSING, manifest=MISSING, observations=MISSING,
           as_of=MISSING, embargo=MISSING):
    plan_windows, plan_observations = _plan()
    base = _proposal()
    return evaluate_prove(
        base if proposal is MISSING else proposal,
        windows=plan_windows if windows is MISSING else windows,
        manifest=_manifest(plan_observations, base) if manifest is MISSING else manifest,
        observations=plan_observations if observations is MISSING else observations,
        as_of=AS_OF if as_of is MISSING else as_of,
        embargo=EMBARGO if embargo is MISSING else embargo)


def _prove_for(proposal, **overrides):
    """Evaluate a caller-supplied proposal with a manifest bound to that exact proposal."""
    plan_windows, plan_observations = _plan()
    return evaluate_prove(proposal, windows=plan_windows,
                          manifest=_manifest(plan_observations, proposal),
                          observations=plan_observations, as_of=AS_OF, embargo=EMBARGO,
                          **overrides)


def _prove_schedule(**window_overrides):
    """Evaluate a re-shaped schedule whose manifest and observations stay consistent with it."""
    windows, observations = _plan(**window_overrides)
    proposal = _proposal()
    return evaluate_prove(proposal, windows=windows, manifest=_manifest(observations, proposal),
                          observations=observations, as_of=AS_OF, embargo=EMBARGO)


def _assert_failed(result, reason):
    assert result.proven is False
    assert result.reasons == (reason,)
    assert result.inputs is None
    assert result.windows == ()
    assert result.aggregate is None
    assert result.proposal_identity is None
    assert result.input_identity is None
    assert result.calibration == "PROVE_WALK_FORWARD_V1"
    assert result.checksum() == result.checksum()


# ------------------------------------------------------------------ schedule and eligibility

def test_a_rolling_multi_fold_schedule_evaluates_deterministically():
    result = _prove()
    assert result.proven is True and result.reasons == ()
    assert [metrics.window_id for metrics in result.windows] == ["eval-1", "eval-2"]
    assert result.aggregate.expected_count == 4
    assert result.aggregate.graded_count == 3
    assert result.aggregate.abstention_count == 1
    assert result.calibration == "PROVE_WALK_FORWARD_V1"
    # A single train/eval pair is equally valid; the harness is not limited to one shape.
    windows = (TRAIN1, EVAL1)
    _, observations = _plan()
    proposal = _proposal()
    single = evaluate_prove(proposal, windows=windows,
                            manifest=_manifest(observations[:2], proposal),
                            observations=observations[:2], as_of=AS_OF, embargo=EMBARGO)
    assert single.proven is True
    assert [metrics.window_id for metrics in single.windows] == ["eval-1"]


def test_overlapping_abutting_and_underembargoed_schedules_fail_with_stable_reasons():
    overlapping = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 9 * DAY, T + 13 * DAY)
    abutting = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 10 * DAY, T + 14 * DAY)
    tight = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 11 * DAY, T + 15 * DAY)
    _assert_failed(_prove_schedule(eval1=overlapping), ProveFailure.WINDOW_OVERLAP)
    _assert_failed(_prove_schedule(eval1=abutting), ProveFailure.WINDOW_OVERLAP)
    _assert_failed(_prove_schedule(eval1=tight), ProveFailure.INSUFFICIENT_EMBARGO)
    # An embargo of exactly the configured span is sufficient: the boundary is inclusive.
    exact = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 13 * DAY, T + 17 * DAY)
    assert _prove_schedule(eval1=exact).proven is True
    _assert_failed(_prove(embargo=10 * DAY), ProveFailure.INSUFFICIENT_EMBARGO)


def test_windows_must_be_closed_intervals_with_unique_ids_and_aware_bounds():
    with pytest.raises(ValueError):
        EvaluationWindow("w", WindowRole.TRAINING, T, T)
    with pytest.raises(ValueError):
        EvaluationWindow("w", WindowRole.TRAINING, T + DAY, T)
    with pytest.raises(ValueError):
        EvaluationWindow("w", WindowRole.TRAINING, T.replace(tzinfo=None), T + DAY)
    with pytest.raises(ValueError):
        EvaluationWindow("", WindowRole.TRAINING, T, T + DAY)
    duplicate = EvaluationWindow("eval-1", WindowRole.TRAINING, T + 50 * DAY, T + 51 * DAY)
    windows, observations = _plan()
    proposal = _proposal()
    _assert_failed(evaluate_prove(proposal, windows=windows + (duplicate,),
                                  manifest=_manifest(observations, proposal),
                                  observations=observations, as_of=AS_OF, embargo=EMBARGO),
                   ProveFailure.DUPLICATE_WINDOW_ID)


def test_a_schedule_without_a_matching_training_fold_cannot_be_proven():
    proposal = _proposal()
    _assert_failed(evaluate_prove(proposal, windows=(TRAIN1, TRAIN2),
                                  manifest=_manifest((), proposal, expectations=()),
                                  observations=(), as_of=AS_OF, embargo=EMBARGO),
                   ProveFailure.MISSING_EVALUATION_WINDOW)
    orphan = EvaluationWindow("eval-1", WindowRole.EVALUATION, T - 10 * DAY, T - 6 * DAY)
    _assert_failed(_prove_schedule(eval1=orphan), ProveFailure.MISSING_TRAINING_FOLD)


def test_only_research_actions_created_strictly_before_an_evaluation_are_eligible():
    for action in (ProposalAction.ABSTAIN, ProposalAction.REJECT):
        _assert_failed(_prove_for(_proposal(action=action)), ProveFailure.INELIGIBLE_ACTION)
    assert _prove_for(_proposal(action=ProposalAction.SHADOW)).proven is True
    assert _prove_for(_proposal(action=ProposalAction.STUDY)).proven is True
    # Exactly at the evaluation start is not "before" it.
    _assert_failed(_prove_for(_proposal(created_at=EVAL1.start)), ProveFailure.PROPOSAL_NOT_PRIOR)
    _assert_failed(_prove_for(_proposal(created_at=EVAL1.start + timedelta(microseconds=1))),
                   ProveFailure.PROPOSAL_NOT_PRIOR)
    assert _prove_for(_proposal(created_at=EVAL1.start - timedelta(microseconds=1))).proven is True


# ------------------------------------------------------------------------ manifest and outcomes

def test_the_manifest_must_be_complete_prior_and_bound_to_the_exact_proposal():
    windows, observations = _plan()
    proposal = _proposal()
    other = _proposal(proposal_id="p-2", thesis="an unrelated thesis")

    def prove(manifest, items=observations):
        return evaluate_prove(proposal, windows=windows, manifest=manifest, observations=items,
                              as_of=AS_OF, embargo=EMBARGO)

    _assert_failed(prove(_manifest((), proposal, expectations=()), ()),
                   ProveFailure.EMPTY_MANIFEST)
    _assert_failed(prove(_manifest(observations, proposal, declared_at=EVAL1.start)),
                   ProveFailure.MANIFEST_NOT_PRIOR)
    _assert_failed(prove(_manifest(observations, proposal, proposal_id="p-2")),
                   ProveFailure.MANIFEST_PROPOSAL_MISMATCH)
    _assert_failed(prove(_manifest(observations, other)),
                   ProveFailure.MANIFEST_PROPOSAL_MISMATCH)
    _assert_failed(prove(_manifest(observations, proposal, identity="sha256:forged")),
                   ProveFailure.MANIFEST_PROPOSAL_MISMATCH)

    expectations = tuple(ExpectedOutcome(item.observation_id, item.window_id)
                         for item in observations)
    _assert_failed(prove(_manifest(observations, proposal, expectations=expectations[:2])),
                   ProveFailure.UNDECLARED_EVALUATION_WINDOW)
    _assert_failed(prove(_manifest(observations, proposal,
                                   expectations=expectations + expectations[:1])),
                   ProveFailure.DUPLICATE_EXPECTED_OUTCOME)
    _assert_failed(prove(_manifest(observations, proposal,
                                   expectations=expectations + (ExpectedOutcome("o5", "ghost"),))),
                   ProveFailure.UNKNOWN_WINDOW_REFERENCE)
    _assert_failed(prove(_manifest(observations, proposal,
                                   expectations=expectations + (ExpectedOutcome("o5", "train-1"),))),
                   ProveFailure.UNKNOWN_WINDOW_REFERENCE)


def test_cherry_picked_missing_extra_and_duplicate_outcome_sets_cannot_be_proven():
    windows, observations = _plan()
    proposal = _proposal()
    manifest = _manifest(observations, proposal)

    def prove(items):
        return evaluate_prove(proposal, windows=windows, manifest=manifest, observations=items,
                              as_of=AS_OF, embargo=EMBARGO)

    losing = observations[2]
    assert losing.gross_return < 0.0
    # Dropping the loss after the fact leaves a declared outcome unobserved.
    _assert_failed(prove(observations[:2] + observations[3:]), ProveFailure.MISSING_OUTCOME)
    _assert_failed(prove(()), ProveFailure.MISSING_OUTCOME)
    extra = _outcome("o5", EVAL2, 1, 0.5, 0.0)
    _assert_failed(prove(observations + (extra,)), ProveFailure.UNDECLARED_OUTCOME)
    _assert_failed(prove(observations + observations[:1]), ProveFailure.DUPLICATE_OUTCOME_ID)
    misplaced = OutcomeObservation("o1", "eval-2", EVAL2.start, EVAL2.start + HOUR, 0.02, 0.005,
                                   HOUR)
    _assert_failed(prove(observations[1:] + (misplaced,)), ProveFailure.OUTCOME_WINDOW_MISMATCH)


def test_outcomes_must_be_inside_their_window_and_knowable_at_the_as_of_instant():
    windows, observations = _plan()
    proposal = _proposal()
    manifest = _manifest(observations, proposal)

    def prove(items, as_of=AS_OF):
        return evaluate_prove(proposal, windows=windows, manifest=manifest, observations=items,
                              as_of=as_of, embargo=EMBARGO)

    outside = _outcome("o1", EVAL1, 10, 0.02, 0.005)
    _assert_failed(prove((outside,) + observations[1:]), ProveFailure.OUTCOME_OUTSIDE_WINDOW)
    # A closed interval includes both bounds.
    on_the_end = OutcomeObservation("o1", "eval-1", EVAL1.end, EVAL1.end + HOUR, 0.02, 0.005, HOUR)
    assert prove((on_the_end,) + observations[1:]).proven is True
    _assert_failed(prove(observations, as_of=EVAL2.start), ProveFailure.OUTCOME_NOT_AVAILABLE_AT_AS_OF)
    with pytest.raises(ValueError):
        OutcomeObservation("o1", "eval-1", EVAL1.start, EVAL1.start - HOUR, 0.0, 0.0, HOUR)
    with pytest.raises(ValueError):
        OutcomeObservation("o1", "eval-1", EVAL1.start, EVAL1.start, 0.0, -0.001, HOUR)
    with pytest.raises(ValueError):
        OutcomeObservation("o1", "eval-1", EVAL1.start, EVAL1.start, 0.0, 0.0, -HOUR)


def test_costs_delays_grades_and_abstentions_reconcile_exactly():
    result = _prove()
    first, second = result.windows
    assert (first.expected_count, first.graded_count, first.abstention_count) == (2, 1, 1)
    assert (second.expected_count, second.graded_count, second.abstention_count) == (2, 2, 0)
    for metrics in (first, second, result.aggregate):
        assert metrics.graded_count + metrics.abstention_count == metrics.expected_count
        assert metrics.net_return == metrics.gross_return - metrics.costs
        assert metrics.costs >= 0.0
        assert metrics.total_delay >= timedelta(0)
    assert result.aggregate.expected_count == first.expected_count + second.expected_count
    assert result.aggregate.graded_count == first.graded_count + second.graded_count
    assert result.aggregate.abstention_count == (first.abstention_count
                                                 + second.abstention_count)
    assert result.aggregate.gross_return == first.gross_return + second.gross_return
    assert result.aggregate.costs == first.costs + second.costs
    assert result.aggregate.total_delay == first.total_delay + second.total_delay
    # The abstention is a complete declared outcome that contributes no return, cost or delay.
    assert first.gross_return == pytest.approx(0.02)
    assert first.costs == pytest.approx(0.005)
    assert first.total_delay == HOUR
    with pytest.raises(ValueError):
        OutcomeObservation("a", "eval-1", EVAL1.start, EVAL1.start, 0.02, 0.0, HOUR, True)
    with pytest.raises(ValueError):
        OutcomeObservation("a", "eval-1", EVAL1.start, EVAL1.start, 0.0, 0.001, HOUR, True)


# ------------------------------------------------------------------- forged audit evidence

def test_forged_window_and_aggregate_metrics_fail_closed():
    with pytest.raises(ValueError):
        WindowMetrics("eval-1", 2, 2, 1, 0.02, 0.005, 0.015, HOUR)
    with pytest.raises(ValueError):
        WindowMetrics("eval-1", 2, 1, 1, 0.02, 0.005, 999.0, HOUR)
    with pytest.raises(ValueError):
        WindowMetrics("eval-1", 2, 1, 1, 0.02, -0.005, 0.025, HOUR)
    with pytest.raises(ValueError):
        WindowMetrics("", 2, 1, 1, 0.02, 0.005, 0.015, HOUR)
    with pytest.raises(ValueError):
        WindowMetrics("eval-1", 1, 0, 1, 0.02, 0.0, 0.02, timedelta(0))
    with pytest.raises(ValueError):
        WindowMetrics("eval-1", 2, 1, 1, float("nan"), 0.0, float("nan"), HOUR)
    with pytest.raises(ValueError):
        AggregateMetrics(4, 3, 2, 0.04, 0.01, 0.03, HOUR)
    with pytest.raises(ValueError):
        AggregateMetrics(4, 3, 1, 0.04, 0.01, 999.0, HOUR)


def test_forged_prove_results_fail_closed_and_identities_stay_derived():
    good = _prove()
    forged_window = WindowMetrics("eval-1", 2, 1, 1, 999.0, 0.0, 999.0, HOUR)
    forged_aggregate = AggregateMetrics(4, 3, 1, 999.0, 0.0, 999.0, HOUR)
    with pytest.raises(ValueError):
        ProveResult(True, (), good.inputs, (forged_window, good.windows[1]), good.aggregate,
                    good.calibration)
    with pytest.raises(ValueError):
        ProveResult(True, (), good.inputs, good.windows, forged_aggregate, good.calibration)
    with pytest.raises(ValueError):
        ProveResult(True, (), good.inputs, (), None, good.calibration)
    with pytest.raises(ValueError):
        ProveResult(True, (ProveFailure.INVALID_INPUT,), good.inputs, good.windows, good.aggregate,
                    good.calibration)
    with pytest.raises(ValueError):
        ProveResult(False, (ProveFailure.INVALID_INPUT,), good.inputs, good.windows,
                    good.aggregate, good.calibration)
    with pytest.raises(ValueError):
        ProveResult(False, (), None, (), None, good.calibration)
    with pytest.raises(ValueError):
        ProveResult(True, (), None, good.windows, good.aggregate, good.calibration)

    # A checksum re-proves the record: tampering after construction cannot be signed.
    tampered = _prove()
    object.__setattr__(tampered.windows[0], "gross_return", 999.0)
    with pytest.raises(ValueError):
        tampered.checksum()

    # Identity is derived from the accepted inputs, never a caller-supplied digest.
    assert good.proposal_identity == proposal_identity(good.inputs.proposal)
    assert good.input_identity == _prove().input_identity
    windows, observations = _plan()
    other = _proposal(proposal_id="p-2", thesis="an unrelated thesis")
    other_inputs = ProveInputs(other, windows, _manifest(observations, other), observations, AS_OF,
                               EMBARGO)
    rebound = ProveResult(True, (), other_inputs, good.windows, good.aggregate, good.calibration)
    assert rebound.proposal_identity != good.proposal_identity
    assert rebound.input_identity != good.input_identity
    assert rebound.checksum() != good.checksum()


def test_forged_protocol_metadata_fails_closed():
    good = _prove()
    assert good.calibration == "PROVE_WALK_FORWARD_V1"
    # The calibration label is pinned to this module's own protocol; it is not caller-supplied text.
    for label in ("TAMPERED", "PROVE_WALK_FORWARD_V2", "prove_walk_forward_v1", ""):
        with pytest.raises(ValueError):
            ProveResult(True, (), good.inputs, good.windows, good.aggregate, label)
        with pytest.raises(ValueError):
            ProveResult(False, (ProveFailure.INVALID_INPUT,), None, (), None, label)
    with pytest.raises(ValueError):
        ProveResult(True, (), good.inputs, good.windows, good.aggregate, None)

    # A refusal reports exactly one stable reason: never several, never the same one twice.
    with pytest.raises(ValueError):
        ProveResult(False, (ProveFailure.INVALID_INPUT, ProveFailure.INVALID_WINDOW), None, (),
                    None, good.calibration)
    with pytest.raises(ValueError):
        ProveResult(False, (ProveFailure.INVALID_INPUT, ProveFailure.INVALID_INPUT), None, (),
                    None, good.calibration)

    # Rewriting either after construction cannot be signed: `checksum()` re-proves the record.
    relabelled = _prove()
    object.__setattr__(relabelled, "calibration", "TAMPERED")
    with pytest.raises(ValueError):
        relabelled.checksum()
    padded = _prove(embargo=10 * DAY)
    assert padded.reasons == (ProveFailure.INSUFFICIENT_EMBARGO,)
    object.__setattr__(padded, "reasons", (ProveFailure.INSUFFICIENT_EMBARGO,
                                           ProveFailure.INVALID_INPUT))
    with pytest.raises(ValueError):
        padded.checksum()


def test_a_proposal_subclass_is_rejected_before_its_behaviour_runs():
    touched: list[str] = []

    class _Overriding(BrainProposal):
        def __getattribute__(self, name):
            touched.append(name)
            return super().__getattribute__(name)

        def checksum(self) -> str:
            touched.append("checksum")
            return "sha256:forged"

    subclass = _Overriding("p-1", CREATED, ProposalAction.STUDY, "thesis", (), ("e1",), "unknown")
    touched.clear()
    _assert_failed(_prove(proposal=subclass), ProveFailure.INVALID_PROPOSAL)
    assert touched == []
    with pytest.raises(ValueError):
        proposal_identity(subclass)
    assert touched == []


def test_a_tampered_exact_proposal_is_revalidated_before_use():
    proposal = _proposal()
    assert _prove_for(proposal).proven is True
    windows, observations = _plan()
    manifest = _manifest(observations, proposal)
    # Still an exact BrainProposal, but no longer the one the manifest was bound to.
    object.__setattr__(proposal, "thesis", "a quietly rewritten thesis")
    _assert_failed(evaluate_prove(proposal, windows=windows, manifest=manifest,
                                  observations=observations, as_of=AS_OF, embargo=EMBARGO),
                   ProveFailure.MANIFEST_PROPOSAL_MISMATCH)
    object.__setattr__(proposal, "thesis", "")
    _assert_failed(evaluate_prove(proposal, windows=windows, manifest=manifest,
                                  observations=observations, as_of=AS_OF, embargo=EMBARGO),
                   ProveFailure.INVALID_PROPOSAL)
    broken = _proposal()
    object.__setattr__(broken, "action", "STUDY")
    _assert_failed(_prove(proposal=broken), ProveFailure.INVALID_PROPOSAL)
    ambiguous = _proposal()
    object.__setattr__(ambiguous, "created_at", CREATED.replace(tzinfo=None))
    _assert_failed(_prove(proposal=ambiguous), ProveFailure.INVALID_PROPOSAL)


# ------------------------------------------------------------------------ hostile explicit input

class _HostileZone(tzinfo):
    """A well-typed tzinfo that refuses to say what it means."""

    def utcoffset(self, dt):
        raise RuntimeError("this offset is unknowable")

    def tzname(self, dt):
        return "HOSTILE"

    def dst(self, dt):
        return None


def test_malformed_and_hostile_explicit_inputs_fail_closed_without_escaping():
    windows, observations = _plan()
    _assert_failed(_prove(proposal=None), ProveFailure.INVALID_PROPOSAL)
    _assert_failed(_prove(proposal=object()), ProveFailure.INVALID_PROPOSAL)
    _assert_failed(_prove(manifest=None), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(manifest=object()), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(windows=None), ProveFailure.INVALID_WINDOW)
    _assert_failed(_prove(windows=list(windows)), ProveFailure.INVALID_WINDOW)
    _assert_failed(_prove(windows=()), ProveFailure.INVALID_WINDOW)
    _assert_failed(_prove(windows=windows + ("not a window",)), ProveFailure.INVALID_WINDOW)
    _assert_failed(_prove(observations=list(observations)), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(observations=None), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(as_of=None), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(as_of=AS_OF.replace(tzinfo=None)), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(embargo=3600), ProveFailure.INVALID_INPUT)
    _assert_failed(_prove(embargo=-DAY), ProveFailure.INVALID_INPUT)

    # A tzinfo whose utcoffset raises must become a deterministic refusal, not a leaked RuntimeError.
    hostile = datetime(2021, 2, 20, tzinfo=_HostileZone())
    _assert_failed(_prove(as_of=hostile), ProveFailure.INVALID_INPUT)

    # Enormous and non-finite numbers smuggled past the constructors.
    for value in (10 ** 5000, float("inf"), float("nan"), 1, "0.02"):
        tampered = _outcome("o1", EVAL1, 0, 0.02, 0.005)
        object.__setattr__(tampered, "gross_return", value)
        _assert_failed(_prove(observations=(tampered,) + observations[1:]),
                       ProveFailure.INVALID_INPUT)
    huge = _outcome("o1", EVAL1, 0, 0.02, 0.005)
    object.__setattr__(huge, "delay", 10 ** 5000)
    _assert_failed(_prove(observations=(huge,) + observations[1:]), ProveFailure.INVALID_INPUT)


def test_unsupported_and_deeply_nested_values_are_rejected_not_collapsed():
    _, observations = _plan()
    opaque = _outcome("o1", EVAL1, 0, 0.02, 0.005)
    object.__setattr__(opaque, "window_id", object())
    _assert_failed(_prove(observations=(opaque,) + observations[1:]), ProveFailure.INVALID_INPUT)
    nested = _proposal()
    deep = "leaf"
    for _ in range(20):
        deep = (deep,)
    object.__setattr__(nested, "uncertainty", deep)
    _assert_failed(_prove(proposal=nested), ProveFailure.INVALID_PROPOSAL)
    # The canonical serializer itself refuses unsupported and over-nested values outright.
    for value in (object(), [1, 2], {"a": 1}, {1, 2}, float("inf"), deep):
        with pytest.raises(ValueError):
            prove_module._encode(value)
    assert prove_module._encode(1) != prove_module._encode("1")
    assert prove_module._encode(1) != prove_module._encode(True)
    # A semantic-set field is still required to be a tuple, never any other iterable.
    loose = Scenario("s1", "a thesis", 0.4,
                     (InvalidationCondition("c1", "CPI_PRINT>CONSENSUS", Stance.REFUTES),))
    object.__setattr__(loose, "invalidation_conditions",
                       [InvalidationCondition("c1", "CPI_PRINT>CONSENSUS", Stance.REFUTES)])
    with pytest.raises(ValueError):
        prove_module._encode(loose)


# ---------------------------------------------------------------------- determinism and identity

def _shift_window(window, zone):
    return EvaluationWindow(window.window_id, window.role, window.start.astimezone(zone),
                            window.end.astimezone(zone))


def _shift_outcome(observation, zone):
    return OutcomeObservation(observation.observation_id, observation.window_id,
                              observation.outcome_time.astimezone(zone),
                              observation.available_time.astimezone(zone),
                              observation.gross_return, observation.cost, observation.delay,
                              observation.abstained)


def test_equivalent_permutations_and_timezone_spellings_produce_identical_proofs():
    windows, observations = _plan()
    proposal = _proposal()
    manifest = _manifest(observations, proposal)
    forward = evaluate_prove(proposal, windows=windows, manifest=manifest,
                             observations=observations, as_of=AS_OF, embargo=EMBARGO)
    reversed_expectations = tuple(reversed(manifest.expectations))
    reverse = evaluate_prove(proposal, windows=tuple(reversed(windows)),
                             manifest=OutcomeManifest(manifest.manifest_id, manifest.declared_at,
                                                      manifest.proposal_id,
                                                      manifest.proposal_identity,
                                                      reversed_expectations),
                             observations=tuple(reversed(observations)), as_of=AS_OF,
                             embargo=EMBARGO)
    assert forward.proven and reverse.proven
    assert forward.checksum() == reverse.checksum()
    assert forward.input_identity == reverse.input_identity
    assert forward.windows == reverse.windows
    assert forward.aggregate == reverse.aggregate

    for zone in (timezone(timedelta(hours=-7)), ZoneInfo("America/New_York")):
        shifted_proposal = BrainProposal(proposal.proposal_id, CREATED.astimezone(zone),
                                         proposal.action, proposal.thesis, proposal.scenarios,
                                         proposal.required_evidence, proposal.uncertainty)
        shifted_windows = tuple(_shift_window(window, zone) for window in windows)
        shifted_observations = tuple(_shift_outcome(item, zone) for item in observations)
        shifted = evaluate_prove(
            shifted_proposal, windows=shifted_windows,
            manifest=_manifest(shifted_observations, shifted_proposal,
                               declared_at=DECLARED.astimezone(zone)),
            observations=shifted_observations, as_of=AS_OF.astimezone(zone), embargo=EMBARGO)
        assert shifted.proven is True
        assert shifted.proposal_identity == forward.proposal_identity
        assert shifted.input_identity == forward.input_identity
        assert shifted.checksum() == forward.checksum()


C1 = InvalidationCondition("c1", "CPI_PRINT>CONSENSUS", Stance.REFUTES)
C2 = InvalidationCondition("c2", "DRIFT_HALF_LIFE<1D", Stance.SUPPORTS)


def _falsifier_proposal(conditions):
    """One proposal whose only variable is how its invalidation-condition set is spelled."""
    scenario = Scenario("s1", "the drift decays within a session", 0.4, conditions)
    return BrainProposal("p-1", CREATED, ProposalAction.STUDY, "drift persists after the print",
                         (scenario,), ("e1", "e2"), "sample is small")


def test_permuting_a_semantic_condition_set_does_not_change_proof_identity():
    forward = _falsifier_proposal((C1, C2))
    reverse = _falsifier_proposal((C2, C1))
    assert (forward.scenarios[0].invalidation_conditions
            != reverse.scenarios[0].invalidation_conditions)
    # Conditions are a set: the same two falsifiers in the other order are the same proposal.
    assert proposal_identity(forward) == proposal_identity(reverse)

    windows, observations = _plan()
    # The manifest is bound to the forward spelling, so the reversed one must still satisfy it.
    manifest = _manifest(observations, forward)
    proofs = [evaluate_prove(proposal, windows=windows, manifest=manifest,
                             observations=observations, as_of=AS_OF, embargo=EMBARGO)
              for proposal in (forward, reverse)]
    assert all(proof.proven is True for proof in proofs)
    assert proofs[0].proposal_identity == proofs[1].proposal_identity
    assert proofs[0].input_identity == proofs[1].input_identity
    assert proofs[0].checksum() == proofs[1].checksum()

    # A different membership is still a different proposal, and the manifest no longer binds it.
    changed = _falsifier_proposal(
        (C1, InvalidationCondition("c2", "DRIFT_HALF_LIFE<2D", Stance.SUPPORTS)))
    assert proposal_identity(changed) != proposal_identity(forward)
    _assert_failed(evaluate_prove(changed, windows=windows, manifest=manifest,
                                  observations=observations, as_of=AS_OF, embargo=EMBARGO),
                   ProveFailure.MANIFEST_PROPOSAL_MISMATCH)
    flipped = _falsifier_proposal((C1, InvalidationCondition("c2", "DRIFT_HALF_LIFE<1D",
                                                             Stance.REFUTES)))
    assert proposal_identity(flipped) != proposal_identity(forward)


def test_adjacent_and_one_microsecond_apart_inputs_stay_distinguishable():
    _, observations = _plan()
    base = _prove()

    def variant(observation):
        return _prove(observations=(observation,) + observations[1:])

    original = observations[0]
    nudged = OutcomeObservation(original.observation_id, original.window_id,
                                original.outcome_time, original.available_time,
                                math.nextafter(original.gross_return, 1.0), original.cost,
                                original.delay, original.abstained)
    slower = OutcomeObservation(original.observation_id, original.window_id,
                                original.outcome_time, original.available_time,
                                original.gross_return, original.cost,
                                original.delay + timedelta(microseconds=1), original.abstained)
    dearer = OutcomeObservation(original.observation_id, original.window_id,
                                original.outcome_time, original.available_time,
                                original.gross_return, math.nextafter(original.cost, 1.0),
                                original.delay, original.abstained)
    later = OutcomeObservation(original.observation_id, original.window_id,
                               original.outcome_time + timedelta(microseconds=1),
                               original.available_time, original.gross_return, original.cost,
                               original.delay, original.abstained)
    identities = {base.input_identity}
    checksums = {base.checksum()}
    for observation in (nudged, slower, dearer, later):
        result = variant(observation)
        assert result.proven is True
        identities.add(result.input_identity)
        checksums.add(result.checksum())
    assert len(identities) == 5
    assert len(checksums) == 5
    # A one-microsecond embargo difference is a different question, and a different proof.
    assert _prove(embargo=EMBARGO - timedelta(microseconds=1)).input_identity != base.input_identity


def test_evaluation_is_repeatable_and_iterator_history_cannot_change_a_proof():
    windows, observations = _plan()
    first = _prove()
    second = _prove()
    assert first == second
    assert first.checksum() == second.checksum()
    assert first.input_identity == second.input_identity

    # A generator is refused outright, so a result can never depend on how far it was consumed.
    stream = (item for item in observations)
    initial = _prove(observations=stream)
    repeat = _prove(observations=stream)
    _assert_failed(initial, ProveFailure.INVALID_INPUT)
    _assert_failed(repeat, ProveFailure.INVALID_INPUT)
    assert initial.checksum() == repeat.checksum()
    assert _prove().checksum() == first.checksum()
    # The evaluator mutated none of its arguments.
    assert windows == _plan()[0]
    assert observations == _plan()[1]


def test_rebinding_module_attributes_cannot_change_a_proof():
    before = _prove()
    tampered = {"CALIBRATION": "TAMPERED", "_CALIBRATION": "TAMPERED",
                "CALIBRATION_VERSION": "TAMPERED", "_EMBARGO": timedelta(0),
                "COSTS": 0.0, "_COST_MODEL": {}}
    for name, value in tampered.items():
        setattr(prove_module, name, value)
    try:
        after = _prove()
        assert after.checksum() == before.checksum()
        assert after.calibration == before.calibration == "PROVE_WALK_FORWARD_V1"
        assert after.aggregate == before.aggregate
    finally:
        for name in tampered:
            delattr(prove_module, name)


# --------------------------------------------------------------------------- fixture integrity

def test_regression_fixtures_exercise_the_intended_paths():
    # `None` really reaches the public boundary; the helper sentinel is not None.
    assert MISSING is not None
    assert _prove().proven is True
    # The abutting fixture's observations reference windows that fixture actually declares, so the
    # failure is the non-overlap boundary rather than an unknown window reference.
    abutting = EvaluationWindow("eval-1", WindowRole.EVALUATION, T + 10 * DAY, T + 14 * DAY)
    windows, observations = _plan(eval1=abutting)
    declared = {window.window_id for window in windows}
    assert {item.window_id for item in observations} <= declared
    assert abutting.start == TRAIN1.end
    for item in observations:
        window = next(w for w in windows if w.window_id == item.window_id)
        assert window.start <= item.outcome_time <= window.end
    _assert_failed(_prove_schedule(eval1=abutting), ProveFailure.WINDOW_OVERLAP)
    # An invalid role is constructed with complete, valid bounds, so the role is what fails.
    with pytest.raises(ValueError):
        EvaluationWindow("w", "TRAINING", T, T + DAY)
    with pytest.raises(ValueError):
        EvaluationWindow("w", None, T, T + DAY)
    # The condition-permutation fixture really varies order and nothing else.
    assert C1 != C2
    assert (_falsifier_proposal((C1, C2)).scenarios[0].invalidation_conditions
            == (C1, C2) != (C2, C1))


# ------------------------------------------------------------------------------ import safety

ALLOWED_IMPORT_ROOTS = {"__future__", "dataclasses", "datetime", "enum", "hashlib", "json",
                        "math", "."}
RISKY_CALLS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "random", "open",
               "eval", "exec", "compile", "__import__", "connect", "request", "urlopen", "run",
               "Popen"}


def _import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add("." if node.level else (node.module or "").split(".")[0])
    return roots


def _risky_calls(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in RISKY_CALLS:
                found.add(name)
    return found


def test_prove_import_safety_is_executable_not_documentation_prose():
    tree = ast.parse(Path(prove_module.__file__).read_text(encoding="utf-8"))
    assert _import_roots(tree) <= ALLOWED_IMPORT_ROOTS
    assert _risky_calls(tree) == set()
    # Prose is not executable safety evidence: it neither condemns nor exonerates a module.
    benign = ('"""randomness, execution, a broker, a clock and trading."""\n'
              "from __future__ import annotations\n")
    benign_tree = ast.parse(benign)
    assert _import_roots(benign_tree) == {"__future__"}
    assert _import_roots(benign_tree) <= ALLOWED_IMPORT_ROOTS
    assert _risky_calls(benign_tree) == set()
    risky_tree = ast.parse("import random\nrandom.random()\n")
    assert not _import_roots(risky_tree) <= ALLOWED_IMPORT_ROOTS
    assert _risky_calls(risky_tree) == {"random"}


def test_collecting_prove_tests_leaves_shared_governance_modules_intact():
    from atp.governance import versioning
    from atp.governance.versioning import ModelRegistry, ModelStatus, ModelVersion

    assert sys.modules["atp.governance.versioning"] is versioning
    assert ModelStatus is versioning.ModelStatus
    assert ModelStatus("research") is ModelStatus.RESEARCH
    registry = ModelRegistry()
    version = ModelVersion("drift", "v1")
    registry.set_baseline(version)
    assert version.status is ModelStatus.RESEARCH
    assert registry.by_status(ModelStatus.RESEARCH) == [version]
    assert registry.by_status(ModelStatus.LIVE) == []
