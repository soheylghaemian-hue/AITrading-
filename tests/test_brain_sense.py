from datetime import UTC, datetime, timedelta, timezone

import pytest

from atp.brain import (Assertion, ContradictionGroup, Evidence, EvidenceQuality, RejectedEvidence,
                       SenseFailure, Stance, evaluate_sense)

# A deliberately historical instant: SENSE must answer from `as_of` alone, never the wall clock.
AS_OF = datetime(2020, 1, 2, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _raw(event, available, observed):
    return Evidence("e1", "filing", event, available, observed, EvidenceQuality.OBSERVED_ONLY,
                    "sha256:e1")


def _evidence(evidence_id="e1", *, event=None, available=None, observed=None, assertions=(),
              source="filing"):
    event = AS_OF - HOUR if event is None else event
    available = event if available is None else available
    observed = available if observed is None else observed
    return Evidence(evidence_id, source, event, available, observed,
                    EvidenceQuality.OBSERVED_ONLY, "sha256:" + evidence_id, assertions)


def test_every_timestamp_is_required():
    for index in range(3):
        stamps = [AS_OF, AS_OF, AS_OF]
        stamps[index] = None
        with pytest.raises(ValueError):
            _raw(*stamps)


def test_naive_timestamps_are_rejected():
    for index in range(3):
        stamps = [AS_OF, AS_OF, AS_OF]
        stamps[index] = AS_OF.replace(tzinfo=None)
        with pytest.raises(ValueError):
            _raw(*stamps)


def test_timestamp_ordering_is_enforced_and_equal_boundaries_are_valid():
    with pytest.raises(ValueError):
        _raw(AS_OF, AS_OF - HOUR, AS_OF)
    with pytest.raises(ValueError):
        _raw(AS_OF - HOUR, AS_OF, AS_OF - HOUR)
    _raw(AS_OF - HOUR, AS_OF - HOUR, AS_OF)
    _raw(AS_OF, AS_OF, AS_OF)


def test_evidence_exactly_at_as_of_is_usable_at_a_zero_freshness_limit():
    item = _evidence(event=AS_OF, available=AS_OF, observed=AS_OF)
    result = evaluate_sense([item], as_of=AS_OF, freshness_limit=timedelta(0))
    assert result.usable == (item,)
    assert result.rejected == ()
    assert result.fully_usable


def test_future_leakage_is_rejected_for_each_timestamp():
    cases = {
        SenseFailure.EVENT_TIME_AFTER_AS_OF: (AS_OF + HOUR, AS_OF + HOUR, AS_OF + HOUR),
        SenseFailure.AVAILABLE_TIME_AFTER_AS_OF: (AS_OF - HOUR, AS_OF + HOUR, AS_OF + HOUR),
        SenseFailure.OBSERVED_TIME_AFTER_AS_OF: (AS_OF - HOUR, AS_OF - HOUR, AS_OF + HOUR),
    }
    for reason, (event, available, observed) in cases.items():
        item = _evidence(event=event, available=available, observed=observed)
        result = evaluate_sense([item], as_of=AS_OF, freshness_limit=DAY)
        assert result.usable == ()
        assert result.rejected == (RejectedEvidence(item, reason),)
        assert not result.fully_usable


def test_only_the_caller_supplied_as_of_defines_the_present():
    item = _evidence(event=AS_OF, available=AS_OF, observed=AS_OF)
    result = evaluate_sense([item], as_of=AS_OF - 365 * DAY, freshness_limit=3650 * DAY)
    assert [r.reason for r in result.rejected] == [SenseFailure.EVENT_TIME_AFTER_AS_OF]


def test_freshness_uses_observation_age_and_the_boundary_is_inclusive():
    released = AS_OF - 3 * HOUR
    fresh = _evidence("fresh", event=released, available=released, observed=AS_OF - HOUR)
    stale = _evidence("stale", event=released, available=released,
                      observed=AS_OF - HOUR - timedelta(seconds=1))
    result = evaluate_sense([fresh, stale], as_of=AS_OF, freshness_limit=HOUR)
    assert result.usable == (fresh,)
    assert result.rejected == (RejectedEvidence(stale, SenseFailure.STALE_BEYOND_FRESHNESS_LIMIT),)


def test_duplicate_evidence_ids_reject_every_occurrence():
    first = _evidence("dup", source="primary")
    second = _evidence("dup", source="mirror")
    result = evaluate_sense([first, second], as_of=AS_OF, freshness_limit=DAY)
    assert result.usable == ()
    assert sorted(r.evidence.source for r in result.rejected) == ["mirror", "primary"]
    assert {r.reason for r in result.rejected} == {SenseFailure.DUPLICATE_EVIDENCE_ID}


def test_ordering_and_checksums_are_stable():
    items = [_evidence("c", event=AS_OF - HOUR), _evidence("a", event=AS_OF - 3 * HOUR),
             _evidence("b", event=AS_OF - 2 * HOUR)]
    forward = evaluate_sense(items, as_of=AS_OF, freshness_limit=DAY)
    reverse = evaluate_sense(list(reversed(items)), as_of=AS_OF, freshness_limit=DAY)
    assert [e.evidence_id for e in forward.usable] == ["a", "b", "c"]
    assert [e.evidence_id for e in reverse.usable] == ["a", "b", "c"]
    assert forward.checksum() == reverse.checksum()


def test_equivalent_instants_in_another_timezone_produce_the_same_checksum():
    elsewhere = timezone(timedelta(hours=2))
    utc_items = [_evidence("a", event=AS_OF - 2 * HOUR), _evidence("b", event=AS_OF - HOUR)]
    shifted = [_evidence(item.evidence_id, event=item.event_time.astimezone(elsewhere),
                         available=item.available_time.astimezone(elsewhere),
                         observed=item.observed_time.astimezone(elsewhere)) for item in utc_items]
    assert (evaluate_sense(shifted, as_of=AS_OF.astimezone(elsewhere), freshness_limit=DAY).checksum()
            == evaluate_sense(utc_items, as_of=AS_OF, freshness_limit=DAY).checksum())


def test_opposing_stances_stay_individually_visible():
    claim = "CPI_PRINT>CONSENSUS"
    items = [_evidence("r1", assertions=(Assertion(claim, Stance.REFUTES),)),
             _evidence("s2", assertions=(Assertion(claim, Stance.SUPPORTS),)),
             _evidence("s1", assertions=(Assertion(claim, Stance.SUPPORTS),))]
    result = evaluate_sense(items, as_of=AS_OF, freshness_limit=DAY)
    assert len(result.usable) == 3
    assert result.contradictions == (ContradictionGroup(claim, ("s1", "s2"), ("r1",)),)


def test_claim_keys_are_matched_exactly():
    items = [_evidence("a", assertions=(Assertion("cpi", Stance.SUPPORTS),)),
             _evidence("b", assertions=(Assertion("CPI", Stance.REFUTES),))]
    assert evaluate_sense(items, as_of=AS_OF, freshness_limit=DAY).contradictions == ()


def test_rejected_evidence_cannot_form_a_contradiction():
    admitted = _evidence("ok", assertions=(Assertion("k", Stance.SUPPORTS),))
    leaked = _evidence("leaked", event=AS_OF + HOUR, assertions=(Assertion("k", Stance.REFUTES),))
    result = evaluate_sense([admitted, leaked], as_of=AS_OF, freshness_limit=DAY)
    assert result.usable == (admitted,)
    assert result.contradictions == ()
    assert not result.fully_usable


def test_one_item_cannot_assert_the_same_claim_key_twice():
    with pytest.raises(ValueError):
        _evidence(assertions=(Assertion("k", Stance.SUPPORTS), Assertion("k", Stance.REFUTES)))


def test_assertions_require_a_claim_key_and_an_explicit_stance():
    with pytest.raises(ValueError):
        Assertion("", Stance.SUPPORTS)
    with pytest.raises(ValueError):
        Assertion("k", "SUPPORTS")


def test_evaluator_arguments_fail_closed():
    item = _evidence()
    with pytest.raises(ValueError):
        evaluate_sense([item], as_of=AS_OF.replace(tzinfo=None), freshness_limit=HOUR)
    with pytest.raises(ValueError):
        evaluate_sense([item], as_of=AS_OF, freshness_limit=-HOUR)
    with pytest.raises(ValueError):
        evaluate_sense([item], as_of=AS_OF, freshness_limit=3600)
    with pytest.raises(ValueError):
        evaluate_sense(["not evidence"], as_of=AS_OF, freshness_limit=HOUR)
