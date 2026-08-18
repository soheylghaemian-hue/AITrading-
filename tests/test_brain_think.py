from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from atp.brain import (Assertion, ContradictionGroup, Evidence, EvidenceQuality, Hypothesis,
                       InvalidationCondition, RejectedEvidence, Scenario, SenseFailure,
                       SenseResult, Stance, ThinkFailure, evaluate_sense, evaluate_think)
from atp.brain import think as think_module

# Historical instants only: THINK answers from the SenseResult's own `as_of`, never the wall clock.
AS_OF = datetime(2020, 1, 2, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
CLAIM = "CPI_PRINT>CONSENSUS"


def _evidence(evidence_id, stance=None, *, event=None, available=None, observed=None,
              quality=EvidenceQuality.VERIFIED, claim_key=CLAIM):
    event = AS_OF - HOUR if event is None else event
    available = event if available is None else available
    observed = available if observed is None else observed
    assertions = () if stance is None else (Assertion(claim_key, stance),)
    return Evidence(evidence_id, "filing", event, available, observed, quality,
                    "sha256:" + evidence_id, assertions)


def _shift(item, tz):
    """The very same instants, spelled in another timezone."""
    return Evidence(item.evidence_id, item.source, item.event_time.astimezone(tz),
                    item.available_time.astimezone(tz), item.observed_time.astimezone(tz),
                    item.quality, item.checksum, item.assertions)


def _hypothesis(hypothesis_id, stance, prior=0.5):
    opposite = Stance.REFUTES if stance is Stance.SUPPORTS else Stance.SUPPORTS
    condition = InvalidationCondition(f"{hypothesis_id}:falsifier", CLAIM, opposite)
    return Hypothesis(hypothesis_id, CLAIM, stance, f"thesis {hypothesis_id}", prior, (condition,))


def _sense(items, as_of=AS_OF, limit=DAY):
    return evaluate_sense(items, as_of=as_of, freshness_limit=limit)


def _forged(usable=(), rejected=(), contradictions=(), as_of=AS_OF, limit=DAY):
    """A hand-built boundary object: correct type, entirely unproven content."""
    return SenseResult(as_of, limit, usable, rejected, contradictions)


def _assert_rejected(result):
    assert result.admitted is False
    assert result.judgements == ()
    assert result.beliefs == ()
    assert result.scenarios == ()
    assert result.reasons == (ThinkFailure.INVALID_SENSE_RESULT,)
    # One deterministic refusal: the reason never varies with how the boundary was forged.
    assert result.checksum() == evaluate_think([], _forged(usable=("junk",))).checksum()


def test_competing_hypotheses_are_all_preserved_with_bounded_scores():
    sense = _sense([_evidence("s1", Stance.SUPPORTS), _evidence("r1", Stance.REFUTES)])
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS), _hypothesis("bear", Stance.REFUTES)]
    result = evaluate_think(hypotheses, sense)
    assert result.admitted and result.reasons == ()
    assert [j.hypothesis_id for j in result.judgements] == ["bear", "bull"]
    assert len(result.beliefs) == len(result.scenarios) == 2
    for belief in result.beliefs:
        assert 0.0 <= belief.score <= 1.0
        assert belief.valid_until >= sense.as_of
    assert {j.hypothesis_id: j.belief.evidence_ids for j in result.judgements} == {
        "bull": ("s1",), "bear": ("r1",)}
    assert {j.hypothesis_id: j.belief.counter_evidence_ids for j in result.judgements} == {
        "bull": ("r1",), "bear": ("s1",)}


def test_every_admitted_scenario_is_machine_checkably_falsifiable():
    result = evaluate_think([_hypothesis("bull", Stance.SUPPORTS)],
                            _sense([_evidence("s1", Stance.SUPPORTS)]))
    assert result.calibration == "THINK_HEURISTIC_V1"
    for scenario in result.scenarios:
        assert scenario.invalidation_conditions
        for condition in scenario.invalidation_conditions:
            assert isinstance(condition, InvalidationCondition)
            assert isinstance(condition.trigger_stance, Stance)
            assert condition.claim_key == CLAIM


