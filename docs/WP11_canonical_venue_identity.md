# WP11 — Canonical Venue & Instrument Identity Resolution

Read-only reference-data work. **No trading, no orders, no execution, no market-data, no autonomous
activity.** `AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0` throughout. Builds on WP10
(`docs/WP10_ibkr_venue_resolution.md`).

## 0. Goal

Resolve the **17 `ERROR_RETRYABLE`** (11 cash "venue_unresolved", 6 derivatives "venue_unresolved") and the
**3 `NOT_TRADABLE`** (bonds) left by the second WP10 qualification canary (run `cb7a8800…`), and enable the
**first safe `VERIFIED`** results — *without guessing venue or identity data*.

## 1. Root cause — why the 17 were stuck (and the 3 bonds were falsely terminal)

The decisive primary-source facts (all cited in the WP11 investigation; ISO 10383 Aug-2026 registry, ESMA
RTS 23, IBKR TWS API reference):

1. **A FIRDS trading-venue MIC is not a primary listing.** RTS 23 Field 6 (*"Trading venue"*) is *"Segment
   MIC for the trading venue or systematic internaliser, where available, otherwise operating MIC"* — it
   denotes **any** reporting venue/SI, and FIRDS carries **no** primary-listing field. ISO 10383 attaches no
   listing-primacy to a MIC. The 20 canary MICs are, verbatim from the ISO registry, **MTF segments,
   Systematic Internalisers, OTFs and derivatives segments** — e.g. `AACA` = *Crédit Agricole CIB* (SI),
   `BTFE` = *Bloomberg Trading Facility* (MTF), `BGEM` = *Borsa Italiana Global Equity Market* (MTF segment
   of `XMIL`), the 6 derivative MICs = Nasdaq Stockholm / Euronext Brussels / D2X derivatives segments.
   **None is a cash primary listing.**
2. **WP10's cash verification required a FIRDS-MIC → IBKR-venue match.** `_consistent` intersected
   `resolve_ibkr_exchanges(<FIRDS MIC>)` with the returned IBKR venue. For an MTF/SI MIC that is *(a)*
   absent from the registry and *(b)* not a listing venue at all, that intersection is **empty by
   construction** → the returned contract is never consistent → `NOT_TRADABLE`-with-candidates →
   `ERROR_RETRYABLE(venue_unresolved)`. The gate was not just unmet, it was **semantically invalid** for
   these lines.
3. **The bond query was malformed.** IBKR resolves bonds by the **CUSIP/ISIN placed in `Contract.symbol`
   with `secType='BOND'`** — *not* `secIdType='ISIN'/secId`. WP10 used the ISIN path for bonds, so the 3
   bond lookups returned a genuine-empty *"no security definition"* and were recorded terminal
   `NOT_TRADABLE`. That verdict is a **query artifact**, not a tradability fact — and IBKR's bond universe /
   bond reference data are entitlement- and account-scoped, so a bond not-found is never proof of global
   untradability.
4. **The 6 derivatives never queried** — an unmapped derivative venue raises `VenueResolutionError` before
   any request (correct: no bad destination is ever sent).

No `con_id` was ever fabricated (all remained NULL).

## 2. New strategy — ISIN-anchored identity, IBKR's own returned venue

WP11 stops trying to equate a FIRDS MIC with a primary listing. For **cash** it verifies on IBKR's own
reply, fail-closed:

- **Discovery** is unchanged (`secType`, `secIdType='ISIN'`, `secId=<isin>`, `exchange='SMART'`), with one
  change: **currency is omitted from the cash ISIN-discovery query** and applied as a Python-side constraint,
  so a listing that exists only in another currency is **observed** (→ non-terminal `currency_conflict`)
  instead of collapsing to an empty result / false `NOT_TRADABLE`.
- **Capture what WP10 discarded**: `contract_detail_to_global` now reads the ISIN IBKR **echoes** in
  `ContractDetails.secIdList` (case-insensitive tag, normalised value) into `GlobalContract.isin`. Fail-
  closed: the tag/casing is undocumented and (for US stocks) subscription-gated, so an absent echo yields
  `""` and is **never assumed**.
