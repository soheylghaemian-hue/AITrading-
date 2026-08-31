# Trader Brain — SENSE, THINK, PROVE and LEARN (research only)

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

A directly constructed `SenseResult` whose content is exactly what canonical SENSE admission would have
produced stays admissible to THINK and LEARN. Both consumers trust the complete, revalidated value rather
than an unverifiable claim about which Python call created the object. Malformed or noncanonical values still
fail closed before any score or transition is produced.

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

## PROVE — complete, embargoed, cost-aware walk-forward evidence

`evaluate_prove(proposal, windows=..., manifest=..., observations=..., as_of=..., embargo=...)` grades one
`BrainProposal` against outcomes that were declared *before* the evaluation windows opened. It is an offline
harness: no persistence, no research provider, no live data, no risk or execution integration. It answers a
single question — *does this predeclared, complete, embargoed and cost-charged record support the proposal?*
— and it either returns a proven audit record or one stable failure reason with no metrics at all.

### Closed windows, rolling folds and the embargo

An `EvaluationWindow(window_id, role, start, end)` is a **closed interval** `[start, end]` over two
timezone-aware instants with `start < end`. Window ids are unique, the `role` is an explicit `WindowRole`
(`TRAINING` or `EVALUATION`), and windows are canonically ordered by `(start, end, window_id)` before any
check, so the caller's tuple order is never load-bearing.

Because the intervals are closed, adjacent windows must be strictly separated: `next.start > previous.end`.
Overlapping *and* abutting schedules both fail with `WINDOW_OVERLAP`, since sharing an endpoint means
sharing an instant of evidence.

A rolling walk-forward schedule such as `train-1 / eval-1 / train-2 / eval-2` is the normal case. Each
`EVALUATION` window is validated against **its own preceding `TRAINING` fold** — the nearest training window
that ends before that evaluation starts — never against every training fold in the schedule. An evaluation
with no earlier training fold fails with `MISSING_TRAINING_FOLD`, and every pair must be separated by at
least the caller's non-negative `embargo` (`INSUFFICIENT_EMBARGO`).

### Prior declaration is required

| Requirement | Reason on failure |
| --- | --- |
| the proposal's `action` is `STUDY` or `SHADOW` | `INELIGIBLE_ACTION` |
| `proposal.created_at` is **strictly** earlier than every evaluation start | `PROPOSAL_NOT_PRIOR` |
| `manifest.declared_at` is **strictly** earlier than every evaluation start | `MANIFEST_NOT_PRIOR` |

A proposal created *exactly at* an evaluation start is rejected: an instant that coincides with the first
graded instant does not prove the proposal existed before the evidence it is graded on.

### The manifest binds the outcome set

An `OutcomeManifest(manifest_id, declared_at, proposal_id, proposal_identity, expectations)` is the
predeclared list of every outcome that will be graded. It must be non-empty (`EMPTY_MANIFEST`), it must name
the exact proposal — both `proposal_id` and the canonical `proposal_identity(proposal)` digest, else
`MANIFEST_PROPOSAL_MISMATCH` — and every `ExpectedOutcome(observation_id, window_id)` must reference a
declared `EVALUATION` window (`UNKNOWN_WINDOW_REFERENCE`). Every evaluation window in the schedule must be
named by at least one expectation (`UNDECLARED_EVALUATION_WINDOW`), so a fold cannot be quietly excluded.

The supplied observations must then match the manifest exactly: one observation per expected id, no more and
no fewer.

| Condition | Reason |
| --- | --- |
| an expected id has no observation | `MISSING_OUTCOME` |
| an observation was never declared | `UNDECLARED_OUTCOME` |
| an observation id appears twice | `DUPLICATE_OUTCOME_ID` |
| an expectation id appears twice | `DUPLICATE_EXPECTED_OUTCOME` |
| an observation names a different window than its expectation | `OUTCOME_WINDOW_MISMATCH` |
| the outcome instant falls outside its closed window | `OUTCOME_OUTSIDE_WINDOW` |
| `available_time` precedes `outcome_time` | `OUTCOME_TIMESTAMPS_OUT_OF_ORDER` |
| `available_time` is later than `as_of` | `OUTCOME_NOT_AVAILABLE_AT_AS_OF` |

