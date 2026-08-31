"""LEARN regression tests: drift, comparison, retirement and value integrity.

Every case builds its own fixtures, so no test can leave a mutated record behind for another.
"""
from __future__ import annotations

import copy
import math
import pickle
import types
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from atp.brain import (
    Assertion,
    BrainProposal,
    ComparisonFailure,
    ComparisonPreference,
    ComparisonResult,
    ContradictionGroup,
    DriftFailure,
    DriftInputs,
    DriftResult,
    EvaluationWindow,
    Evidence,
    EvidenceQuality,
    ExpectedOutcome,
    InvalidationCondition,
    ModelRecord,
    ModelRole,
    OutcomeManifest,
    OutcomeObservation,
    ProposalAction,
    ReinstatementResult,
    RetirementGround,
    RetirementResult,
    Scenario,
    SenseResult,
    Stance,
    ThinkFailure,
    TransitionFailure,
    evaluate_comparison,
    evaluate_drift,
    evaluate_prove,
    evaluate_reinstatement,
    evaluate_retirement,
    evaluate_sense,
    evaluate_think,
    proposal_identity,
)
from atp.brain import learn as learn_module

CLAIM = "REGIME/SPX_TREND"
LIMIT = timedelta(days=30)
DOC = Path(__file__).resolve().parents[1] / "docs" / "TRADER_BRAIN.md"


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 3, day, hour, tzinfo=UTC)


AS_OF = _at(10)
PROOF_AS_OF = _at(6)


class _ExplodingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("hostile timezone")

    def dst(self, _value):
        return timedelta(0)

    def tzname(self, _value):
        return "EXPLODING"


def _evidence(evidence_id: str, stance: Stance, *, day: int = 9,
              quality: EvidenceQuality = EvidenceQuality.VERIFIED,
              claim_key: str = CLAIM) -> Evidence:
    moment = _at(day)
    return Evidence(evidence_id, "filing", moment, moment, moment, quality,
                    "sha256:" + evidence_id, (Assertion(claim_key, stance),))


def _spelled(item: Evidence, tz) -> Evidence:
    """The very same instants, spelled in another timezone."""
    return Evidence(item.evidence_id, item.source, item.event_time.astimezone(tz),
                    item.available_time.astimezone(tz), item.observed_time.astimezone(tz),
                    item.quality, item.checksum, item.assertions)


def _sense(items=(), *, as_of: datetime = AS_OF, limit: timedelta = LIMIT) -> SenseResult:
    return evaluate_sense(items, as_of=as_of, freshness_limit=limit)


def _model(model_id: str = "m-champion", *, proposal_id: str = "p-champion",
           role: ModelRole = ModelRole.CHAMPION, day: int = 1) -> ModelRecord:
    return ModelRecord(model_id, proposal_id, role, _at(day))


def _proposal(proposal_id: str) -> BrainProposal:
    condition = InvalidationCondition("c-1", CLAIM, Stance.REFUTES)
    scenario = Scenario("s-1", "the regime persists", 0.5, (condition,))
    return BrainProposal(proposal_id, _at(1), ProposalAction.STUDY, "study the regime",
                         (scenario,), ("e-required",), "wide")


def _proof(proposal_id: str, *, net: float = 0.5, as_of: datetime = PROOF_AS_OF,
           reverse_windows: bool = False):
    proposal = _proposal(proposal_id)
    windows = (EvaluationWindow("train-1", learn_window_role(), _at(2), _at(3)),
               EvaluationWindow("eval-1", evaluation_role(), _at(4), _at(5)))
    if reverse_windows:
        windows = tuple(reversed(windows))
    manifest = OutcomeManifest("man-" + proposal_id, _at(1, 1), proposal_id,
                               proposal_identity(proposal), (ExpectedOutcome("o-1", "eval-1"),))
    observations = (OutcomeObservation("o-1", "eval-1", _at(4, 1), _at(4, 2), net, 0.0,
                                       timedelta(0)),)
    return evaluate_prove(proposal, windows=windows, manifest=manifest, observations=observations,
                          as_of=as_of, embargo=timedelta(0))


def _refused_proof(proposal_id: str):
    return evaluate_prove(
        _proposal(proposal_id),
        windows=(),
        manifest=OutcomeManifest(
            "man-" + proposal_id,
            _at(1),
            proposal_id,
            "sha256:none",
            (ExpectedOutcome("o-1", "eval-1"),),
        ),
        observations=(),
        as_of=PROOF_AS_OF,
        embargo=timedelta(0),
    )


def learn_window_role():
    from atp.brain import WindowRole

    return WindowRole.TRAINING


def evaluation_role():
    from atp.brain import WindowRole

    return WindowRole.EVALUATION


def _drift(*, stance: Stance = Stance.REFUTES, prior: float = 0.9, threshold: float = 0.5,
           model: ModelRecord | None = None, as_of: datetime = AS_OF) -> DriftResult:
    evidence = _sense((_evidence("e-1", stance), _evidence("e-2", stance, day=8)), as_of=as_of)
    return evaluate_drift(model or _model(), evidence, claim_key=CLAIM, prior_confidence=prior,
                          abstention_threshold=threshold, as_of=as_of)


def _comparison(*, champion: ModelRecord | None = None, challenger: ModelRecord | None = None,
                champion_net: float = 0.1, challenger_net: float = 0.9,
                as_of: datetime = AS_OF) -> ComparisonResult:
    champion = champion or _model()
    challenger = challenger or _model("m-challenger", proposal_id="p-challenger",
                                      role=ModelRole.CHALLENGER)
    return evaluate_comparison(champion, challenger,
                               champion_proof=_proof(champion.proposal_id, net=champion_net),
                               challenger_proof=_proof(challenger.proposal_id,
                                                       net=challenger_net),
                               as_of=as_of)


def _retirement(*, model: ModelRecord | None = None, drift: DriftResult | None = None,
                comparison: ComparisonResult | None = None,
                retirement_id: str = "r-1") -> RetirementResult:
    model = model or _model()
    if drift is None and comparison is None:
        drift = _drift(model=model)
    return evaluate_retirement(model, retirement_id=retirement_id, as_of=_at(11), drift=drift,
                               comparison=comparison)


# ------------------------------------------------------------------------------------ drift