def test_typed_invalidation_contracts_fail_closed():
    for bad_id in ("", 1, None, True):
        with pytest.raises(ValueError):
            InvalidationCondition(bad_id, CLAIM, Stance.REFUTES)
        with pytest.raises(ValueError):
            InvalidationCondition("c", bad_id, Stance.REFUTES)
    with pytest.raises(ValueError):
        InvalidationCondition("c", CLAIM, "REFUTES")
    with pytest.raises(ValueError):
        Scenario("s", "thesis", 0.5, ())
    with pytest.raises(ValueError):
        Scenario("s", "thesis", 0.5, ("prose only",))
    with pytest.raises(ValueError):
        Scenario("s", "thesis", 0.5, [InvalidationCondition("c", CLAIM, Stance.REFUTES)])
    with pytest.raises(ValueError):
        Scenario("s", "thesis", float("nan"), (InvalidationCondition("c", CLAIM, Stance.REFUTES),))
    with pytest.raises(ValueError):
        Hypothesis("h", CLAIM, Stance.SUPPORTS, "thesis", 0.5,
                   (InvalidationCondition("c", CLAIM, Stance.REFUTES),
                    InvalidationCondition("c", CLAIM, Stance.SUPPORTS)))


def test_confidence_is_a_bounded_evidence_weighted_heuristic():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    none = evaluate_think(hypotheses, _sense([])).beliefs[0]
    supported = evaluate_think(hypotheses, _sense([_evidence("s1", Stance.SUPPORTS)])).beliefs[0]
    refuted = evaluate_think(hypotheses, _sense([_evidence("r1", Stance.REFUTES)])).beliefs[0]
    weaker = evaluate_think(hypotheses, _sense([
        _evidence("s1", Stance.SUPPORTS, quality=EvidenceQuality.UNKNOWN)])).beliefs[0]
    assert none.score == 0.5
    assert refuted.score < none.score < weaker.score < supported.score
    assert all(0.0 <= belief.score <= 1.0 for belief in (none, supported, refuted, weaker))
    assert none.valid_until == AS_OF


def test_permutation_and_timezone_spelling_change_nothing():
    elsewhere = timezone(timedelta(hours=-7))
    items = [_evidence("s1", Stance.SUPPORTS), _evidence("r1", Stance.REFUTES),
             _evidence("s2", Stance.SUPPORTS, quality=EvidenceQuality.OBSERVED_ONLY)]
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS), _hypothesis("bear", Stance.REFUTES)]
    forward = evaluate_think(hypotheses, _sense(items))
    reverse = evaluate_think(list(reversed(hypotheses)), _sense(list(reversed(items))))
    shifted = evaluate_think(hypotheses, _sense([_shift(item, elsewhere) for item in items],
                                                as_of=AS_OF.astimezone(elsewhere)))
    assert forward.checksum() == reverse.checksum() == shifted.checksum()
    for other in (reverse, shifted):
        assert [j.hypothesis_id for j in forward.judgements] == [
            j.hypothesis_id for j in other.judgements]
        assert [b.score for b in forward.beliefs] == [b.score for b in other.beliefs]
        assert forward.calibration == other.calibration


def test_future_or_stale_usable_evidence_fails_closed():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    future = _evidence("future", Stance.SUPPORTS, event=AS_OF + HOUR, available=AS_OF + HOUR,
                       observed=AS_OF + HOUR)
    late_availability = _evidence("late", Stance.SUPPORTS, available=AS_OF + HOUR,
                                  observed=AS_OF + HOUR)
    stale = _evidence("stale", Stance.SUPPORTS, event=AS_OF - 3 * DAY)
    for item in (future, late_availability, stale):
        _assert_rejected(evaluate_think(hypotheses, _forged(usable=(item,))))


def test_duplicate_ids_and_partition_tampering_fail_closed():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    first = _evidence("dup", Stance.SUPPORTS)
    second = _evidence("dup", Stance.REFUTES)
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(first, second))))
    _assert_rejected(evaluate_think(hypotheses, _forged(
        usable=(first,),
        rejected=(RejectedEvidence(second, SenseFailure.DUPLICATE_EVIDENCE_ID),))))
    fresh = _evidence("fresh", Stance.SUPPORTS)
    _assert_rejected(evaluate_think(hypotheses, _forged(
        rejected=(RejectedEvidence(fresh, SenseFailure.STALE_BEYOND_FRESHNESS_LIMIT),))))
    leaked = _evidence("leaked", Stance.SUPPORTS, event=AS_OF + HOUR, available=AS_OF + HOUR,
                       observed=AS_OF + HOUR)
    _assert_rejected(evaluate_think(hypotheses, _forged(
        rejected=(RejectedEvidence(leaked, SenseFailure.DUPLICATE_EVIDENCE_ID),))))