An empty outcome set, a retrospective manifest, a manifest bound to an unrelated proposal, and a
cherry-picked subset that silently drops a loss are therefore all unprovable.

### Costs, delays and abstentions are explicit

An `OutcomeObservation` carries `gross_return`, a non-negative `cost`, a non-negative `delay` and an
explicit `abstained` flag. Nothing is implied or defaulted from another field. An abstention is a **complete
declared outcome** — it still counts toward `expected_count` — but it contributes no graded return and no
cost, so it must declare `gross_return == 0.0` and `cost == 0.0` (`INCONSISTENT_ABSTENTION`).

Metrics are computed only from the complete canonical observation set and reconcile exactly:

* `graded_count + abstention_count == expected_count`, per window and in aggregate;
* `net_return == gross_return - costs`, per window and in aggregate;
* the aggregate counts, gross return, costs and total delay equal the per-window sums taken in canonical
  window order;
* a window with no graded outcome carries neither return nor cost.

### Audit objects revalidate instead of trusting construction

`WindowMetrics`, `AggregateMetrics`, `ProveInputs` and `ProveResult` are public frozen dataclasses, and a
frozen dataclass is still mutable through `object.__setattr__`. Every one of them re-proves its invariants
at construction, and `ProveResult` re-proves them again on each `checksum()`.

A proven `ProveResult` carries the accepted `ProveInputs` themselves, so it is self-contained: it rebinds
and regrades those inputs and requires the stored per-window and aggregate metrics to match exactly
(`INCONSISTENT_METRICS`). `proposal_identity` and `input_identity` are **derived properties**, never stored
fields — an unrelated caller-supplied digest can never be recorded as a trusted fact. A failed result
carries no inputs, no windows and no aggregate, so partial metrics from a rejected boundary cannot exist.

The protocol metadata is held to the same standard as the numbers. `calibration` must equal this module's
own `PROVE_WALK_FORWARD_V1` label, so an arbitrary or rewritten label is refused rather than recorded
alongside otherwise valid metrics; and a refusal must name **exactly one** stable reason, never several and
never the same reason twice. Rewriting either after construction fails the same way, because `checksum()`
re-proves them before it signs anything.

The proposal's canonical identity is derived inside PROVE from the exact `BrainProposal` fields; it never
calls the proposal's own `checksum()`, which a subclass could override. `type(proposal) is BrainProposal` is
checked **before any attribute access**, so a subclass is refused with `INVALID_PROPOSAL` without executing
any overridden `__getattribute__`, property or method. An exact but tampered proposal is revalidated field
by field and re-digested, so a mutated field either fails closed or visibly changes the identity the
manifest is bound to.

### One canonical serializer

A single exhaustive encoder covers the supported schema and rejects everything else. Aware datetimes are
normalised to UTC, `timedelta` values are encoded as their exact `(days, seconds, microseconds)` triple,
finite floats are encoded losslessly with `float.hex()`, integers are encoded as exact decimal text, and
every encoded value is tagged with its type so `1`, `"1"` and `True` can never collide. Semantic sets —
windows, expectations, observations, required evidence, scenarios and the invalidation conditions inside a
scenario — are sorted by canonical form before both evaluation and hashing, so the caller's iteration order
binds nothing: listing the same two falsifiers in the other order leaves `proposal_identity`, the manifest
binding and the proof checksum unchanged.

Nothing is rounded, no value is collapsed to a token, and there is no `repr`/`str` fallback. An unsupported
type, a non-finite float, or a structure nested deeper than the schema allows is **rejected**, not
approximated. Consequently adjacent floats, delays differing by one microsecond, and every other distinct
accepted value produce distinct `input_identity` and result checksums, while equivalent observation
permutations and timezone spellings of the same instants produce identical ones.

### Purity and repeatability

