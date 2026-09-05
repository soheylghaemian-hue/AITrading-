"""§ WP11 — REAL PostgreSQL proof of infra/db/wp11_bond_reset.sql (the guarded 3-row bond reset).

Runs the actual file through ``psql`` (the operator path, including its ``\\set ON_ERROR_STOP``) against a
FRESH throwaway database per case and proves: exactly 3 matching bond rows → committed, exactly those 3
reset to the import-initial state, every other row / the run row / the event trail untouched; 4 or 2
matching rows → the DO block raises, psql exits non-zero and the snapshot is IDENTICAL before and after
(full rollback). SKIPPED unless a disposable Postgres is provided (never faked with SQLite):

    export ATP_TEST_POSTGRES_DSN="postgresql://atp_test@127.0.0.1:5499/atp_test"
    PYTHONPATH=src python3 -m pytest tests/integration/test_wp11_bond_reset_sql_postgres.py -q

The DSN's user must be allowed to CREATE DATABASE (a disposable instance); ``psql`` must be on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ATP_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATP_TEST_POSTGRES_DSN to run Postgres tests")

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.store import open_store

SQL = Path(__file__).resolve().parents[2] / "infra" / "db" / "wp11_bond_reset.sql"
SRC = "cb7a88002d074b60862eea807dc2ab8e"


def _rec(ac, mic, isin):
    return InstrumentRecord(symbol=isin, asset_class=ac, exchange=mic, trading_currency="EUR", isin=isin,
                            local_symbol=isin, primary_exchange=mic, region="EUROPE", country="FR",
                            timezone="Europe/Paris", trading_calendar="eu", source="t", multiplier="1")


def _ev(run, seq, iid):
    return {"id": f"{run}-e{seq}", "seq": seq, "instrument_id": iid, "event_type": "QUALIFY_RESULT",
            "severity": "INFO"}


def _fresh_db(name: str) -> str:
    """CREATE a throwaway database on the DSN's server and return its DSN (skip if not permitted)."""
    parts = urlsplit(DSN)
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            try:
                cur.execute(f'CREATE DATABASE "{name}"')
            except psycopg.errors.InsufficientPrivilege:
                pytest.skip("DSN user may not CREATE DATABASE (need a disposable instance)")
    finally:
        admin.close()
    return urlunsplit(parts._replace(path=f"/{name}"))


def _seed(dsn: str, n_bonds: int):
    s = open_store(dsn)                                          # migrations 1..32 on real Postgres
    s.iq_create_run(run_id=SRC, request_checksum="c", run_label="recanary", exchange=None, batch_size=1,
                    pause_seconds=3.0)
    s.iq_advance_run_status(SRC, "PLANNED", "RUNNING")
    seq = 0
    for i in range(n_bonds):                                     # bonds WP10 stored as terminal NOT_TRADABLE
        r = _rec(AssetClass.BOND, ["AURO", "AFSO", "ALXP", "AURB"][i], f"FR00140{i:05d}Q3S{i}")
        s.im_upsert_instrument(r.as_record())
        s.iq_mark_pending(r.instrument_id, SRC)
        seq += 1
        s.iq_apply_outcome(r.instrument_id, run_id=SRC, qualification_status="NOT_TRADABLE",
                           reason="no contract details returned", tradability_status="not_tradable",
                           count_attempt=True, event=_ev(SRC, seq, r.instrument_id))
    for i in range(17):                                          # the 17 ERROR_RETRYABLE (stay untouched)
        r = _rec(AssetClass.EQUITY, "AQEU", f"FR0000{i:06d}")
        s.im_upsert_instrument(r.as_record())
        s.iq_mark_pending(r.instrument_id, SRC)
        seq += 1
        s.iq_apply_outcome(r.instrument_id, run_id=SRC, qualification_status="ERROR_RETRYABLE",
                           reason="venue_unresolved: seed", count_attempt=False,
                           event=_ev(SRC, seq, r.instrument_id))
    s.iq_finalize_run(SRC, status="COMPLETED")
    d = _rec(AssetClass.EQUITY, "XPAR", "FR0000900001")          # decoy: DISCOVERED
    s.im_upsert_instrument(d.as_record())
    o = _rec(AssetClass.BOND, "AURO", "FR0000900002")            # decoy: NOT_TRADABLE bond of ANOTHER run
    s.im_upsert_instrument(o.as_record())
    s.iq_create_run(run_id="otherrun", request_checksum="c", run_label="x", exchange=None, batch_size=1,
                    pause_seconds=0.0)
    s.iq_advance_run_status("otherrun", "PLANNED", "RUNNING")
    s.iq_mark_pending(o.instrument_id, "otherrun")
    s.iq_apply_outcome(o.instrument_id, run_id="otherrun", qualification_status="NOT_TRADABLE", reason="x",
                       tradability_status="not_tradable", event=_ev("otherrun", 1, o.instrument_id))
    s.iq_finalize_run("otherrun", status="COMPLETED")
    return s


