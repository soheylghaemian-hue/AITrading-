"""§ R3.0 migration hotfix — PostgreSQL placeholder escaping (psycopg-safe DDL).

Production migration 18 stopped safely on `psycopg.ProgrammingError: only '%s','%b','%t' are allowed as
placeholders, got '%:'` because the PL/pgSQL trigger functions raised `RAISE EXCEPTION '%: ...',
TG_TABLE_NAME`. psycopg parses the query string for placeholders (even with empty params), so the literal
`%` must be escaped `%%`; psycopg then transmits a single `%`, which is the PL/pgSQL RAISE placeholder.

These tests exercise the ACTUAL psycopg query-adaptation semantics (not just "the SQL mentions a trigger
name"): the real psycopg client-side query converter when psycopg is installed, and otherwise the exact
placeholder RULE psycopg enforces. They also verify migrations 18 & 19 apply sequentially after 1–17 and
that a failed DDL migration rolls back and is not recorded (retry-safe).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from atp.store import open_store
from atp.store.postgres_store import PostgresStore
from atp.store.schema import (
    _migration_018,
    _migration_019,
    _migration_020,
    _migration_021,
    _migration_022,
    _migration_023,
    _migration_024,
    _migration_025,
    _migration_026,
    _migration_027,
    _migration_028,
)

PG_PLACEHOLDER = PostgresStore.PLACEHOLDER          # "%s"


def _q_postgres(sql: str) -> str:
    """Exactly what SqlStore._q does for the Postgres dialect before handing SQL to psycopg.execute()."""
    return sql.replace("?", PG_PLACEHOLDER)


def _illegal_percent_fragments(sql: str) -> list[str]:
    """psycopg's rule: after a `%`, only `%`, `s`, `b`, `t` are legal (i.e. %%, %s, %b, %t). Anything else
    (e.g. `%:` or `% `) is rejected by psycopg's client-side parser. Returns the offending fragments."""
    out, i = [], 0
    while i < len(sql):
        if sql[i] == "%":
            nxt = sql[i + 1] if i + 1 < len(sql) else ""
            if nxt in ("%", "s", "b", "t"):
                i += 2
                continue
            out.append(sql[i:i + 2])
            i += 1
        else:
            i += 1
    return out


def _psycopg_convert_or_none(sql: str):
    """Run psycopg's REAL client-side query conversion (the exact path that raised in production). Returns
    an error string if psycopg raises, "OK" if it accepts, or None if psycopg is unavailable here."""
    try:
        from psycopg._queries import PostgresQuery
        from psycopg.adapt import Transformer
    except Exception:
        return None
    try:
        PostgresQuery(Transformer()).convert(sql.encode(), ())
        return "OK"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def test_postgres_migration_placeholders_are_psycopg_safe():
    stmts = (
        _migration_018("postgres")
        + _migration_019("postgres")
        + _migration_020("postgres")
        + _migration_021("postgres")
        + _migration_022("postgres")
        + _migration_023("postgres")
        + _migration_024("postgres")
        + _migration_025("postgres")
        + _migration_026("postgres")
        + _migration_027("postgres")
        + _migration_028("postgres")
    )
    psycopg_ran = False
    for raw in stmts:
        q = _q_postgres(raw)
        # (a) the placeholder RULE psycopg enforces — meaningful even without psycopg installed
        assert _illegal_percent_fragments(q) == [], f"illegal % placeholder in: {q[:80]!r}"
        # (b) the REAL psycopg client-side converter, when available (exact production code path)
        res = _psycopg_convert_or_none(q)
        if res is not None:
            psycopg_ran = True
            assert res == "OK", f"psycopg rejected migration DDL: {res} :: {q[:80]!r}"
    # Limitation note: if psycopg is not installed in this environment, only rule (a) ran. A live
    # PostgreSQL integration test additionally requires a server (ATP_TEST_PG_DSN) and is not run here.
    assert psycopg_ran or _psycopg_convert_or_none("SELECT 1") is None


