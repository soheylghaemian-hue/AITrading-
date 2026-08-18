# Trader Brain — SENSE (research only)

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