- **Verification (`_consistent`)** requires `con_id>0`, asset-class compatibility, `currency ==`, **and a
  POSITIVE identity anchor beyond the ISIN search key** — either:
  - **(A) ISIN echo** — IBKR echoed the instrument's exact ISIN **and** returned a real (non-routing) venue
    (recorded as the venue of record); or
  - **(B) venue match** — the returned real venue intersects the registry translation of the FIRDS MIC (the
    WP10 anchor; only possible when the MIC maps to an IBKR code).

  **Verifying on the search key alone (echo absent *and* MIC unmapped) is forbidden** — it is not fail-
  closed and it is the exact false-verify the adversarial design review caught. An ISIN echo that is
  **present but different** is a hard identity conflict (never consistent, never rescued by a venue match).
- **Uniqueness / multiple listings**: `match_contract` groups by `conId`; exactly one consistent conId →
  `VERIFIED`; more than one → `AMBIGUOUS`; a returned-but-none-consistent set is re-examined (below).
- **What is stored on a cash `VERIFIED`**: IBKR's returned `conId`, and IBKR's returned **real
  `primaryExchange`** in a **new `ibkr_primary_exchange` column** — never `SMART`, never the FIRDS MIC. The
  FIRDS-MIC `primary_exchange` is **left untouched** so `resolve_ibkr_exchanges` provenance survives a re-run
  (idempotency).
- **`SMART` is only ever a router.** A reply whose only venue is `SMART`/blank has no real venue → cannot be
  `VERIFIED` (fail-closed, area 3).

**Bonds**: the query is fixed to `Contract(secType='BOND', symbol=<isin>, exchange='SMART', currency=…)`. A
well-formed bond not-found is reclassified to a re-queryable, budget-neutral `ERROR_RETRYABLE`
(`bond_not_found`) — **never** terminal `NOT_TRADABLE` (a contract-details miss cannot assert untradability;
the bond universe is entitlement/account-scoped).

**Derivatives**: unchanged and fail-closed. A derivative requires a concrete, provenance-grounded IBKR
exchange plus full identity (expiry + strike + right + multiplier + underlying). The 6 canary derivative
MICs have **no primary-source IBKR exchange code**, so they remain `ERROR_RETRYABLE(venue_unresolved)` — no
guess is ever made to inflate coverage.

**Error 200**: WP11 also recognises IBKR's *other* documented 200 message — *"…is ambiguous"* — and maps it
to `AMBIGUOUS` (a more-specific query is needed), instead of looping forever as `venue_unresolved`.

## 3. Fail-closed reclassification of a returned-but-inconsistent result (`_qualify_one`)

When the matcher would return terminal `NOT_TRADABLE`, WP11 re-examines it before it stands:

| Situation | Outcome | Terminal? |
|---|---|---|
| Empty, well-formed **bond** lookup | `ERROR_RETRYABLE` `bond_not_found` (budget-neutral) | no |
| Empty, well-formed **cash** lookup | `NOT_TRADABLE` (a genuine ISIN not-found) | yes |
| Candidates returned, **none in the requested currency** | `ERROR_RETRYABLE` `currency_conflict` | no |
| Candidates returned, **unmapped MIC**, none consistent | `ERROR_RETRYABLE` `venue_unresolved` (WP10) | no |
| Candidates returned, **mapped MIC**, none consistent | `NOT_TRADABLE` (genuine identity mismatch) | yes |

## 4. Registry (`ibkr_venue.py`) — provenance only; unknown stays unresolved

- **`VenueMapping` now carries `operating_mic` and `venue_category`** so the four namespaces (FIRDS MIC /
  operating MIC / IBKR exchange / category) are explicit in the data.
- **New primary mappings, grounded in the repo's own `marketdata/universe.py` (REPO provenance):**
  `XMAD→BM`, `XTKS→TSEJ`, `XASX→ASX`, `XSES→SGX`. Refined `XASE→NYSEAMER` (the repo's US parsers emit
  `NYSEAMER`, not the legacy `AMEX`). Upgraded `XNYS/BATS/IEXG/XCBO` to REPO confidence (used by
  `listing_sources`/`directories`/`brokers`).
- **`_NON_PRIMARY_VENUES`** records the canary MTF/SI/OTF/derivative MICs (with their ISO operating MIC and
  category) so the system **recognises** a non-primary venue and refuses to treat it as a listing. It carries
  **no IBKR exchange** — `resolve_ibkr_exchanges` stays fail-closed for every one of them. Helpers:
  `venue_category()`, `operating_mic()`, `is_non_primary_venue()`.
- **Derivative venues are deliberately NOT mapped** — no primary-source IBKR code exists for them; mapping a
  guess would be exactly the error WP11 exists to prevent.

## 5. Status model — analysis, what ships, and the deferred migration-capable proposal

