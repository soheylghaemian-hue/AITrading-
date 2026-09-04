# WP10 — IBKR Venue & Contract Resolution Hardening

Read-only reference-data work. **No trading, no orders, no execution, no market-data, no autonomous
activity.** `AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0` throughout.

## 1. Root cause — why the canary marked 20 instruments `NOT_TRADABLE`

The WP3 read-only qualification canary qualified 20 FIRDS instruments and IBKR answered **error 200**:
*"The destination or exchange selected is Invalid"* (equity/ETF/fund/bond/warrant) and *"Invalid value in
field # 541"* (future/option). Every one was recorded `NOT_TRADABLE`. That verdict was **wrong** — the
requests were malformed, so IBKR never actually judged tradability. Two namespace bugs, plus a
classification bug, combined:

1. **ISIN put in `Contract.symbol`.** For a FIRDS row `symbol == isin == local_symbol` (e.g.
   `XS1877838877`); there is no ticker. IBKR's `symbol` field expects a ticker/local symbol, not an ISIN.
   (`qualification.py` old `_build_contract`, `ibkr_catalog.py` old `_ib_contract`.)
2. **FIRDS MIC sent as the IBKR exchange.** The ISO-10383 venue MIC (`XPAR`, `XETR`, `AFSO`, …) was put
   straight into IBKR's `exchange` (non-STK) or `primaryExchange` (STK/ETF). **IBKR uses its own exchange
   codes** (`SBF`, `IBIS`, `EBS`, `LSE`, `BVME`, `EUREX`, …) — the repo already knows these in
   `src/atp/marketdata/universe.py`, but nothing mapped a FIRDS MIC onto them. A MIC is not a valid IBKR
   destination → error 200.
3. **Empty result → `NOT_TRADABLE`, unconditionally.** `reqContractDetailsAsync` resolves *empty* on an
   error-200 and the error text was discarded, so `match_contract([])` recorded `NOT_TRADABLE` regardless of
   *why* it was empty. A venue/query-resolution failure was indistinguishable from a genuine not-found.
4. **Second-order:** even a *successful* lookup would fail verification — `_consistent` intersected the
   instrument's FIRDS MIC (`XPAR`) with the returned contract's IBKR code (`SBF`); different namespaces never
   intersect, so the row would have been `NOT_TRADABLE` anyway.

**Conclusion:** all 20 `NOT_TRADABLE` verdicts are venue/query-resolution artifacts, **not** evidence of
non-tradability. No `con_id` was fabricated (all remained NULL).

## 2. New query & matching strategy

- **ISIN-first discovery.** Build the query with `secType`, `currency`, `secIdType='ISIN'`, `secId=<isin>`,
  and `exchange='SMART'` (search/routing only). The FIRDS symbol (an ISIN) is **never** placed in
  `Contract.symbol`; the raw MIC is **never** sent. A real ticker on an already-IBKR venue (US listing
  sources emit `NASDAQ`/`ARCA`, not MICs) keeps its symbol query; a previously-resolved `conId` is used
  when present.
- **Fail-closed MIC→IBKR venue registry** (`src/atp/instruments/ibkr_venue.py`). An explicit, hand-curated,
  **provenance-tracked** table (never derived by string-munging a MIC). Its high-confidence rows are
  grounded in `universe.py` (the repo's own IBKR codes: `XETR→IBIS`, `XPAR→SBF`, `XSWX→EBS`, `XLON→LSE`,
  `XMIL→BVME`, `XSTO→SFB`, `XHEL→HEX`, `XWBO→VSE`, US `NASDAQ`/`ARCA`); the rest are standard IBKR codes
  marked `medium` pending validation against IBKR's authoritative exchange listing. **An unmapped MIC
  resolves to an empty result (fail-closed)** — never the raw MIC, never SMART-as-a-venue.
  - **Verification** translates the instrument's MIC to its IBKR code(s) before intersecting with the
    returned contract's venue, so a correct ISIN lookup on a mapped MIC can VERIFY. An unmapped MIC can
    never VERIFY on venue (fail-closed). When a contract IS returned but the venue is unconfirmable (MIC not
    in the registry), `_qualify_one` reclassifies the outcome to a **re-queryable `ERROR_RETRYABLE`
    (venue-unresolved, budget-neutral)** — never a false terminal `NOT_TRADABLE`. Only a well-formed ISIN
    query that genuinely returns *nothing* stays `NOT_TRADABLE` (a real not-found, not a venue gap). *(This
    reclassification closes an adversarial-review finding: without it, every cash instrument on an unmapped
    MIC whose ISIN resolved would have been wrongly marked terminal `NOT_TRADABLE`.)*
  - **Derivatives** need a concrete IBKR exchange (ISIN alone cannot resolve them); it comes from the
    registry. An unmapped derivative venue does not send a query at all — it raises `VenueResolutionError`.
- **`SMART` is only ever a search/routing token.** Verification still requires a UNIQUE returned contract
  consistent on real exchange (via the registry) + currency + asset class + full identity (conId,
  multiplier, expiry, strike, right for derivatives). `con_id` is stored only from that unique reply;
  several plausible → `AMBIGUOUS`; nothing invented.
