-- WP11 correction (docs/WP11_canonical_venue_identity.md §8): reset EXACTLY the 3 bond rows the second WP10
-- canary (run cb7a88002d074b60862eea807dc2ab8e) stored as terminal NOT_TRADABLE back to their import-initial
-- DISCOVERED state, so the deployed WP11 code (ISIN in Contract.symbol; bond not-found → non-terminal) can
-- re-qualify them via requalify_wp11.py --instrument-ids. Those verdicts were query artifacts of a malformed
-- bond query, not tradability facts. ONLY the documented qualification/tradability/verification fields are
-- reset; con_id is already NULL and stays NULL; run + event audit history is retained.
--
-- Handover contract (the ids the runner receives): the target set is locked and captured INSIDE the
-- transaction (SELECT … FOR UPDATE into a temp table), only those rows are updated, the ids that were
-- ACTUALLY updated are captured via UPDATE … RETURNING, the two sets must be identical and of size 3 —
-- otherwise the transaction raises and rolls back completely — and BOND_IDS_FOR_RUNNER is printed only
-- AFTER a successful COMMIT (ON_ERROR_STOP stops the script before that line on any failure).
-- RUN ONLY UNDER EXPLICIT AUTHORIZATION, after WP11 is deployed and a fresh verified backup exists.
-- The 17 ERROR_RETRYABLE rows need NO reset (already re-selectable in run mode).
\set ON_ERROR_STOP on

\echo == RUN IDENTIFICATION (expect the source run, COMPLETED) ==
SELECT run_id, run_label, status, not_tradable_count, error_retryable_count
  FROM instrument_qualification_runs WHERE run_id = 'cb7a88002d074b60862eea807dc2ab8e';

\echo == PRE-COUNT (informational only — the authoritative set is captured under lock below) ==
SELECT asset_class, count(*) AS precount FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL
 GROUP BY asset_class;

\echo == GUARDED TRANSACTION (targets locked + captured inside; update only those; verify RETURNING set) ==
BEGIN;

CREATE TEMP TABLE wp11_bond_targets (instrument_id text PRIMARY KEY);
INSERT INTO wp11_bond_targets (instrument_id)
SELECT instrument_id FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
   AND qualification_status = 'NOT_TRADABLE'
   AND con_id IS NULL
   AND asset_class = 'bond'
 ORDER BY instrument_id
 FOR UPDATE;

DO $$
DECLARE locked int;
BEGIN
  SELECT count(*) INTO locked FROM wp11_bond_targets;
  IF locked <> 3 THEN
    RAISE EXCEPTION 'ABORT: locked % target rows, expected exactly 3', locked;
  END IF;
END $$;

CREATE TEMP TABLE wp11_bond_updated (instrument_id text PRIMARY KEY);
WITH upd AS (
  UPDATE instruments
     SET qualification_status = 'DISCOVERED',
         qualification_reason = NULL,
         qualification_run_id = NULL,
         qualification_detail = NULL,
         last_qualified_at    = NULL,
         tradability_status   = 'unknown',
         verification_status  = 'unverified',
         updated_at           = now()
   WHERE instrument_id IN (SELECT instrument_id FROM wp11_bond_targets)
     AND qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e'
     AND qualification_status = 'NOT_TRADABLE'
     AND con_id IS NULL
     AND asset_class = 'bond'
  RETURNING instrument_id
)
INSERT INTO wp11_bond_updated (instrument_id) SELECT instrument_id FROM upd;

DO $$
DECLARE updated int; missing int; extra int;
BEGIN
  SELECT count(*) INTO updated FROM wp11_bond_updated;
  SELECT count(*) INTO missing FROM
    (SELECT instrument_id FROM wp11_bond_targets EXCEPT SELECT instrument_id FROM wp11_bond_updated) m;
  SELECT count(*) INTO extra FROM
    (SELECT instrument_id FROM wp11_bond_updated EXCEPT SELECT instrument_id FROM wp11_bond_targets) e;
  IF updated <> 3 OR missing <> 0 OR extra <> 0 THEN
    RAISE EXCEPTION 'ABORT: updated % rows (missing % / extra % vs the locked set), expected exactly 3',
      updated, missing, extra;
  END IF;
  RAISE NOTICE 'CORRECTION OK: locked=3 updated=%', updated;
END $$;
COMMIT;

\echo == POST-COMMIT: the source run keeps exactly its 17 ERROR_RETRYABLE rows and 0 NOT_TRADABLE ==
SELECT qualification_status, count(*) FROM instruments
 WHERE qualification_run_id = 'cb7a88002d074b60862eea807dc2ab8e' GROUP BY 1;

\echo == POST-COMMIT: the ids that were ACTUALLY corrected (from UPDATE … RETURNING), now DISCOVERED / run NULL ==
SELECT i.instrument_id, i.isin, i.exchange, i.qualification_status, i.qualification_run_id
  FROM instruments i JOIN wp11_bond_updated u USING (instrument_id) ORDER BY i.instrument_id;
SELECT string_agg(instrument_id, ',' ORDER BY instrument_id) AS bond_ids FROM wp11_bond_updated \gset
\echo BOND_IDS_FOR_RUNNER=:bond_ids
\echo (pass exactly these to: requalify_wp11.py --instrument-ids :bond_ids --expect 3)