def test_fresh_refuting_evidence_lowers_confidence_and_forces_abstention():
    result = _drift(prior=0.9, threshold=0.5)
    assert result.accepted is True
    assert result.reasons == ()
    assert result.drift_score == 1.0
    assert result.posterior_confidence == 0.0
    assert result.posterior_confidence < 0.9
    assert result.abstain is True
    assert result.refuting_evidence_ids == ("e-1", "e-2")
    assert result.supporting_evidence_ids == ()


def test_supporting_evidence_preserves_confidence_without_abstention():
    result = _drift(stance=Stance.SUPPORTS, prior=0.8, threshold=0.5)
    assert result.accepted is True
    assert result.drift_score == 0.0
    assert result.posterior_confidence == pytest.approx(0.8)
    assert result.abstain is False


def test_mixed_evidence_lowers_confidence_proportionally():
    evidence = _sense((_evidence("e-up", Stance.SUPPORTS),
                       _evidence("e-down", Stance.REFUTES, day=8,
                                 quality=EvidenceQuality.OBSERVED_ONLY)))
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.8,
                            abstention_threshold=0.1, as_of=AS_OF)
    assert result.accepted is True
    assert result.drift_score == pytest.approx(0.6 / 1.6)
    assert result.posterior_confidence == pytest.approx(0.8 * (1.0 - 0.6 / 1.6))
    assert result.abstain is False


def test_empty_evidence_cannot_manufacture_drift_or_abstention():
    result = evaluate_drift(_model(), _sense(()), claim_key=CLAIM, prior_confidence=0.05,
                            abstention_threshold=0.9, as_of=AS_OF)
    assert result.accepted is True
    assert result.drift_score == 0.0
    assert result.posterior_confidence == pytest.approx(0.05)
    assert result.abstain is False


@pytest.mark.parametrize("prior", (0.1234567890126, 0.12345678901234))
def test_non_bearing_evidence_preserves_high_precision_prior_exactly(prior):
    empty = evaluate_drift(_model(), _sense(()), claim_key=CLAIM, prior_confidence=prior,
                           abstention_threshold=0.9, as_of=AS_OF)
    supporting = _drift(stance=Stance.SUPPORTS, prior=prior, threshold=0.9)
    assert empty.accepted is True
    assert supporting.accepted is True
    assert empty.posterior_confidence == prior
    assert supporting.posterior_confidence == prior


def test_drift_threshold_uses_the_documented_unrounded_formula():
    evidence = _sense((_evidence("e-support", Stance.SUPPORTS),
                       _evidence("e-refute", Stance.REFUTES,
                                 quality=EvidenceQuality.UNKNOWN)))
    threshold = 0.7692307692308846
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=1.0,
                            abstention_threshold=threshold, as_of=AS_OF)
    expected_score = 0.3 / 1.3
    assert result.accepted is True
    assert result.drift_score == expected_score
    assert result.posterior_confidence == 1.0 - expected_score
    assert result.abstain is True


def test_empty_evidence_cannot_justify_retirement():
    empty = evaluate_drift(_model(), _sense(()), claim_key=CLAIM, prior_confidence=0.05,
                           abstention_threshold=0.9, as_of=AS_OF)
    refusal = evaluate_retirement(_model(), retirement_id="r-empty", as_of=_at(11), drift=empty)
    assert refusal.accepted is False
    assert refusal.reasons == (TransitionFailure.INSUFFICIENT_EVIDENCE,)
    assert refusal.inputs is None
    assert refusal.grounds == ()


def test_evidence_on_another_claim_key_does_not_bear_on_drift():
    evidence = _sense((_evidence("e-other", Stance.REFUTES, claim_key="REGIME/OTHER"),))
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.4,
                            abstention_threshold=0.9, as_of=AS_OF)
    assert result.accepted is True
    assert result.abstain is False
    assert result.posterior_confidence == pytest.approx(0.4)


def test_future_registered_model_cannot_produce_actionable_drift():
    result = _drift(model=_model(day=11))
    assert result.accepted is False
    assert result.reasons == (DriftFailure.UNKNOWABLE_MODEL,)
    assert result.inputs is None


def test_drift_refuses_malformed_scalars_and_models():
    evidence = _sense((_evidence("e-1", Stance.REFUTES),))
    bad_prior = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=float("nan"),
                               abstention_threshold=0.5, as_of=AS_OF)
    assert bad_prior.reasons == (DriftFailure.INVALID_INPUT,)
    bad_model = evaluate_drift("not-a-model", evidence, claim_key=CLAIM, prior_confidence=0.5,
                               abstention_threshold=0.5, as_of=AS_OF)
    assert bad_model.reasons == (DriftFailure.INVALID_MODEL,)
    shell = object.__new__(ModelRecord)
    exact_shell = evaluate_drift(shell, evidence, claim_key=CLAIM, prior_confidence=0.5,
                                 abstention_threshold=0.5, as_of=AS_OF)
    assert exact_shell.reasons == (DriftFailure.INVALID_MODEL,)
    naive = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.5,
                           abstention_threshold=0.5, as_of=_at(10).replace(tzinfo=None))
    assert naive.reasons == (DriftFailure.INVALID_INPUT,)


def test_hostile_model_timezone_maps_to_each_evaluators_model_failure():
    model = _model()
    object.__setattr__(model, "registered_at", datetime(2024, 3, 1,
                                                        tzinfo=_ExplodingTimezone()))
    challenger = _model("m-challenger", proposal_id="p-challenger",
                        role=ModelRole.CHALLENGER)
    drift = evaluate_drift(model, _sense((_evidence("e-1", Stance.REFUTES),)),
                           claim_key=CLAIM, prior_confidence=0.5,
                           abstention_threshold=0.5, as_of=AS_OF)
    comparison = evaluate_comparison(model, challenger,
                                     champion_proof=_proof("p-champion"),
                                     challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    retirement = evaluate_retirement(model, retirement_id="r-1", as_of=_at(11))
    assert drift.reasons == (DriftFailure.INVALID_MODEL,)
    assert comparison.reasons == (ComparisonFailure.INVALID_MODEL,)
    assert retirement.reasons == (TransitionFailure.INVALID_MODEL,)


def test_hostile_explicit_as_of_remains_an_input_failure():
    as_of = datetime(2024, 3, 10, tzinfo=_ExplodingTimezone())
    result = evaluate_drift(_model(), _sense((_evidence("e-1", Stance.REFUTES),)),
                            claim_key=CLAIM, prior_confidence=0.5,
                            abstention_threshold=0.5, as_of=as_of)
    assert result.reasons == (DriftFailure.INVALID_INPUT,)


def test_drift_requires_the_admission_as_of_to_match():
    evidence = _sense((_evidence("e-1", Stance.REFUTES),), as_of=_at(10))
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.5,
                            abstention_threshold=0.5, as_of=_at(11))
    assert result.reasons == (DriftFailure.INVALID_SENSE_RESULT,)


