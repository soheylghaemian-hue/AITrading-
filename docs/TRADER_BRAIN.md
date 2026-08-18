# Trader Brain — SENSE and THINK (research only)

`atp.brain` is a research vocabulary. It holds no order, allocation, leverage or execution authority, it
cannot mutate the safety constitution, and nothing here may be promoted to trading authority outside a
separately governed phase. SENSE only decides which evidence a researcher may look at for one explicit
point in time.

## The three timestamps

Every `Evidence` item carries three required, timezone-aware timestamps. A missing or naive timestamp is
rejected, and no timestamp is ever inferred from another.

| Timestamp | Meaning |
| --- | --- |
| `event_time` | when the fact itself happened in the world |
| `available_time` | when the fact first became externally knowable (published, filed, released) |
| `observed_time` | when this system actually observed the fact |

They must be ordered `event_time <= available_time <= observed_time`. Equal timestamps are legal: a
release observed in the same instant it is published is valid, not suspicious.

## Point-in-time admission

`evaluate_sense(evidence, as_of=..., freshness_limit=...)` is pure. It takes the caller's `as_of` instant
and a non-negative `timedelta` limit, and it never reads the clock, performs I/O or calls a provider — the
package contains no provider client at all. The wall-clock time of the run is irrelevant, so a question
asked about 2020 is answered exactly as it would have been answered in 2020.

Freshness is **observation age** — `as_of - observed_time` — the same convention
`atp.research.intel.provenance` already uses when it ages an observation against its capture time. The
boundary is inclusive: an age exactly equal to the limit is still usable.

## Failure reasons

Admission fails closed. Every rejected item is preserved together with one stable reason code, so an audit
never has to re-derive what was dropped. When several conditions apply, the first in this order is reported.

| Reason | Condition |
| --- | --- |
| `DUPLICATE_EVIDENCE_ID` | the id appears more than once; every occurrence is rejected instead of implicitly preferring one |
| `EVENT_TIME_AFTER_AS_OF` | the event had not happened yet at `as_of` |
| `AVAILABLE_TIME_AFTER_AS_OF` | the fact was not yet knowable at `as_of` |
| `OBSERVED_TIME_AFTER_AS_OF` | this system had not observed it yet at `as_of` |
| `STALE_BEYOND_FRESHNESS_LIMIT` | the observation age exceeds the caller's limit |

`SenseResult.fully_usable` is true only when nothing was rejected, so a partially admitted set can never be
mistaken for a clean one. Invalid arguments — a naive `as_of`, a negative or non-`timedelta` limit, a
non-`Evidence` input — raise instead of returning a degraded result.

## Contradictions stay visible

An `Assertion` binds one exact `claim_key` to an explicit `Stance` (`SUPPORTS` or `REFUTES`). There is no
neutral stance, and keys are compared exactly: never case-folded, trimmed or normalised. When usable
evidence takes both stances on the same key, `SenseResult.contradictions` reports a group naming every
contributing evidence id on each side. A conflict is never averaged into a score, dropped, or resolved by
recency. Only rejected evidence is absent, because it was never admitted in the first place.

## Determinism

Ordering and checksums come from canonical serialization: timestamps are normalised to UTC, JSON keys are
sorted, and evidence is sorted by `(event_time, available_time, observed_time, evidence_id, canonical
payload)`. Re-running the evaluator on equivalent inputs — in any input order, in any timezone spelling of
the same instants — produces identical ordering and an identical `SenseResult.checksum()`.

## THINK — competing beliefs, never a winner

`evaluate_think(hypotheses, sense_result)` turns admitted SENSE evidence into one bounded belief and one
falsifiable scenario per hypothesis. Every hypothesis the caller supplies comes back: opposing positions on
the same `claim_key` stay side by side, in `hypothesis_id` order. Nothing selects a winner, ranks by score,
sizes a position or implies an action.

### The SENSE boundary is untrusted

A `SenseResult` is a plain dataclass, so a caller can hand-build one holding future, stale, duplicated or
mutually inconsistent evidence, and a frozen dataclass can still be tampered with through
`object.__setattr__`. THINK never trusts the declared type. It revalidates every field, re-proves each
`Evidence` item's own constructor invariants, then re-runs canonical SENSE admission over the union of the
represented usable and rejected evidence using the result's own `as_of` and `freshness_limit`. The supplied
partitions, rejection reasons, ordering and contradiction groups must match that reconstruction exactly.

Anything else — a malformed nested object, a naive `as_of`, a negative or non-`timedelta` limit, a future or
stale item parked in `usable`, a duplicate id within or across partitions, a fabricated rejection reason, a
missing or extraneous contradiction group — returns one deterministic non-admitted `ThinkResult`: `admitted`
false, `reasons == (INVALID_SENSE_RESULT,)`, no judgements, no beliefs, no scenarios. Evidence from a failed
boundary is never partially used, and the single reason code deliberately does not describe how the forgery
was built.

### Scoring is a heuristic, not a probability

    score = (prior + weighted_support) / (1 + weighted_support + weighted_counter)

Each admitted item contributes a fixed weight by evidence quality — `VERIFIED` 1.0, `OBSERVED_ONLY` 0.6,
`UNKNOWN` 0.3 — and only assertions on the hypothesis' exact `claim_key` count. An assertion whose stance
equals the hypothesis' stance supports it; the opposite stance counters it. The form is bounded in `[0, 1]`,
returns the prior when no admitted evidence bears on the claim, and is rounded to twelve decimal places for
checksum stability. It is a documented, versioned heuristic labelled `THINK_HEURISTIC_V1`. **It is not an
empirically calibrated probability** and must not be read as a frequency, an edge or an expectancy.

The weights and the label are literals inside the evaluator rather than module attributes: a module
attribute is rebindable by any importer, which would silently change scores, calibration labels and
checksums for identical explicit inputs.

### Falsification is machine-checkable

Every `Scenario` carries at least one typed `InvalidationCondition(condition_id, claim_key,
trigger_stance)`. Admitted evidence taking `trigger_stance` on `claim_key` breaks the scenario, so
falsification is decidable by code rather than by reading prose. Empty conditions, a non-tuple collection,
non-string condition ids or claim keys, truthy lookalikes such as `1`, repeated condition ids and
non-`Stance` triggers all fail closed at construction.

### Validity and determinism

A belief's `valid_until` is derived only from admitted evidence and the explicit SENSE horizon: the earliest
`observed_time` among the items that actually contributed, plus `freshness_limit`. With no contributing
evidence it collapses to `as_of`. Rejected evidence never reaches a score, an evidence id, a scenario, an
invalidation condition or a horizon.

SENSE admits any non-negative `freshness_limit`, so that sum can exceed the largest representable instant.
The horizon then saturates at `datetime.max` in UTC instead of raising: a valid SENSE result always stays
consumable, and the horizon is still evidence-derived and never earlier than the observation it extends.

Every instant is normalised to UTC before any ordering, future, freshness, validity or deterministic-sort
comparison — in the contracts, in SENSE and in THINK. Python compares two aware datetimes that share one
`tzinfo` by wall time, so during a DST fold an absolutely later observation would otherwise look knowable.
THINK checksums canonicalise timestamps and collection ordering, so permuting hypotheses or evidence, or
spelling the same instants in another timezone, yields identical ordering, scores, calibration labels and
checksums.

### Research-only limits

THINK performs no I/O, reads no clock, calls no provider and has no side effect. It represents no order,
allocation, sizing, leverage, routing or execution authority, and `atp.brain.think` is covered by the
import-graph proof that the brain loads no broker, execution, live, risk, runtime or service module.