`evaluate_prove` reads no clock, performs no I/O, calls no provider, touches no shared or rebindable module
calibration, and has no side effect. The calibration label `PROVE_WALK_FORWARD_V1` is a literal inside the
evaluator for the same reason THINK's weights are, and it is spelled as a literal again where the audit
record re-proves it, so no importer can rebind a shared name and move both at once.

The boundary accepts only exact built-in and schema types: `windows` and `observations` must be immutable
tuples. A list, a generator or any other iterable is refused with a stable reason rather than materialised,
so a result can never depend on how far a shared iterator had already been consumed. Evaluating the same
accepted inputs twice yields equal results and identical checksums.

Ordinary exceptions raised while validating or binding hostile explicit input — an enormous integer smuggled
into a numeric field, a `tzinfo` whose `utcoffset` raises — are caught and returned as a deterministic
failed `ProveResult` with no usable metrics. Malformed input is never partially graded.

### Research-only limits

PROVE represents no order, allocation, sizing, leverage, routing or execution authority. `atp.brain.prove`
is covered by the same subprocess import-graph proof as the rest of the brain: importing it loads no broker,
execution, live, risk, runtime or service module. That proof is executable — it inspects real imports, not
the wording of any docstring.

## LEARN — drift, champion–challenger evidence and reversible transitions

`atp.brain.learn` answers three research questions and nothing else: *has the regime this model was built
for drifted?*, *does complete walk-forward proof prefer the challenger or the champion?*, and *is this
retirement evidence-backed and exactly reversible?* LEARN represents no order, allocation, sizing,
execution, deployment, promotion or risk-relaxation authority. Every evaluator is pure: no clock, no I/O, no
provider, no persistence, no shared mutable module state and no side effect.

### Public inventory

| Export | Kind | Purpose |
| --- | --- | --- |
| `ModelRole` | enum | `CHAMPION`, `CHALLENGER`, `RETIRED` — the only roles a `ModelRecord` may hold |
| `ModelRecord` | dataclass | `model_id`, `proposal_id`, `role`, `registered_at` |
| `ProofSummary` | dataclass | the canonical, immutable snapshot LEARN keeps of one accepted `ProveResult` |
| `ComparisonPreference` | enum | `CHAMPION`, `CHALLENGER`, `INCONCLUSIVE` — research evidence only, never a promotion |
| `RetirementGround` | enum | `DRIFT_ABSTENTION`, `INFERIOR_COMPARISON` — the only admissible retirement grounds |
| `DriftInputs` / `DriftResult` / `DriftFailure` | dataclass, dataclass, enum | drift assessment inputs, result and stable reasons |
| `ComparisonInputs` / `ComparisonResult` / `ComparisonFailure` | dataclass, dataclass, enum | champion–challenger inputs, result and stable reasons |
| `RetirementInputs` / `RetirementResult` | dataclass | retirement inputs and the reversible transition record |
| `ReinstatementInputs` / `ReinstatementResult` | dataclass | reinstatement inputs and the reversal record |
| `TransitionFailure` | enum | the stable reasons shared by retirement and reinstatement |
| `evaluate_drift` | function | `evaluate_drift(model, evidence, claim_key=..., prior_confidence=..., abstention_threshold=..., as_of=...)` |
| `evaluate_comparison` | function | `evaluate_comparison(champion, challenger, champion_proof=..., challenger_proof=..., as_of=...)` |
| `evaluate_retirement` | function | `evaluate_retirement(model, retirement_id=..., as_of=..., drift=..., comparison=...)` |
| `evaluate_reinstatement` | function | `evaluate_reinstatement(retirement, reversal_id=..., as_of=...)` |

No other name is exported, and no exported object carries a method or field that could place an order, size
a position, allocate capital, deploy a model, promote a challenger or relax a risk limit.

### LEARN re-proves the complete SENSE value

LEARN revalidates every `SenseResult` field, every `Evidence` item's own invariants, and a full re-run of
canonical SENSE admission over the union of the represented usable and rejected evidence. The supplied
partitions, reasons, ordering and contradictions must match that reconstruction exactly. A canonical direct
reconstruction is the same research value; future, stale, duplicate-id, out-of-order or internally
inconsistent evidence fails closed before it can affect confidence or a transition. The drift `as_of` must
equal the `SenseResult`'s own `as_of`, so one admission value is never evaluated against another instant.