# --------------------------------------------------------------- the SENSE boundary in LEARN

def test_canonical_direct_sense_result_is_admissible_to_think_and_learn():
    genuine = _sense((_evidence("e-1", Stance.REFUTES),))
    replica = SenseResult(genuine.as_of, genuine.freshness_limit, genuine.usable,
                          genuine.rejected, genuine.contradictions)
    assert replica.checksum() == genuine.checksum()
    admitted = evaluate_think((), replica)
    assert admitted.admitted is True
    assert admitted.reasons == ()
    original = evaluate_drift(_model(), genuine, claim_key=CLAIM, prior_confidence=0.5,
                              abstention_threshold=0.5, as_of=AS_OF)
    reconstructed = evaluate_drift(_model(), replica, claim_key=CLAIM, prior_confidence=0.5,
                                   abstention_threshold=0.5, as_of=AS_OF)
    assert reconstructed.accepted is True
    assert reconstructed == original
    assert reconstructed.checksum() == original.checksum()


@pytest.mark.parametrize("builder", [
    # A future item parked in `usable`.
    lambda: SenseResult(AS_OF, LIMIT, (_evidence("e-future", Stance.REFUTES, day=11),), (), ()),
    # A stale item parked in `usable`.
    lambda: SenseResult(AS_OF, timedelta(hours=1),
                        (_evidence("e-stale", Stance.REFUTES, day=1),), (), ()),
    # The same id admitted twice.
    lambda: SenseResult(AS_OF, LIMIT, (_evidence("e-dup", Stance.REFUTES),
                                       _evidence("e-dup", Stance.SUPPORTS)), (), ()),
    # A contradiction that the represented evidence does not support.
    lambda: SenseResult(AS_OF, LIMIT, (_evidence("e-1", Stance.REFUTES),), (),
                        (__import__("atp.brain", fromlist=["ContradictionGroup"])
                         .ContradictionGroup(CLAIM, ("e-1",), ("e-2",)),)),
])
def test_malformed_direct_sense_results_fail_closed_in_think_and_learn(builder):
    forged = builder()
    assert evaluate_think((), forged).reasons == (ThinkFailure.INVALID_SENSE_RESULT,)
    refused = evaluate_drift(_model(), forged, claim_key=CLAIM, prior_confidence=0.5,
                             abstention_threshold=0.5, as_of=AS_OF)
    assert refused.reasons == (DriftFailure.INVALID_SENSE_RESULT,)
    assert refused.inputs is None


def test_a_coherently_rebound_sense_value_is_revalidated_at_its_new_instant():
    evidence = _sense((_evidence("e-1", Stance.REFUTES),))
    object.__setattr__(evidence, "as_of", _at(11))
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.5,
                            abstention_threshold=0.5, as_of=_at(11))
    assert result.accepted is True
    assert result.reasons == ()


def test_noncanonical_nested_sense_mutation_is_refused():
    evidence = _sense((_evidence("e-1", Stance.REFUTES),))
    object.__setattr__(evidence.usable[0], "observed_time", _at(11))
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.5,
                            abstention_threshold=0.5, as_of=AS_OF)
    assert result.reasons == (DriftFailure.INVALID_SENSE_RESULT,)


def test_contradiction_group_fields_require_exact_canonical_shapes():
    evidence = _sense((_evidence("e-up", Stance.SUPPORTS),
                       _evidence("e-down", Stance.REFUTES)))
    assert type(evidence.contradictions[0]) is ContradictionGroup
    object.__setattr__(evidence.contradictions[0], "supporting_evidence_ids", ["e-up"])
    result = evaluate_drift(_model(), evidence, claim_key=CLAIM, prior_confidence=0.5,
                            abstention_threshold=0.5, as_of=AS_OF)
    assert result.reasons == (DriftFailure.INVALID_SENSE_RESULT,)


# ------------------------------------------------------------------- canonical determinism

def test_permuted_and_timezone_spelled_evidence_produce_equal_results_and_checksums():
    plus = timezone(timedelta(hours=5, minutes=30))
    minus = timezone(timedelta(hours=-4))
    first = _evidence("e-1", Stance.REFUTES)
    second = _evidence("e-2", Stance.SUPPORTS, day=8)
    assert first.event_time.utcoffset() != _spelled(first, plus).event_time.utcoffset()
    forward = _sense((first, second))
    reverse = _sense((_spelled(second, minus), _spelled(first, plus)))
    left = evaluate_drift(_model(), forward, claim_key=CLAIM, prior_confidence=0.7,
                          abstention_threshold=0.2, as_of=AS_OF)
    right = evaluate_drift(_model(), reverse, claim_key=CLAIM, prior_confidence=0.7,
                           abstention_threshold=0.2, as_of=AS_OF.astimezone(plus))
    assert left == right
    assert left.checksum() == right.checksum()


def test_permuted_assertions_do_not_move_the_result_or_the_checksum():
    moment = _at(9)
    keys = (Assertion(CLAIM, Stance.REFUTES), Assertion("REGIME/OTHER", Stance.SUPPORTS))
    forward = Evidence("e-1", "filing", moment, moment, moment, EvidenceQuality.VERIFIED,
                       "sha256:e-1", keys)
    reverse = Evidence("e-1", "filing", moment, moment, moment, EvidenceQuality.VERIFIED,
                       "sha256:e-1", tuple(reversed(keys)))
    left = evaluate_drift(_model(), _sense((forward,)), claim_key=CLAIM, prior_confidence=0.5,
                          abstention_threshold=0.1, as_of=AS_OF)
    right = evaluate_drift(_model(), _sense((reverse,)), claim_key=CLAIM, prior_confidence=0.5,
                           abstention_threshold=0.1, as_of=AS_OF)
    assert left == right
    assert left.checksum() == right.checksum()