- **Context-aware error 200.** `classify_contract_query_error` reads the captured error message:
  *invalid destination / invalid value in field* → `VenueResolutionError` (re-queryable, **not**
  `NOT_TRADABLE`); *no security definition has been found* (from a well-formed query) → falls through to the
  ordinary `NOT_TRADABLE`. An unattributable 200 is treated conservatively as venue-resolution (never a
  false `NOT_TRADABLE`).

## 3. Status model — reuse `ERROR_RETRYABLE` (no migration)

`qualification_status` carries an inline `CHECK (... IN (8 literals))` constraint (`schema.py`,
`_migration_027`). **Adding a new status value requires a migration** — which WP10 must not add unilaterally.

The venue/query-resolution semantics ("inconclusive; re-query, e.g. via ISIN/SMART") are correctly expressed
by the existing **`ERROR_RETRYABLE`**: it is re-selectable (`SELECTABLE_STATUSES`) and non-terminal, and it
is **not** the terminal `NOT_TRADABLE` exclusion filter. Crucially it is applied **budget-neutral**
(`count_attempt=False`, mirroring the `ConnectionUnavailableError` precedent), so a venue-resolution failure
never consumes the retry budget nor auto-escalates to `ERROR_PERMANENT` while the venue is unmapped. The
distinction is carried in `qualification_reason` (`venue_unresolved`).

**Optional future proposal (NOT applied here):** a dedicated `VENUE_UNRESOLVED` status would let operators
route these to a targeted re-query pass instead of a blind retry. It **requires a migration**
(`_migration_03x` rewriting the CHECK constraint — Postgres `DROP`/`ADD CONSTRAINT`, SQLite table rebuild —
plus `QualificationStatus`, `SELECTABLE_STATUSES`, and the `iq_finalize_run` counter derivation). Deferred
by design; not a correctness requirement.

## 4. Correction plan for the 20 stored `NOT_TRADABLE` canary rows

**No production data is changed by WP10.** This is the *plan*; each step needs its own authorization.

**Current state (atp_prod, run `68a330fbbfde4d79b7656558d0c86493`):** 20 instruments with
`qualification_status='NOT_TRADABLE'`, `tradability_status='not_tradable'`, `qualification_run_id=<run>`,
`con_id IS NULL`, `verification_status='unverified'`. These verdicts are invalid (venue-resolution
artifacts).

**Goal:** reset exactly those 20 rows to `DISCOVERED` (re-selectable) so a future qualification pass — running
the deployed WP10 code — re-queries them correctly by ISIN. Leave the historical run row `68a330fb…`
untouched (immutable audit trail).

**Preconditions (each separately authorized):**
1. WP10 merged to `main` and the backend **deployed**, so re-qualification uses the fixed query path.
2. A **fresh, verified** `atp_prod` backup (size, SHA-256, `gzip -t`, `pg_restore --list`) immediately before.
3. Trading/execution remain disabled; no market-data/orders.

**Bounded, reversible correction (run under authorization, in one transaction):**
```sql
-- scope: EXACTLY the 20 instruments this canary run touched; read tradability default from an untouched row
BEGIN;
-- sanity: expect 20
SELECT count(*) FROM instruments WHERE qualification_run_id = '68a330fbbfde4d79b7656558d0c86493';
-- reset to DISCOVERED, clear the stale qualification fields; con_id already NULL and stays NULL
UPDATE instruments
   SET qualification_status = 'DISCOVERED',
       qualification_reason = NULL,
       qualification_run_id = NULL,
       last_qualified_at    = NULL,
       tradability_status   = 'unknown',   -- restore the pre-qualification import default (verify vs a DISCOVERED row first)
       verification_status  = 'unverified',
       updated_at           = now()
 WHERE qualification_run_id = '68a330fbbfde4d79b7656558d0c86493'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL;                      -- guard: never touch a verified row
-- verify: 20 now DISCOVERED, 0 con_id set, run history unchanged, no other table touched
SELECT qualification_status, count(*) FROM instruments
 WHERE instrument_id IN (SELECT instrument_id FROM instruments WHERE qualification_run_id IS NULL)  -- post-reset
 GROUP BY 1;
COMMIT;   -- or ROLLBACK if the counts are not exactly as expected
```
*Before running:* confirm the correct `tradability_status` default by reading a still-`DISCOVERED` instrument
(the other 4,780); do not hard-code `'unknown'` if the import used a different default. The run row and all
`instrument_qualification_events` are **retained** as audit history.

**Post-checks:** instruments still 4,800; the 20 now `DISCOVERED` with NULL qualification fields; `con_id`
still 0 anywhere; `orders`/`fills`/`positions`/`md_*` still 0; safety flags off.

**Alternative (lower blast radius):** leave the 20 as-is and instead extend the venue registry / re-run a
bounded qualification that *re-selects* `ERROR_RETRYABLE` rows — but since the stored rows are the terminal
`NOT_TRADABLE` (not selectable), a reset is required for them to be re-qualified. The reset above is the
minimal, auditable path.