### Drift can only lower confidence or force abstention

Only usable evidence asserting the exact `claim_key` bears on drift. `REFUTES` is regime breakage,
`SUPPORTS` is regime persistence, and each item contributes the same fixed quality weight THINK uses
(`VERIFIED` 1.0, `OBSERVED_ONLY` 0.6, `UNKNOWN` 0.3). The versioned heuristic is labelled
`LEARN_DRIFT_V1`:

    drift_score = weighted_refuting / (weighted_supporting + weighted_refuting)
    posterior_confidence = prior_confidence * (1 - drift_score)

`posterior_confidence` is never above `prior_confidence`, and `abstain` is true only when bearing evidence
exists **and** the posterior falls to or below the caller's `abstention_threshold`. Empty usable evidence
can never manufacture drift, abstention or retirement grounds. With no bearing evidence the drift score is
`0.0`, the posterior equals the prior, and `abstain` is false regardless of how low the prior or how high
the threshold is. A model registered after the evaluation as-of instant can never produce actionable drift.
Such a model is refused with `UNKNOWABLE_MODEL` before any evidence is scored.

| `DriftFailure` | Condition |
| --- | --- |
| `INVALID_INPUT` | the inputs shell, `claim_key`, `prior_confidence`, `abstention_threshold` or `as_of` is not an exact, in-range value |
| `INVALID_MODEL` | the model is not an exact, completely formed `ModelRecord` |
| `INVALID_SENSE_RESULT` | the evidence is not a canonical `SenseResult` for this exact `as_of` |
| `UNKNOWABLE_MODEL` | `model.registered_at` is later than `as_of` |

Drift phases run in that fixed order: inputs shell, model shape, explicit scalars, SENSE boundary, then
point-in-time knowability.

### Comparison is evidence, never promotion

`evaluate_comparison` binds one `CHAMPION` record and one `CHALLENGER` record to their own accepted
`ProveResult`s and reports a `ComparisonPreference` derived only from the aggregate net return of the two
proofs. Every proof must grade exactly the `proposal_id` its `ModelRecord` names. A preference is research
evidence only, and it confers no promotion authority. Nothing in `ComparisonResult` can change a role.

Validation phases are fixed and symmetric across the two sides:

1. `INVALID_INPUT` — the inputs shell or `as_of`.
2. `INVALID_MODEL` — either record is not an exact, completely formed `ModelRecord`; exact shells with
   missing slots are refused here, never leaked as an exception.
3. `INVALID_PROOF` — either proof is not an exact `ProveResult` that re-proves itself.
4. `PROOF_NOT_PROVEN` — either proof is a refusal rather than a proven record.
5. `PROOF_MODEL_MISMATCH` — either proof grades a different `proposal_id` than its own record names.
6. `ROLE_MISMATCH` — the champion side is not `CHAMPION`, or the challenger side is not `CHALLENGER`.
7. `SELF_COMPARISON` — both sides carry the correct roles but the same `model_id` or the same `proposal_id`.
8. `UNKNOWABLE_EVIDENCE` — a record was registered, or a proof was evaluated, after the comparison `as_of`;
   champion model, challenger model, champion proof, challenger proof, in that order.

Role checking precedes identity checking, so a wrong-role pair with distinct identities is always
`ROLE_MISMATCH` and a correctly rolled pair with one shared identity is always `SELF_COMPARISON`.
Proof-to-model binding precedes knowability, so a future-registered model holding a wrong-proposal proof is
`PROOF_MODEL_MISMATCH`.

### Retirement is evidence-backed, reinstatement is exact

`evaluate_retirement` accepts a target `ModelRecord` plus at least one accepted evidence result. Drift
evidence counts only when it names the target model and its `abstain` is true (`DRIFT_ABSTENTION`).
Comparison evidence counts only when its champion side is the target model and its preference is
`CHALLENGER` (`INFERIOR_COMPARISON`). Evidence dated after the retirement instant is never grounds.