def test_equal_signed_zero_priors_agree_in_both_equality_and_checksum():
    evidence_a = _sense((_evidence("e-1", Stance.REFUTES),))
    evidence_b = _sense((_evidence("e-1", Stance.REFUTES),))
    positive = evaluate_drift(_model(), evidence_a, claim_key=CLAIM, prior_confidence=0.0,
                              abstention_threshold=0.5, as_of=AS_OF)
    negative = evaluate_drift(_model(), evidence_b, claim_key=CLAIM, prior_confidence=-0.0,
                              abstention_threshold=0.5, as_of=AS_OF)
    assert positive == negative
    assert positive.checksum() == negative.checksum()


def test_adjacent_floats_and_distinct_identities_produce_distinct_bindings():
    base = evaluate_drift(_model(), _sense((_evidence("e-1", Stance.SUPPORTS),)), claim_key=CLAIM,
                          prior_confidence=0.5, abstention_threshold=0.5, as_of=AS_OF)
    nudged = evaluate_drift(_model(), _sense((_evidence("e-1", Stance.SUPPORTS),)),
                            claim_key=CLAIM, prior_confidence=math.nextafter(0.5, 1.0),
                            abstention_threshold=0.5, as_of=AS_OF)
    other_model = evaluate_drift(_model("m-other"), _sense((_evidence("e-1", Stance.SUPPORTS),)),
                                 claim_key=CLAIM, prior_confidence=0.5, abstention_threshold=0.5,
                                 as_of=AS_OF)
    assert base != nudged
    assert base.checksum() != nudged.checksum()
    assert base != other_model
    assert base.checksum() != other_model.checksum()


def test_replaying_drift_on_identical_inputs_is_deterministic():
    first = _drift()
    second = _drift()
    assert first == second
    assert first.checksum() == second.checksum()


# ------------------------------------------------------------------------------ comparison

def test_comparison_prefers_the_better_proof_without_promoting_anything():
    result = _comparison()
    assert result.accepted is True
    assert result.preference is ComparisonPreference.CHALLENGER
    assert result.inputs.challenger.role is ModelRole.CHALLENGER
    assert result.champion_proof.net_return == pytest.approx(0.1)
    assert result.challenger_proof.net_return == pytest.approx(0.9)


def test_comparison_reports_champion_and_inconclusive_preferences():
    champion_wins = _comparison(champion_net=0.9, challenger_net=0.1)
    assert champion_wins.preference is ComparisonPreference.CHAMPION
    tied = _comparison(champion_net=0.4, challenger_net=0.4)
    assert tied.preference is ComparisonPreference.INCONCLUSIVE