**Can the existing 8 statuses express WP11's outcomes?** `VERIFIED` (ISIN-confirmed) and `AMBIGUOUS`: yes.
The genuinely new distinctions — *venue-unresolved*, *bond-not-in-universe*, *currency-conflict* — are all
**re-queryable, budget-neutral `ERROR_RETRYABLE`**, which is the correct fail-closed posture (never a false
terminal). So no new status *value* is required for correctness. What the model cannot do is express these
distinctly **at the status level** for operator routing/reporting.

**What WP11 SHIPS (additive, backward-compatible, safe on both dialects — `_migration_032`):**
- `qualification_detail TEXT` (nullable) — a machine-readable sub-classification with a closed vocabulary
  (`verified_isin_echo`, `verified_venue_match`, `ambiguous`, `currency_conflict`, `bond_not_found`,
  `venue_unresolved`, `not_found`). Refines — never replaces — `qualification_status`.
- `ibkr_primary_exchange TEXT` (nullable) — IBKR's returned real venue for a `VERIFIED` contract, stored
  separately from the FIRDS-MIC `primary_exchange`.

`ALTER TABLE instruments ADD COLUMN … TEXT` is a metadata-only change on Postgres **and** SQLite; it performs
no DROP/CREATE/RENAME, so the FK children of `instruments` are untouched and `PRAGMA foreign_keys` never needs
toggling. **No** `qualification_status` CHECK change, **no** run `*_count` column, **no** enum / counter
lock-step. Test-verified on a fresh SQLite DB (`test_migration_032_columns_present_and_writable`,
`test_migrations_18_19_apply_sequentially_after_1_17`).

**What WP11 PROPOSES but DEFERS — first-class status values (needs a future authorization + a migrator
enhancement):** promote `VENUE_UNRESOLVED` and `NOT_IN_BROKER_UNIVERSE` to `qualification_status` values.

- **Postgres (clean):**
  ```sql
  ALTER TABLE instruments DROP CONSTRAINT IF EXISTS instruments_qualification_status_check;
  ALTER TABLE instruments ADD CONSTRAINT instruments_qualification_status_check
    CHECK (qualification_status IN ( …8 existing…, 'VENUE_UNRESOLVED','NOT_IN_BROKER_UNIVERSE'));
  ```
  (The inline unnamed CHECK from `_migration_027` is auto-named `instruments_qualification_status_check`.)
- **SQLite (BLOCKED as-is):** SQLite has no `DROP CHECK`; the only precedent (`_migration_025`) rebuilds the
  table, but `instruments` is the FK parent of `md_quotes_current`, `md_quote_history`, `md_bars`,
  `md_corporate_actions`, `md_provider_entitlements`, `news_message_instruments`,
  `fundamental_series_instruments`, and `PRAGMA foreign_keys` is a **no-op inside a transaction** — which is
  where `Migrator.apply` runs every migration. A rebuild would violate/rewrite child rows on any populated
  SQLite (and would pass on the empty CI DB — a false-green trap). **Prerequisite:** extend `Migrator` to run
  `PRAGMA foreign_keys=OFF` / `PRAGMA foreign_key_check` *around* (not inside) a rebuild-class migration.
- **Full lock-step for the deferred change** (all must land together): `_migration_03x` (Postgres CHECK-widen
  + `ALTER TABLE instrument_qualification_runs ADD COLUMN venue_unresolved_count/not_in_broker_universe_count`
  — that table has no FK children, so it is safe); `QualificationStatus` enum; `SELECTABLE_STATUSES`;
  `_outcome_fields` (both new statuses map to `(None,None,None,None,False)` — **never** `tradability_status=
  'not_tradable'`); `InstrumentQualificationRunRow` + `_IQ_RUN_COLS` + `iq_create_run` placeholder count;
  `iq_finalize_run` + `iq_reclaim_stale_running` counter derivation; `QualificationSummary`/`_summary`; and
  `test_status_values_match_the_spec` (which pins the exact 8-value list). Prod is Postgres-only, so this
  cannot be validated locally without DB access — hence deferred under the "kein DB-Zugriff" constraint.

## 6. Test results

Run in a `.[dev,live]` environment (offline; no network/service tests):
- **Full backend suite: green** (0 failed) — one migration-count test was updated to expect migration 32.
- **Safety gate** (`tests/test_autopilot*.py` + `tests/test_brain_contracts.py`): pass.
- **Import-graph gates** (incl. WP3 qualification): pass; `ibkr_venue`/`qualification`/`ibkr_catalog` pull no
  broker SDK (`ib_async` stays lazy), form no cycle.