def test_forged_contradiction_groups_and_ordering_fail_closed():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    supports = _evidence("s1", Stance.SUPPORTS)
    refutes = _evidence("r1", Stance.REFUTES)
    honest = _sense([supports, refutes])
    assert honest.contradictions and [e.evidence_id for e in honest.usable] == ["r1", "s1"]
    # A suppressed contradiction group: the same usable set, minus the conflict it must report.
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(refutes, supports))))
    # An extraneous group naming evidence that was never admitted.
    _assert_rejected(evaluate_think(hypotheses, _forged(
        usable=(supports,), contradictions=(ContradictionGroup(CLAIM, ("s1",), ("ghost",)),))))
    early = _evidence("a", Stance.SUPPORTS, event=AS_OF - 3 * HOUR)
    later = _evidence("b", Stance.SUPPORTS, event=AS_OF - HOUR)
    assert [e.evidence_id for e in _sense([early, later]).usable] == ["a", "b"]
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(later, early))))


def test_tampered_evidence_internals_fail_closed():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    reordered = _evidence("t1", Stance.SUPPORTS)
    sense = _sense([reordered])
    assert evaluate_think(hypotheses, sense).admitted
    object.__setattr__(reordered, "available_time", AS_OF - timedelta(minutes=1))
    _assert_rejected(evaluate_think(hypotheses, sense))
    untyped = _evidence("t2", Stance.SUPPORTS)
    other = _sense([untyped])
    object.__setattr__(untyped, "quality", "VERIFIED")
    _assert_rejected(evaluate_think(hypotheses, other))


def test_malformed_boundary_objects_fail_closed():
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    item = _evidence("s1", Stance.SUPPORTS)
    _assert_rejected(evaluate_think(hypotheses, None))
    _assert_rejected(evaluate_think(hypotheses, object()))
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=("not evidence",))))
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=[item])))
    _assert_rejected(evaluate_think(hypotheses, _forged(rejected=(item,))))
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(item,), limit=3600)))
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(item,), limit=-DAY)))
    _assert_rejected(evaluate_think(hypotheses,
                                    _forged(usable=(item,), as_of=AS_OF.replace(tzinfo=None))))
    _assert_rejected(evaluate_think(hypotheses, _forged(
        usable=(item,), contradictions=(ContradictionGroup(CLAIM, ("s1",), (7,)),))))


def test_a_dst_fold_cannot_smuggle_a_later_instant():
    zone = ZoneInfo("America/New_York")
    as_of = datetime(2020, 11, 1, 1, 30, tzinfo=zone, fold=0)
    folded = datetime(2020, 11, 1, 1, 30, tzinfo=zone, fold=1)
    # Identical wall time, but the folded instant is an hour later in absolute time.
    assert as_of.astimezone(UTC) < folded.astimezone(UTC)
    released = datetime(2020, 11, 1, 0, 30, tzinfo=zone)
    item = _evidence("fold", Stance.SUPPORTS, event=released, available=released, observed=folded)
    sense = evaluate_sense([item], as_of=as_of, freshness_limit=DAY)
    assert sense.usable == ()
    assert [r.reason for r in sense.rejected] == [SenseFailure.OBSERVED_TIME_AFTER_AS_OF]
    utc_sense = evaluate_sense([_shift(item, UTC)], as_of=as_of.astimezone(UTC),
                               freshness_limit=DAY)
    assert utc_sense.checksum() == sense.checksum()
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    folded_think = evaluate_think(hypotheses, sense)
    utc_think = evaluate_think(hypotheses, utc_sense)
    assert folded_think.checksum() == utc_think.checksum()
    assert folded_think.beliefs[0].evidence_ids == ()
    assert folded_think.beliefs[0].counter_evidence_ids == ()
    # Forced into `usable`, the folded item must fail closed rather than inform a belief.
    _assert_rejected(evaluate_think(hypotheses, _forged(usable=(item,), as_of=as_of)))