def test_comparison_refuses_invalid_input_and_invalid_model_shells():
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    naive = evaluate_comparison(_model(), challenger, champion_proof=_proof("p-champion"),
                                challenger_proof=_proof("p-challenger"),
                                as_of=_at(10).replace(tzinfo=None))
    assert naive.reasons == (ComparisonFailure.INVALID_INPUT,)
    wrong_type = evaluate_comparison("champion", challenger, champion_proof=_proof("p-champion"),
                                     challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    assert wrong_type.reasons == (ComparisonFailure.INVALID_MODEL,)
    shell = object.__new__(ModelRecord)
    exact_shell = evaluate_comparison(_model(), shell, champion_proof=_proof("p-champion"),
                                      challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    assert exact_shell.reasons == (ComparisonFailure.INVALID_MODEL,)
    assert exact_shell.inputs is None
    assert exact_shell.preference is None


@pytest.mark.parametrize("bad_side", ["champion", "challenger"])
def test_comparison_refuses_invalid_proofs_on_both_sides(bad_side):
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    champion_proof = "proof" if bad_side == "champion" else _proof("p-champion")
    challenger_proof = "proof" if bad_side == "challenger" else _proof("p-challenger")
    not_a_proof = evaluate_comparison(_model(), challenger, champion_proof=champion_proof,
                                      challenger_proof=challenger_proof, as_of=AS_OF)
    assert not_a_proof.reasons == (ComparisonFailure.INVALID_PROOF,)


def test_comparison_refuses_tampered_proof_before_using_it():
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    tampered = _proof("p-champion")
    object.__setattr__(tampered, "calibration", "FORGED")
    assert evaluate_comparison(_model(), challenger, champion_proof=tampered,
                               challenger_proof=_proof("p-challenger"),
                               as_of=AS_OF).reasons == (ComparisonFailure.INVALID_PROOF,)


@pytest.mark.parametrize("refused_side", ["champion", "challenger"])
def test_comparison_refuses_unproven_proofs_on_both_sides(refused_side):
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    champion_proof = (_refused_proof("p-champion") if refused_side == "champion"
                      else _proof("p-champion"))
    challenger_proof = (_refused_proof("p-challenger") if refused_side == "challenger"
                        else _proof("p-challenger"))
    refusal = champion_proof if refused_side == "champion" else challenger_proof
    assert refusal.proven is False
    assert evaluate_comparison(_model(), challenger, champion_proof=champion_proof,
                               challenger_proof=challenger_proof,
                               as_of=AS_OF).reasons == (ComparisonFailure.PROOF_NOT_PROVEN,)


def test_invalid_proof_precedes_refusal_symmetrically_when_both_sides_fail():
    champion = _model()
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    refused_champion = _refused_proof("p-champion")
    refused_challenger = _refused_proof("p-challenger")
    left = evaluate_comparison(champion, challenger, champion_proof=refused_champion,
                               challenger_proof="bad", as_of=AS_OF)
    right = evaluate_comparison(champion, challenger, champion_proof="bad",
                                challenger_proof=refused_challenger, as_of=AS_OF)
    assert left.reasons == right.reasons == (ComparisonFailure.INVALID_PROOF,)
    both_refused = evaluate_comparison(champion, challenger,
                                       champion_proof=refused_champion,
                                       challenger_proof=refused_challenger, as_of=AS_OF)
    assert both_refused.reasons == (ComparisonFailure.PROOF_NOT_PROVEN,)


def test_a_proof_must_grade_exactly_the_proposal_its_model_names():
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    swapped = evaluate_comparison(_model(), challenger, champion_proof=_proof("p-elsewhere"),
                                  challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    assert swapped.reasons == (ComparisonFailure.PROOF_MODEL_MISMATCH,)
    challenger_side = evaluate_comparison(_model(), challenger,
                                          champion_proof=_proof("p-champion"),
                                          challenger_proof=_proof("p-elsewhere"), as_of=AS_OF)
    assert challenger_side.reasons == (ComparisonFailure.PROOF_MODEL_MISMATCH,)


def test_proof_binding_is_checked_before_model_knowability():
    future_champion = _model(day=11)
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    result = evaluate_comparison(future_champion, challenger,
                                 champion_proof=_proof("p-elsewhere"),
                                 challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    assert result.reasons == (ComparisonFailure.PROOF_MODEL_MISMATCH,)


def test_wrong_roles_with_distinct_identities_are_a_role_mismatch():
    champion = _model("m-a", proposal_id="p-a", role=ModelRole.CHALLENGER)
    challenger = _model("m-b", proposal_id="p-b", role=ModelRole.CHAMPION)
    result = evaluate_comparison(champion, challenger, champion_proof=_proof("p-a"),
                                 challenger_proof=_proof("p-b"), as_of=AS_OF)
    assert result.reasons == (ComparisonFailure.ROLE_MISMATCH,)


def test_retired_side_is_a_role_mismatch():
    challenger = _model("m-b", proposal_id="p-b", role=ModelRole.RETIRED)
    result = evaluate_comparison(_model(), challenger, champion_proof=_proof("p-champion"),
                                 challenger_proof=_proof("p-b"), as_of=AS_OF)
    assert result.reasons == (ComparisonFailure.ROLE_MISMATCH,)


@pytest.mark.parametrize("model_id,proposal_id", [("m-champion", "p-other"),
                                                  ("m-other", "p-champion")])
def test_correct_roles_with_a_shared_identity_are_a_self_comparison(model_id, proposal_id):
    challenger = _model(model_id, proposal_id=proposal_id, role=ModelRole.CHALLENGER)
    result = evaluate_comparison(_model(), challenger, champion_proof=_proof("p-champion"),
                                 challenger_proof=_proof(proposal_id), as_of=AS_OF)
    assert result.reasons == (ComparisonFailure.SELF_COMPARISON,)


@pytest.mark.parametrize("case", ["champion_model", "challenger_model", "champion_proof",
                                  "challenger_proof"])
def test_unknowable_evidence_is_refused_symmetrically(case):
    champion = _model(day=11 if case == "champion_model" else 1)
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER,
                        day=11 if case == "challenger_model" else 1)
    champion_proof = _proof("p-champion", as_of=_at(12) if case == "champion_proof" else _at(6))
    challenger_proof = _proof("p-challenger",
                              as_of=_at(12) if case == "challenger_proof" else _at(6))
    result = evaluate_comparison(champion, challenger, champion_proof=champion_proof,
                                 challenger_proof=challenger_proof, as_of=AS_OF)
    assert result.reasons == (ComparisonFailure.UNKNOWABLE_EVIDENCE,)
    assert result.champion_proof is None
    assert result.challenger_proof is None


def test_equivalent_proof_permutations_compare_equal():
    champion = _model()
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    left = evaluate_comparison(champion, challenger,
                               champion_proof=_proof("p-champion"),
                               challenger_proof=_proof("p-challenger"), as_of=AS_OF)
    right = evaluate_comparison(champion, challenger,
                                champion_proof=_proof("p-champion", reverse_windows=True),
                                challenger_proof=_proof("p-challenger", reverse_windows=True),
                                as_of=AS_OF)
    assert left == right
    assert left.checksum() == right.checksum()
    different = _comparison(challenger_net=0.8)
    assert left != different
    assert left.checksum() != different.checksum()


# ------------------------------------------------------------------------------ retirement

def test_drift_abstention_retires_a_champion_reversibly():
    result = _retirement()
    assert result.accepted is True
    assert result.grounds == (RetirementGround.DRIFT_ABSTENTION,)
    assert result.previous_role is ModelRole.CHAMPION
    assert result.retirement_id == "r-1"
    assert result.retired_at == _at(11)


def test_inferior_comparison_retires_the_champion():
    result = _retirement(drift=None, comparison=_comparison())
    assert result.accepted is True
    assert result.grounds == (RetirementGround.INFERIOR_COMPARISON,)


def test_a_favourable_comparison_is_not_retirement_grounds():
    result = _retirement(drift=None, comparison=_comparison(champion_net=0.9,
                                                            challenger_net=0.1))
    assert result.accepted is False
    assert result.reasons == (TransitionFailure.INSUFFICIENT_EVIDENCE,)


def test_retirement_requires_some_evidence():
    result = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11))
    assert result.reasons == (TransitionFailure.INSUFFICIENT_EVIDENCE,)


def test_retirement_refuses_evidence_naming_another_model():
    other = _model("m-other", proposal_id="p-other")
    result = evaluate_retirement(other, retirement_id="r-1", as_of=_at(11), drift=_drift())
    assert result.reasons == (TransitionFailure.EVIDENCE_MODEL_MISMATCH,)


def test_already_retired_models_are_refused_before_any_evidence_binding():
    retired = _model(role=ModelRole.RETIRED)
    result = evaluate_retirement(retired, retirement_id="r-1", as_of=_at(11), drift=_drift())
    assert result.reasons == (TransitionFailure.MODEL_ALREADY_RETIRED,)
    # The same reason holds when the accompanying evidence would itself have been rejected.
    with_bad_evidence = evaluate_retirement(retired, retirement_id="r-1", as_of=_at(11),
                                            drift="not-a-drift-result")
    assert with_bad_evidence.reasons == (TransitionFailure.MODEL_ALREADY_RETIRED,)
    with_bad_scalar = evaluate_retirement(retired, retirement_id="", as_of=_at(11))
    assert with_bad_scalar.reasons == (TransitionFailure.INVALID_INPUT,)


def test_retirement_refuses_malformed_input_and_models():
    assert evaluate_retirement("model", retirement_id="r-1",
                               as_of=_at(11)).reasons == (TransitionFailure.INVALID_MODEL,)
    assert evaluate_retirement(_model(), retirement_id="",
                               as_of=_at(11)).reasons == (TransitionFailure.INVALID_INPUT,)


def test_nested_refusals_translate_without_leaking_another_enum():
    refused_drift = _drift(model=_model(day=11))
    assert refused_drift.accepted is False
    result = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11), drift=refused_drift)
    assert result.reasons == (TransitionFailure.INVALID_DRIFT,)
    assert all(type(reason) is TransitionFailure for reason in result.reasons)
    refused_comparison = _comparison(champion=_model(day=11))
    assert refused_comparison.accepted is False
    other = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11),
                                comparison=refused_comparison)
    assert other.reasons == (TransitionFailure.INVALID_COMPARISON,)
    assert other.inputs is None
    assert other.grounds == ()