- **New `tests/test_ibkr_canonical_identity_wp11.py` (20 offline regression tests)**: secIdList capture;
  ISIN-echo verify on an unmapped MIC; echo mismatch; echo-absent + unmapped MIC never verifies ("no false
  verification"); venue-match still verifies; SMART-only never verifies; multiple listings → AMBIGUOUS;
  currency deviation → ERROR_RETRYABLE(currency_conflict); genuine not-found stays NOT_TRADABLE; ambiguous-200
  → AMBIGUOUS; bond query construction; bond not-found → ERROR_RETRYABLE(bond_not_found); complete/incomplete
  derivative identity; unmapped derivative stays venue_unresolved with no query; new registry mappings;
  venue-category metadata + fail-closed; migration_032 columns; end-to-end ISIN-echo verify records
  detail+venue and preserves the FIRDS MIC; conId collision → AMBIGUOUS (never a crash). The WP10
  `_build_contract` cash test was updated (bonds now have a distinct query).

## 7. Safety

Read-only reference data only. **No order, market-data, account, position, scanner, subscription or
execution path is added** (verified against the diff and the import-graph gate). No IBKR connection was made;
production, the gateway session and the DB are unchanged. No push/PR/deploy in the code itself.

## 8. Separate correction plan for the 20 canary rows (no production data changed)

**WP11 changes no production data.** This is the *plan*; each step needs its own authorization. The 20
instruments are, in `atp_prod` after the WP10 re-canary reset + re-canary (run `cb7a8800…`): **11 cash + 6
derivatives = 17 `ERROR_RETRYABLE` (`con_id` NULL)** and **3 bonds `NOT_TRADABLE` (`con_id` NULL)**.

**What each state needs to be re-qualified by the deployed WP11 code:**
- The 17 `ERROR_RETRYABLE` are **already re-selectable** (`ERROR_RETRYABLE ∈ SELECTABLE_STATUSES`) — a bounded
  re-qualification pass picks them up with **no DB reset at all**.
- The 3 bonds are terminal `NOT_TRADABLE` (not selectable). They need a **bounded reset to `DISCOVERED`** —
  identical in shape to the WP10 correction (one guarded transaction, `WHERE qualification_run_id='cb7a8800…'
  AND qualification_status='NOT_TRADABLE' AND con_id IS NULL`, expect exactly 3, resetting only
  `qualification_status/reason/run_id/last_qualified_at/tradability_status/verification_status`, run/event
  audit history retained).

**Preconditions (each separately authorized):**
1. WP11 merged to `main` **and deployed** first, so re-qualification runs the fixed path (deploy ordering:
   `_migration_032` is additive and applies automatically on startup before any code writes the new columns —
   no manual sequencing needed for the shipped change).
2. A **fresh, verified** `atp_prod` backup (size, SHA-256, `gzip -t`, `pg_restore --list`) immediately before.
3. Gateway connected, `4002` open, a conflict-free Client-ID; trading/execution disabled; no
   market-data/orders/scanner/subscription; strictly `reqContractDetailsAsync` only.

**Expected outcome of the re-qualification (offline-unverifiable; depends on IBKR's reply):**
- **11 cash** → `VERIFIED` for each ISIN that resolves to a unique conId in the requested currency **with an
  ISIN echo or a mapped venue**; `AMBIGUOUS` for multiple listings; `ERROR_RETRYABLE(currency_conflict)` for a
  currency deviation; `ERROR_RETRYABLE(venue_unresolved)` if IBKR returns no ISIN echo and the MIC stays
  unmapped (honest, re-queryable — never a false verify).
- **3 bonds** → `ERROR_RETRYABLE(bond_not_found)` (re-queryable) unless an entitled `bondContractDetails`
  reply carries a conId+ISIN, in which case they verify under the same ISIN-anchored rule. The false terminal
  `NOT_TRADABLE` is removed.
- **6 derivatives** → remain `ERROR_RETRYABLE(venue_unresolved)` until a provenance-grounded IBKR derivative
  exchange is added to the registry (a separate, evidence-gated step). No guess.

**Post-checks:** instruments still 4,800; `con_id` stored only for uniquely ISIN-confirmed lines, each with a
recorded `ibkr_primary_exchange` (a real IBKR venue, never a MIC/SMART); `orders`/`fills`/`positions`/`md_*`
still 0; safety flags off; the historical run rows and event trail retained.