def _snapshot(s):
    q = s._all
    return {
        "bonds_by_status": sorted(q("SELECT qualification_status, count(*) FROM instruments "
                                    "WHERE asset_class='bond' GROUP BY 1")),
        "src_not_tradable": q(f"SELECT count(*) FROM instruments WHERE qualification_run_id='{SRC}' "
                              "AND qualification_status='NOT_TRADABLE'")[0][0],
        "src_error_retryable": q(f"SELECT count(*) FROM instruments WHERE qualification_run_id='{SRC}' "
                                 "AND qualification_status='ERROR_RETRYABLE'")[0][0],
        "fully_reset_bonds": q("SELECT count(*) FROM instruments WHERE asset_class='bond' "
                               "AND qualification_status='DISCOVERED' AND qualification_run_id IS NULL "
                               "AND qualification_reason IS NULL AND qualification_detail IS NULL "
                               "AND last_qualified_at IS NULL AND tradability_status='unknown' "
                               "AND verification_status='unverified' AND con_id IS NULL")[0][0],
        "other_run_bond": q("SELECT qualification_status, qualification_run_id FROM instruments "
                            "WHERE isin='FR0000900002'")[0],
        "discovered_decoy": q("SELECT qualification_status FROM instruments WHERE isin='FR0000900001'")[0][0],
        "run_row": q(f"SELECT status, not_tradable_count, error_retryable_count "
                     f"FROM instrument_qualification_runs WHERE run_id='{SRC}'")[0],
        "events": q("SELECT count(*) FROM instrument_qualification_events")[0][0],
        "con_ids": q("SELECT count(con_id) FROM instruments")[0][0],
    }


def _psql(dsn: str) -> subprocess.CompletedProcess:
    psql = shutil.which("psql")
    if not psql:
        pytest.skip("psql not on PATH (the operator path runs the file through psql)")
    return subprocess.run([psql, "-X", "-d", dsn, "-f", str(SQL)], capture_output=True, text=True)


def _run_case(name: str, n_bonds: int):
    dsn = _fresh_db(name)
    s = _seed(dsn, n_bonds)
    before = _snapshot(s)
    proc = _psql(dsn)
    return before, _snapshot(s), proc


def test_exactly_three_bonds_are_reset_and_nothing_else_moves():
    before, after, proc = _run_case("wp11_bond_reset_ok", 3)
    assert proc.returncode == 0, proc.stderr
    assert "CORRECTION OK: locked=3 updated=3" in proc.stderr + proc.stdout
    assert before["src_not_tradable"] == 3 and after["src_not_tradable"] == 0
    assert after["fully_reset_bonds"] == 3                                   # every documented field reset
    assert after["src_error_retryable"] == before["src_error_retryable"] == 17
    assert after["other_run_bond"] == before["other_run_bond"] == ("NOT_TRADABLE", "otherrun")
    assert after["discovered_decoy"] == "DISCOVERED"
    assert after["run_row"] == before["run_row"] == ("COMPLETED", 3, 17)   # audit history retained
    assert after["events"] == before["events"] and after["con_ids"] == 0


@pytest.mark.parametrize("n_bonds", [4, 2])
def test_any_other_count_raises_and_rolls_back_completely(n_bonds):
    before, after, proc = _run_case(f"wp11_bond_reset_dev{n_bonds}", n_bonds)
    assert proc.returncode != 0
    assert f"ABORT: locked {n_bonds} target rows, expected exactly 3" in proc.stderr + proc.stdout
    assert after == before                                                    # full rollback, byte-for-byte
    assert after["src_not_tradable"] == n_bonds and after["fully_reset_bonds"] == 0