def test_tampered_accepted_drift_is_refused_as_invalid_drift():
    drift = _drift()
    object.__setattr__(drift, "posterior_confidence", 0.99)
    result = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11), drift=drift)
    assert result.reasons == (TransitionFailure.INVALID_DRIFT,)


def test_evidence_dated_after_the_retirement_instant_is_not_grounds():
    drift = _drift(as_of=_at(12), model=_model())
    assert drift.accepted is True
    result = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11), drift=drift)
    assert result.reasons == (TransitionFailure.INSUFFICIENT_EVIDENCE,)


# --------------------------------------------------------------------------- reinstatement

def test_reinstatement_restores_the_exact_original_role():
    retirement = _retirement()
    result = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    assert result.accepted is True
    assert result.restored_role is ModelRole.CHAMPION
    assert result.retirement_id == "r-1"
    assert result.reversal_id == "rev-1"
    assert result.retirement_checksum == retirement.checksum()


def test_a_challenger_can_never_be_reinstated_as_champion():
    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    retirement = _retirement(model=challenger, drift=_drift(model=challenger))
    assert retirement.accepted is True
    result = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    assert result.restored_role is ModelRole.CHALLENGER
    assert result.restored_role is not ModelRole.CHAMPION


def test_reinstatement_binds_the_exact_retirement_value():
    left = _retirement(drift=_drift(prior=0.9))
    right = _retirement(drift=_drift(prior=0.8))
    assert left.checksum() != right.checksum()
    first = evaluate_reinstatement(left, reversal_id="rev-1", as_of=_at(12))
    second = evaluate_reinstatement(right, reversal_id="rev-1", as_of=_at(12))
    assert first != second
    assert first.checksum() != second.checksum()


def test_reinstatement_replay_is_deterministic_and_not_consumed_once():
    retirement = _retirement()
    first = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    second = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    assert first.accepted is True
    assert second.accepted is True
    assert first == second
    assert first.checksum() == second.checksum()


def test_reinstatement_must_follow_the_retirement_it_reverses():
    retirement = _retirement()
    same_instant = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(11))
    assert same_instant.reasons == (TransitionFailure.RETIREMENT_NOT_PRIOR,)
    earlier = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(10))
    assert earlier.reasons == (TransitionFailure.RETIREMENT_NOT_PRIOR,)


def test_reinstatement_refuses_invalid_retirements_and_input():
    assert evaluate_reinstatement("retirement", reversal_id="rev-1",
                                  as_of=_at(12)).reasons == (
        TransitionFailure.INVALID_RETIREMENT,)
    refusal = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11))
    assert evaluate_reinstatement(refusal, reversal_id="rev-1",
                                  as_of=_at(12)).reasons == (
        TransitionFailure.INVALID_RETIREMENT,)
    assert evaluate_reinstatement(_retirement(), reversal_id="",
                                  as_of=_at(12)).reasons == (TransitionFailure.INVALID_INPUT,)


def test_mutated_retirement_is_refused_rather_than_reversed():
    retirement = _retirement()
    object.__setattr__(retirement, "previous_role", ModelRole.CHALLENGER)
    result = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    assert result.reasons == (TransitionFailure.INVALID_RETIREMENT,)
    assert result.restored_role is None


def test_every_declared_transition_reason_is_reachable():
    seen = set()
    seen.update(evaluate_retirement("model", retirement_id="r", as_of=_at(11)).reasons)
    seen.update(evaluate_retirement(_model(), retirement_id="", as_of=_at(11)).reasons)
    seen.update(evaluate_retirement(_model(role=ModelRole.RETIRED), retirement_id="r",
                                    as_of=_at(11)).reasons)
    seen.update(evaluate_retirement(_model(), retirement_id="r", as_of=_at(11),
                                    drift="nope").reasons)
    seen.update(evaluate_retirement(_model(), retirement_id="r", as_of=_at(11),
                                    comparison="nope").reasons)
    seen.update(evaluate_retirement(_model("m-other", proposal_id="p-other"), retirement_id="r",
                                    as_of=_at(11), drift=_drift()).reasons)
    seen.update(evaluate_retirement(_model(), retirement_id="r", as_of=_at(11)).reasons)
    seen.update(evaluate_reinstatement("nope", reversal_id="rev", as_of=_at(12)).reasons)
    seen.update(evaluate_reinstatement(_retirement(), reversal_id="rev", as_of=_at(11)).reasons)
    assert seen == set(TransitionFailure)