| `TransitionFailure` | Condition |
| --- | --- |
| `INVALID_INPUT` | the inputs shell, `retirement_id`, `reversal_id` or `as_of` is not an exact value |
| `INVALID_MODEL` | the target is not an exact, completely formed `ModelRecord` |
| `MODEL_ALREADY_RETIRED` | the target's role is already `RETIRED` |
| `INVALID_DRIFT` | the supplied drift evidence is not an accepted, fully reconciling `DriftResult` |
| `INVALID_COMPARISON` | the supplied comparison evidence is not an accepted, fully reconciling `ComparisonResult` |
| `EVIDENCE_MODEL_MISMATCH` | the evidence does not name the exact target record |
| `INSUFFICIENT_EVIDENCE` | no evidence was supplied, or none of it establishes an admissible ground |
| `INVALID_RETIREMENT` | the reinstatement input is not an accepted, fully reconciling `RetirementResult` |
| `RETIREMENT_NOT_PRIOR` | the reinstatement `as_of` is not strictly later than `retired_at` |

After the explicit retirement id and instant are validated, `MODEL_ALREADY_RETIRED` is checked before any
evidence binding, so it is reachable for every already-retired target regardless of what evidence accompanies it.

`evaluate_reinstatement` binds the exact retirement it reverses: the retirement checksum, the retirement id,
the retired model and proposal identities, the complete canonical evidence that justified it, the reversal
id and the reinstatement instant. Reinstatement restores exactly the role the model held before its
retirement. A challenger can never be promoted to champion. Two genuine retirements that differ only in the
evidence behind them therefore produce different reinstatement results and different checksums, because the
reinstatement state embeds the whole retirement state.

### Nested refusals never leak another evaluator's reason

A failed, tampered, non-reconciling or malformed nested result is translated into the caller's own typed refusal —
`INVALID_DRIFT`, `INVALID_COMPARISON` or `INVALID_RETIREMENT` — carrying exactly one reason, no inputs, no
metrics and no evidence ids. No `DriftFailure` or `ComparisonFailure` value ever appears inside a
`TransitionFailure` tuple, and constructing the refusal never raises.

### Canonical equality, checksums and stateless replay

Every LEARN result derives both its equality and its `checksum()` from one complete canonical state: UTC
instants, canonically sorted semantic collections (including the assertions inside each evidence item),
lossless `float.hex()` encoding with equal signed zeros normalised to `0.0`, exact decimal integers and
type-tagged values. Equivalent inputs produce equal results and identical checksums. Permuted evidence,
permuted assertions and alternative timezone spellings of the same instants are equivalent; distinct
accepted values, adjacent floats and different model, proposal, evidence or transition identities are not.

Replaying an evaluator on identical inputs is stateless and reproduces the same result. LEARN records no
consumption and makes no exactly-once promise: calling `evaluate_reinstatement` twice with the same
retirement, reversal id and instant returns two equal results with identical checksums.

### Complete value revalidation, not object-origin authentication

Every accepted `DriftResult`, `ComparisonResult`, `RetirementResult` and `ReinstatementResult` rebinds its
complete canonical inputs and recomputes every derived output on construction, equality, checksumming and
downstream consumption. A direct reconstruction, copy or serialization round-trip that represents exactly
the same value is therefore equal and has the same checksum. An incomplete shell, malformed nested value or
stored metric, preference, ground, role, id or timestamp that does not match recomputation is refused.

This module deliberately makes no claim that it can authenticate which Python function created an object.
Code with arbitrary reflection inside the same process can inspect or rewrite any Python-held token, ledger
or module global; treating one as a security boundary would be false assurance. LEARN instead provides a
pure, deterministic research-value contract. It has no deployment or execution authority, and callers that
need cryptographic origin authentication must add a separate privileged signing boundary outside this
module.

### Research-only limits

LEARN performs no I/O, reads no clock, calls no provider, opens no connection and writes nothing.
`atp.brain.learn` is covered by the same executable import-graph proof as the rest of the brain: neither its
declared imports — absolute, relative or deferred inside a function body — nor the transitive graph actually
loaded when it is imported may reach a broker, execution, live, risk, runtime or service module.