def test_rejected_evidence_never_reaches_beliefs_scenarios_or_horizons():
    admitted = _evidence("ok", Stance.SUPPORTS)
    leaked = _evidence("leaked", Stance.REFUTES, event=AS_OF + HOUR, available=AS_OF + HOUR,
                       observed=AS_OF + HOUR)
    sense = _sense([admitted, leaked])
    assert not sense.fully_usable
    result = evaluate_think([_hypothesis("bull", Stance.SUPPORTS)], sense)
    belief = result.beliefs[0]
    assert belief.evidence_ids == ("ok",)
    assert belief.counter_evidence_ids == ()
    assert belief.valid_until == AS_OF - HOUR + DAY
    assert belief.score == evaluate_think([_hypothesis("bull", Stance.SUPPORTS)],
                                          _sense([admitted])).beliefs[0].score


def test_an_extreme_freshness_limit_stays_consumable():
    # `evaluate_sense` admits any non-negative limit, so the validity horizon must saturate at the
    # largest representable instant rather than raise OverflowError out of a valid result.
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    sense = _sense([_evidence("s1", Stance.SUPPORTS)], limit=timedelta.max)
    assert [e.evidence_id for e in sense.usable] == ["s1"]
    result = evaluate_think(hypotheses, sense)
    assert result.admitted and result.reasons == ()
    belief = result.beliefs[0]
    assert belief.evidence_ids == ("s1",)
    assert belief.valid_until == datetime.max.replace(tzinfo=UTC)
    assert result.checksum() == evaluate_think(hypotheses, sense).checksum()
    # An unreachable horizon is still bounded: with no contributing evidence it stays `as_of`.
    empty = evaluate_think(hypotheses, _sense([], limit=timedelta.max))
    assert empty.beliefs[0].valid_until == AS_OF


def test_rebinding_module_calibration_attributes_changes_nothing():
    sense = _sense([_evidence("s1", Stance.SUPPORTS), _evidence("r1", Stance.REFUTES)])
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS), _hypothesis("bear", Stance.REFUTES)]
    before = evaluate_think(hypotheses, sense)
    tampered = {"_QUALITY_WEIGHT": {}, "QUALITY_WEIGHT": {}, "_CALIBRATION_VERSION": "TAMPERED",
                "CALIBRATION_VERSION": "TAMPERED", "CALIBRATION": "TAMPERED"}
    for name, value in tampered.items():
        setattr(think_module, name, value)
    try:
        after = evaluate_think(hypotheses, sense)
        assert after.checksum() == before.checksum()
        assert after.calibration == before.calibration == "THINK_HEURISTIC_V1"
        assert [b.score for b in after.beliefs] == [b.score for b in before.beliefs]
        assert [j.hypothesis_id for j in after.judgements] == [
            j.hypothesis_id for j in before.judgements]
    finally:
        for name in tampered:
            delattr(think_module, name)


def test_hypothesis_arguments_fail_closed():
    sense = _sense([])
    condition = (InvalidationCondition("c", CLAIM, Stance.REFUTES),)
    with pytest.raises(ValueError):
        evaluate_think(["not a hypothesis"], sense)
    with pytest.raises(ValueError):
        evaluate_think([_hypothesis("dup", Stance.SUPPORTS), _hypothesis("dup", Stance.REFUTES)],
                       sense)
    with pytest.raises(ValueError):
        Hypothesis("h", CLAIM, "SUPPORTS", "thesis", 0.5, condition)
    with pytest.raises(ValueError):
        Hypothesis("h", CLAIM, Stance.SUPPORTS, "thesis", float("nan"), condition)
    with pytest.raises(ValueError):
        Hypothesis("h", CLAIM, Stance.SUPPORTS, "thesis", 1.5, condition)
    with pytest.raises(ValueError):
        Hypothesis("h", CLAIM, Stance.SUPPORTS, "thesis", 0.5, ())


def test_evaluation_is_pure_and_repeatable():
    items = [_evidence("s1", Stance.SUPPORTS), _evidence("r1", Stance.REFUTES)]
    sense = _sense(items)
    hypotheses = [_hypothesis("bull", Stance.SUPPORTS)]
    first = evaluate_think(hypotheses, sense)
    second = evaluate_think(hypotheses, sense)
    assert first == second and first.checksum() == second.checksum()
    # The evaluator mutated neither its arguments nor any shared state.
    assert sense.checksum() == _sense(items).checksum()
    assert hypotheses == [_hypothesis("bull", Stance.SUPPORTS)]