def test_every_declared_drift_and_comparison_reason_is_reachable():
    evidence = _sense((_evidence("e-1", Stance.REFUTES),))
    drift_seen = set()
    drift_seen.update(evaluate_drift(_model(), evidence, claim_key="", prior_confidence=0.5,
                                     abstention_threshold=0.5, as_of=AS_OF).reasons)
    drift_seen.update(evaluate_drift("model", evidence, claim_key=CLAIM, prior_confidence=0.5,
                                     abstention_threshold=0.5, as_of=AS_OF).reasons)
    drift_seen.update(evaluate_drift(_model(), "evidence", claim_key=CLAIM, prior_confidence=0.5,
                                     abstention_threshold=0.5, as_of=AS_OF).reasons)
    drift_seen.update(_drift(model=_model(day=11)).reasons)
    assert drift_seen == set(DriftFailure)

    challenger = _model("m-challenger", proposal_id="p-challenger", role=ModelRole.CHALLENGER)
    comparison_seen = set()
    comparison_seen.update(evaluate_comparison(_model(), challenger,
                                               champion_proof=_proof("p-champion"),
                                               challenger_proof=_proof("p-challenger"),
                                               as_of="now").reasons)
    comparison_seen.update(evaluate_comparison("champ", challenger,
                                               champion_proof=_proof("p-champion"),
                                               challenger_proof=_proof("p-challenger"),
                                               as_of=AS_OF).reasons)
    comparison_seen.update(evaluate_comparison(_model(), challenger, champion_proof="proof",
                                               challenger_proof=_proof("p-challenger"),
                                               as_of=AS_OF).reasons)
    refusal = evaluate_prove(_proposal("p-challenger"), windows=(), manifest=OutcomeManifest(
        "man", _at(1), "p-challenger", "sha256:none", (ExpectedOutcome("o-1", "eval-1"),)),
        observations=(), as_of=_at(6), embargo=timedelta(0))
    comparison_seen.update(evaluate_comparison(_model(), challenger,
                                               champion_proof=refusal,
                                               challenger_proof=_proof("p-challenger"),
                                               as_of=AS_OF).reasons)
    comparison_seen.update(evaluate_comparison(_model(), challenger,
                                               champion_proof=_proof("p-elsewhere"),
                                               challenger_proof=_proof("p-challenger"),
                                               as_of=AS_OF).reasons)
    comparison_seen.update(evaluate_comparison(
        _model("m-a", proposal_id="p-a", role=ModelRole.CHALLENGER),
        _model("m-b", proposal_id="p-b", role=ModelRole.CHAMPION),
        champion_proof=_proof("p-a"), challenger_proof=_proof("p-b"), as_of=AS_OF).reasons)
    comparison_seen.update(evaluate_comparison(
        _model(), _model("m-champion", proposal_id="p-other", role=ModelRole.CHALLENGER),
        champion_proof=_proof("p-champion"), challenger_proof=_proof("p-other"),
        as_of=AS_OF).reasons)
    comparison_seen.update(_comparison(champion=_model(day=11)).reasons)
    assert comparison_seen == set(ComparisonFailure)


# ------------------------------------------------------------------------- value integrity

def _accepted():
    drift = _drift()
    comparison = _comparison()
    retirement = _retirement(drift=drift)
    reinstatement = evaluate_reinstatement(retirement, reversal_id="rev-1", as_of=_at(12))
    return drift, comparison, retirement, reinstatement


def test_genuine_results_checksum_and_are_consumable():
    for result in _accepted():
        assert result.accepted is True
        assert result.checksum().startswith("sha256:")


def test_coherent_direct_reconstruction_is_an_equal_consumable_value():
    genuine = _drift()
    reconstructed = DriftResult(True, (), genuine.inputs, genuine.drift_score,
                                genuine.posterior_confidence, genuine.abstain,
                                genuine.supporting_evidence_ids,
                                genuine.refuting_evidence_ids, genuine.calibration)
    assert reconstructed == genuine
    assert reconstructed.checksum() == genuine.checksum()
    retirement = evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11),
                                     drift=reconstructed)
    assert retirement.accepted is True


def test_all_accepted_result_types_support_coherent_direct_reconstruction():
    for genuine in _accepted():
        reconstructed = type(genuine)(*(getattr(genuine, field.name)
                                         for field in fields(genuine)))
        assert reconstructed == genuine
        assert reconstructed.checksum() == genuine.checksum()


def test_incoherent_direct_reconstruction_is_rejected_on_construction():
    genuine = _drift()
    with pytest.raises(ValueError):
        DriftResult(True, (), genuine.inputs, 0.25, genuine.posterior_confidence,
                    genuine.abstain, genuine.supporting_evidence_ids,
                    genuine.refuting_evidence_ids, genuine.calibration)


def test_refusal_shapes_require_exact_zero_metrics_and_empty_tuples():
    with pytest.raises(ValueError):
        DriftResult(False, (DriftFailure.INVALID_INPUT,), None, False, False, False, [], [],
                    "LEARN_DRIFT_V1")
    with pytest.raises(ValueError):
        RetirementResult(False, (TransitionFailure.INVALID_INPUT,), None, None, None, [], None,
                         None, "LEARN_TRANSITION_V1")


def test_object_new_shells_must_still_reconcile_completely():
    genuine = _drift()
    shell = object.__new__(DriftResult)
    for name in ("accepted", "reasons", "inputs", "drift_score", "posterior_confidence",
                 "abstain", "supporting_evidence_ids", "refuting_evidence_ids", "calibration"):
        object.__setattr__(shell, name, getattr(genuine, name))
    object.__setattr__(shell, "posterior_confidence", 0.5)
    with pytest.raises(ValueError):
        shell.checksum()
    assert evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11),
                               drift=shell).reasons == (TransitionFailure.INVALID_DRIFT,)


def test_incoherent_output_mutations_fail_while_coherent_input_values_rebind():
    drift = _drift()
    object.__setattr__(drift, "abstain", False)
    with pytest.raises(ValueError):
        drift.checksum()

    nested = _drift()
    previous = nested.checksum()
    object.__setattr__(nested.inputs.model, "model_id", "m-forged")
    assert nested.checksum() != previous
    assert evaluate_retirement(_model(), retirement_id="r-1", as_of=_at(11),
                               drift=nested).reasons == (
                                   TransitionFailure.EVIDENCE_MODEL_MISMATCH,)

    comparison = _comparison()
    object.__setattr__(comparison, "preference", ComparisonPreference.CHAMPION)
    with pytest.raises(ValueError):
        comparison.checksum()

    retirement = _retirement()
    object.__setattr__(retirement, "retirement_id", "r-forged")
    with pytest.raises(ValueError):
        retirement.checksum()

    reinstatement = evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12))
    object.__setattr__(reinstatement, "reversal_id", "rev-forged")
    with pytest.raises(ValueError):
        reinstatement.checksum()