def test_postgres_functions_use_escaped_percent_and_sqlite_uses_none():
    # the two dynamic-message Postgres functions must carry the `%%` escape …
    pg = "\n".join(_migration_018("postgres"))
    assert "'%%: parent run is terminal (immutable)'" in pg
    assert "'%%: rows are immutable'" in pg
    assert "'%:" not in pg                         # no bare single-% placeholder remains
    # … the R3.0A migration-20 dataset triggers carry the same `%%` escape and no bare single-% …
    pg20 = "\n".join(_migration_020("postgres"))
    assert "'%%: parent dataset is terminal (immutable)'" in pg20
    assert "'%%: rows are immutable'" in pg20
    assert "'%:" not in pg20
    # … while the SQLite path (separate builder) contains no percent signs at all (behaviour unchanged)
    sqlite = "\n".join(_migration_018("sqlite"))
    assert "%" not in sqlite
    assert "%" not in "\n".join(_migration_020("sqlite"))
    pg21 = "\n".join(_migration_021("postgres"))
    assert "'%%: rows are immutable'" in pg21
    assert "'%:" not in pg21
    assert "%" not in "\n".join(_migration_021("sqlite"))
    pg22 = "\n".join(_migration_022("postgres"))
    assert "'%%: rows are immutable'" in pg22
    assert "'%:" not in pg22
    assert "%" not in "\n".join(_migration_022("sqlite"))
    # … WP2 migration 26 instrument-import triggers carry the same `%%` escape and no bare single-% …
    pg26 = "\n".join(_migration_026("postgres"))
    assert "'%%: rows are immutable (insert-only)'" in pg26
    assert "'%:" not in pg26
    assert "%" not in "\n".join(_migration_026("sqlite"))
    # … WP3 migration 27 instrument-qualification triggers carry the same `%%` escape and no bare single-% …
    pg27 = "\n".join(_migration_027("postgres"))
    assert "'%%: rows are immutable (insert-only)'" in pg27
    assert "'%:" not in pg27
    assert "%" not in "\n".join(_migration_027("sqlite"))
    # … WP4 migration 28 market-data triggers carry the same `%%` escape and no bare single-% …
    pg28 = "\n".join(_migration_028("postgres"))
    assert "'%%: rows are immutable (insert-only)'" in pg28
    assert "'%:" not in pg28
    assert "%" not in "\n".join(_migration_028("sqlite"))
    assert _migration_025("postgres") == [
        "ALTER TABLE paper_accounts "
        "DROP CONSTRAINT IF EXISTS paper_accounts_cash_check"
    ]


def test_migrations_18_19_apply_sequentially_after_1_17():
    s = open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))   # applies 1..20 in order
    rows = s._all("SELECT version, name FROM schema_migrations ORDER BY version")
    versions = [int(r[0]) for r in rows]
    assert versions == list(range(1, 29))          # 1..28 all applied, in order
    names = {int(r[0]): r[1] for r in rows}
    assert names[18] == "research_backtesting" and names[19] == "backtest_actual_risk"
    assert names[20] == "research_datasets"        # R3.0A immutable dataset tables
    assert names[21] == "research_intel_validation"   # R3.1A intel/validation tables
    assert names[22] == "durable_paper_canary"     # P2 dedicated durable paper ledger
    assert names[23] == "paper_canary_operator_bindings"
    assert names[24] == "paper_canary_daily_loss_aggregate"
    assert names[25] == "paper_canary_signed_account_ledger"
    assert names[26] == "global_instrument_model"   # WP2 persistent global instrument model
    assert names[27] == "instrument_ibkr_qualification"   # WP3 read-only IBKR qualification
    assert names[28] == "wp4_market_data_foundation"       # WP4 persistent market-data foundation
    s._one("SELECT instrument_id FROM instruments")                 # 26's tables exist
    s._one("SELECT run_id FROM instrument_import_runs")
    s._one("SELECT qualification_status FROM instruments")          # 27's column exists
    s._one("SELECT run_id FROM instrument_qualification_runs")      # 27's tables exist
    s._one("SELECT instrument_id FROM md_quotes_current")           # 28's tables exist
    s._one("SELECT run_id FROM md_import_runs")
    s._one(
        "SELECT trade_date,risk_capital_baseline,cumulative_equity_delta,version "
        "FROM paper_daily_loss_state"
    )
    s._one("SELECT snapshot_id FROM research_intel_snapshots")  # 21 tables exist
    # 18 created the trigger + table; 19 added columns ON TOP of 18's table → both applied sequentially
    trigs = {r[0] for r in s._all("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_bt_runs_no_update_terminal" in trigs
    s._one("SELECT expected_risk_per_share, actual_risk_per_share FROM backtest_trades")   # 19's columns
    s._one("SELECT dataset_id, dataset_checksum FROM backtest_runs")                        # 20's pin columns
    # re-opening the SAME database re-runs the migrator: it is idempotent — no duplicate, no error (retry-safe)
    from atp.store import open_store as _open
    same_path = str(Path(tempfile.mkdtemp()) / "atp.db")
    a = _open(same_path)
    b = _open(same_path)                                    # migrate again over an already-migrated db
    assert [int(r[0]) for r in b._all("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, 29))


def test_failed_migration_version_is_not_recorded_and_retry_is_safe():
    """Retry-safety guarantee: the Migrator writes each migration's statements AND the schema_migrations
    version row in ONE transaction (see Migrator.apply), so if any statement fails the whole transaction
    rolls back and the version is NOT recorded — the next deploy re-runs it cleanly.

    Here we prove the schema_migrations version record rolls back with a failing transaction. On
    PostgreSQL, because DDL is transactional, the same rollback additionally discards the migration's
    tables/functions/triggers (which is why the stopped migration 18 left the database clean). That DDL
    property is PostgreSQL-specific; SQLite's Python driver implicitly commits DDL, so it is not asserted
    here (no PostgreSQL server in this environment)."""
    s = open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))
    try:
        with s.tx() as cur:
            s._exec(cur, "INSERT INTO schema_migrations (version,name,applied_at) VALUES (999,'probe','t')")
            s._exec(cur, "INSERT INTO schema_migrations (version,name,applied_at) VALUES (999,'dup','t')")  # PK dup → fail
    except Exception:
        pass
    versions = {int(r[0]) for r in s._all("SELECT version FROM schema_migrations")}
    assert 999 not in versions                              # the version record rolled back → retry-safe
