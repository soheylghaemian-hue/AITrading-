-- WP11 correction (docs/WP11_canonical_venue_identity.md §8): reset EXACTLY the 3 bond rows the second WP10
-- canary (run cb7a88002d074b60862eea807dc2ab8e) stored as terminal NOT_TRADABLE back to their import-initial
-- DISCOVERED state, so the deployed WP11 code (ISIN in Contract.symbol; bond not-found → non-terminal) can
-- re-qualify them. Those verdicts were query artifacts of a malformed bond query, not tradability facts.
-- ONLY the documented qualification/tradability/verification fields are reset; con_id is already NULL and
-- stays NULL; run + event audit history is retained. Guarded: rolls back unless exactly 3 rows are locked
-- AND exactly 3 updated. RUN ONLY UNDER EXPLICIT AUTHORIZATION, after WP11 is deployed and a fresh
-- verified backup exists. The 17 ERROR_RETRYABLE rows need NO reset (already re-selectable).
\set ON_ERROR_STOP on

\echo == RUN IDENTIFICATION (expect the source run, COMPLETED) ==
SELECT run_id, run_label, status, not_tradable_count, error_retryable_count
  FROM instrument_qualification_runs WHERE run_id = 'cb7a88002d074b60862eea807dc2ab8e';

\echo == PRE-COUNT selection (expect 3, all asset_class = bond) ==
SELECT asset_class, count(*) AS precount FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL
 GROUP BY asset_class;

\echo == THE THREE ROWS (capture BOND_IDS_FOR_RUNNER below for: requalify_wp11.py --instrument-ids ... --expect 3) ==
SELECT instrument_id, isin, exchange, asset_class FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL
 ORDER BY instrument_id;
SELECT string_agg(instrument_id, ',' ORDER BY instrument_id) AS bond_ids FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL
   AND asset_class = 'bond' \gset
\echo BOND_IDS_FOR_RUNNER=:bond_ids

\echo == GUARDED TRANSACTION ==
BEGIN;
DO $$
DECLARE locked int; updated int;
BEGIN
  PERFORM 1 FROM instruments
   WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
     AND qualification_status = 'NOT_TRADABLE'
     AND con_id IS NULL
     AND asset_class = 'bond'
   FOR UPDATE;
  GET DIAGNOSTICS locked = ROW_COUNT;
  IF locked <> 3 THEN
    RAISE EXCEPTION 'ABORT: locked % target rows, expected exactly 3', locked;
  END IF;

  UPDATE instruments
     SET qualification_status = 'DISCOVERED',
         qualification_reason = NULL,
         qualification_run_id = NULL,
         qualification_detail = NULL,
         last_qualified_at    = NULL,
         tradability_status   = 'unknown',
         verification_status  = 'unverified',
         updated_at           = now()
   WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
     AND qualification_status = 'NOT_TRADABLE'
     AND con_id IS NULL
     AND asset_class = 'bond';
  GET DIAGNOSTICS updated = ROW_COUNT;
  IF updated <> 3 THEN
    RAISE EXCEPTION 'ABORT: UPDATE affected % rows, expected exactly 3', updated;
  END IF;

  RAISE NOTICE 'CORRECTION OK: locked=% updated=%', locked, updated;
END $$;
COMMIT;

\echo == POST-COMMIT: the source run should keep exactly its 17 ERROR_RETRYABLE rows and 0 NOT_TRADABLE ==
SELECT qualification_status, count(*) FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e' GROUP BY 1;
\echo == POST-COMMIT: the three ids are now DISCOVERED with a NULL run id (re-qualify them with --instrument-ids) ==
\echo BOND_IDS_FOR_RUNNER=:bond_ids