def test_every_derived_result_field_is_recomputed_before_checksumming():
    cases = (
        (_drift, "drift_score", lambda _result: 0.25),
        (_drift, "posterior_confidence", lambda _result: 0.25),
        (_drift, "abstain", lambda _result: False),
        (_drift, "supporting_evidence_ids", lambda _result: ("e-forged",)),
        (_drift, "refuting_evidence_ids", lambda _result: ()),
        (_drift, "calibration", lambda _result: "FORGED"),
        (_comparison, "preference", lambda _result: ComparisonPreference.CHAMPION),
        (_comparison, "champion_proof", lambda result: result.challenger_proof),
        (_comparison, "challenger_proof", lambda result: result.champion_proof),
        (_comparison, "calibration", lambda _result: "FORGED"),
        (_retirement, "model_id", lambda _result: "m-forged"),
        (_retirement, "previous_role", lambda _result: ModelRole.CHALLENGER),
        (_retirement, "grounds", lambda _result: ()),
        (_retirement, "retirement_id", lambda _result: "r-forged"),
        (_retirement, "retired_at", lambda _result: _at(12)),
        (_retirement, "calibration", lambda _result: "FORGED"),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "model_id", lambda _result: "m-forged"),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "restored_role", lambda _result: ModelRole.CHALLENGER),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "retirement_id", lambda _result: "r-forged"),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "retirement_checksum", lambda _result: "sha256:forged"),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "reversal_id", lambda _result: "rev-forged"),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "reinstated_at", lambda _result: _at(13)),
        (lambda: evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12)),
         "calibration", lambda _result: "FORGED"),
    )
    for builder, field, replacement in cases:
        result = builder()
        object.__setattr__(result, field, replacement(result))
        with pytest.raises(ValueError):
            result.checksum()


def test_nested_proof_summary_counts_require_exact_non_negative_ints():
    comparison = _comparison()
    object.__setattr__(comparison.champion_proof, "expected_count", True)
    with pytest.raises(ValueError):
        comparison.checksum()


def test_transition_identifiers_reject_hostile_string_subclasses():
    class AlwaysEqual(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    retirement = _retirement()
    object.__setattr__(retirement, "model_id", AlwaysEqual("m-forged"))
    with pytest.raises(ValueError):
        retirement.checksum()

    reinstatement = evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12))
    object.__setattr__(reinstatement, "reversal_id", AlwaysEqual("rev-forged"))
    with pytest.raises(ValueError):
        reinstatement.checksum()


def test_results_are_unhashable_because_reflective_mutation_is_possible():
    for result in _accepted():
        with pytest.raises(TypeError):
            hash(result)


def test_copy_and_pickle_round_trips_preserve_the_same_revalidated_value():
    for result in _accepted():
        for reconstructed in (copy.copy(result), copy.deepcopy(result),
                              pickle.loads(pickle.dumps(result))):
            assert reconstructed == result
            assert reconstructed.checksum() == result.checksum()


def test_results_cannot_carry_extra_attributes():
    for result in _accepted():
        with pytest.raises(AttributeError):
            object.__setattr__(result, "promote", True)


def test_exact_globals_evaluator_clone_produces_the_same_canonical_value():
    clone = types.FunctionType(evaluate_drift.__code__, evaluate_drift.__globals__, "clone",
                               evaluate_drift.__defaults__, evaluate_drift.__closure__)
    model = _model()
    evidence = _sense((_evidence("e-1", Stance.REFUTES),))
    cloned = clone(model, evidence, claim_key=CLAIM, prior_confidence=0.5,
                   abstention_threshold=0.5, as_of=AS_OF)
    genuine = evaluate_drift(model, evidence, claim_key=CLAIM, prior_confidence=0.5,
                             abstention_threshold=0.5, as_of=AS_OF)
    assert cloned == genuine
    assert cloned.checksum() == genuine.checksum()


# --------------------------------------------------------------------------- documentation

PINNED = (
    ("LEARN represents no order, allocation, sizing, execution, deployment, promotion or "
     "risk-relaxation authority."),
    "A challenger can never be promoted to champion.",
    "Equivalent inputs produce equal results and identical checksums.",
    "Every proof must grade exactly the `proposal_id` its `ModelRecord` names.",
    "Empty usable evidence can never manufacture drift, abstention or retirement grounds.",
    "Reinstatement restores exactly the role the model held before its retirement.",
    "Replaying an evaluator on identical inputs is stateless and reproduces the same result.",
    "A model registered after the evaluation as-of instant can never produce actionable drift.",
    ("This module deliberately makes no claim that it can authenticate which Python function "
     "created an object."),
)

LEARN_EXPORTS = ("ComparisonFailure", "ComparisonInputs", "ComparisonPreference",
                 "ComparisonResult", "DriftFailure", "DriftInputs", "DriftResult", "ModelRecord",
                 "ModelRole", "ProofSummary", "ReinstatementInputs", "ReinstatementResult",
                 "RetirementGround", "RetirementInputs", "RetirementResult", "TransitionFailure",
                 "evaluate_comparison", "evaluate_drift", "evaluate_reinstatement",
                 "evaluate_retirement")


def _prose() -> str:
    """Markup-aware normalisation: line wrapping must not hide a pinned sentence."""
    return " ".join(DOC.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("sentence", PINNED)
def test_documentation_pins_every_learn_contract_sentence(sentence):
    assert sentence in _prose()


def test_documentation_covers_the_exact_public_learn_inventory():
    prose = _prose()
    for name in LEARN_EXPORTS:
        assert f"`{name}`" in prose, name


def test_public_brain_surface_is_sorted_unique_and_complete():
    from atp import brain

    assert brain.__all__ == sorted(brain.__all__)
    assert len(brain.__all__) == len(set(brain.__all__))
    for name in LEARN_EXPORTS:
        assert name in brain.__all__
        assert getattr(brain, name) is getattr(learn_module, name)


def test_learn_module_explicitly_exports_only_the_documented_inventory():
    assert learn_module.__all__ == LEARN_EXPORTS
    assert learn_module.__all__ == tuple(sorted(learn_module.__all__))


def test_no_learn_export_offers_deployment_or_promotion_capability():
    for name in LEARN_EXPORTS:
        exported = getattr(learn_module, name)
        if not isinstance(exported, type):
            continue
        declared = set(vars(exported))
        assert not (declared & {"promote", "deploy", "execute", "allocate", "size", "order"}), name


def test_drift_inputs_shell_is_public_but_validating():
    with pytest.raises(ValueError):
        DriftInputs(_model(), "evidence", CLAIM, 0.5, 0.5, AS_OF)


def test_reinstatement_result_type_is_exported_and_sealed():
    reinstatement = evaluate_reinstatement(_retirement(), reversal_id="rev-1", as_of=_at(12))
    assert isinstance(reinstatement, ReinstatementResult)
    with pytest.raises(AttributeError):
        object.__setattr__(reinstatement, "promote", True)
