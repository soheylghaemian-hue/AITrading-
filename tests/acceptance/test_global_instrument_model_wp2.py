"""§ WP2 acceptance — persistent, unified global-instrument & market model (REFERENCE DATA ONLY).

End-to-end over the durable store + importer:
  * additive migration 26 applies after 1–25; the three tables exist;
  * idempotent, collision-safe upsert (inserted / unchanged / updated) and DB-level UNIQUE(natural_key);
  * symbol collision across exchanges yields two distinct rows;
  * import-run lifecycle with observable progress (events + counters);
  * per-market error isolation → PARTIAL, other markets still imported;
  * resumability — an interrupted RUNNING run resumes and skips completed markets;
  * idempotent re-run of a COMPLETED import is a no-op; a PARTIAL/FAILED import is retryable;
  * DB-enforced immutability of import events and of terminal runs;
  * atomic reclaim of a stale RUNNING run.

SAFETY: no trading, no orders/execution/broker, no market-data subscription, no IBKR qualification.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atp.core.enums import AssetClass
from atp.instruments.importer import (
    MarketPlan,
    MarketSource,
    import_instruments,
    import_request_checksum,
)
from atp.instruments.listing_sources import ListingCandidate
from atp.instruments.model import InstrumentRecord
from atp.store import open_store


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _plan(mid, region="AMERICAS", country="US", tz="America/New_York", cal="us_equity", ccy="USD"):
    return MarketPlan(market_id=mid, region=region, country=country, timezone=tz, calendar=cal,
                      default_currency=ccy)


def _cand(symbol, sec_type="STK", exchange="NASDAQ", currency="USD", description="", lot=1.0, source="test"):
    return ListingCandidate(symbol=symbol, sec_type=sec_type, exchange=exchange, currency=currency,
                            description=description, lot_size=lot, source=source)


def _rec(symbol="AAPL", exchange="NASDAQ", **kw):
    base = dict(symbol=symbol, asset_class=AssetClass.EQUITY, exchange=exchange, trading_currency="USD",
                region="AMERICAS", country="US", timezone="America/New_York", trading_calendar="us_equity",
                multiplier="1", source="test")
    base.update(kw)
    return InstrumentRecord(**base)


# --------------------------------------------------------------------- migration
def test_migration_26_applied_and_tables_exist():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {1, 25, 26} <= versions and max(versions) == 26
    # each additive table is queryable
    for tbl in ("instruments", "instrument_import_runs", "instrument_import_events"):
        assert store._all(f"SELECT COUNT(*) FROM {tbl}")[0][0] == 0


# --------------------------------------------------------------------- upsert idempotency + collision
def test_upsert_is_idempotent_and_change_detecting():
    store = _store()
    rec = _rec()
    assert store.im_upsert_instrument(rec.as_record()) == "inserted"
    assert store.im_upsert_instrument(rec.as_record()) == "unchanged"
    assert store.im_count_instruments() == 1
    changed = _rec(description="Apple Inc.")
    assert changed.instrument_id == rec.instrument_id           # same identity
    assert store.im_upsert_instrument(changed.as_record()) == "updated"
    assert store.im_count_instruments() == 1                    # still one row
    got = store.im_get_instrument(rec.instrument_id)
    assert got.description == "Apple Inc." and got.verification_status == "unverified"
    assert got.isin is None and got.con_id is None              # NO DATA persisted honestly


def test_symbol_collision_across_exchanges_yields_two_rows():
    store = _store()
    store.im_upsert_instrument(_rec(symbol="AAPL", exchange="NASDAQ").as_record())
    store.im_upsert_instrument(_rec(symbol="AAPL", exchange="XLON", trading_currency="GBP").as_record())
    assert store.im_count_instruments() == 2
    assert store.im_count_instruments(exchange="NASDAQ") == 1
    assert store.im_count_instruments(exchange="XLON") == 1


def test_db_rejects_duplicate_natural_key():
    store = _store()
    rec = _rec()
    store.im_upsert_instrument(rec.as_record())
    with pytest.raises(Exception):  # noqa: B017 — DB IntegrityError under both dialects
        with store.tx() as cur:
            store._exec(cur,
                        "INSERT INTO instruments (instrument_id,natural_key,symbol,exchange,trading_currency,"
                        "asset_class,tradability_status,market_data_status,source_status,verification_status,"
                        "content_checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("INS-OTHER", rec.natural_key, "AAPL", "NASDAQ", "USD", "equity", "unknown",
                         "unknown", "discovered", "unverified", "sha256:x", "2026-01-01T00:00:00+00:00",
                         "2026-01-01T00:00:00+00:00"))


# --------------------------------------------------------------------- importer lifecycle + observability
def test_import_lifecycle_and_progress_is_observable():
    store = _store()
    us = MarketSource(plan=_plan("US"), provider=lambda: [
        _cand("MSFT", description="Microsoft"), _cand("SPY", sec_type="ETF", exchange="ARCA")])
    summary = import_instruments(store, source_label="listings", markets=[us])
    assert summary.status == "COMPLETED"
    assert summary.completed_markets == ["US"] and summary.failed_markets == []
    assert summary.discovered == 2 and summary.inserted == 2 and summary.updated == 0
    assert store.im_count_instruments() == 2

    run = store.im_get_run(summary.run_id)
    assert run.status == "COMPLETED" and run.started_at and run.ended_at
    assert run.inserted_count == 2 and run.failed_market_count == 0
    events = [e.event_type for e in store.im_list_run_events(summary.run_id)]
    assert events == ["MARKET_START", "MARKET_OK"]


def test_per_market_error_isolation_gives_partial():
    store = _store()
    good = MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT"), _cand("AAPL")])

    def boom():
        raise RuntimeError("provider unavailable")

    bad = MarketSource(plan=_plan("EU", region="EUROPE", country="DE", tz="Europe/Berlin",
                                  cal="xetra", ccy="EUR"), provider=boom)
    summary = import_instruments(store, source_label="listings", markets=[good, bad])
    assert summary.status == "PARTIAL"
    assert summary.completed_markets == ["US"] and summary.failed_markets == ["EU"]
    assert store.im_count_instruments() == 2                       # the good market still imported
    types = [(e.market, e.event_type) for e in store.im_list_run_events(summary.run_id)]
    assert ("EU", "MARKET_ERROR") in types and ("US", "MARKET_OK") in types
    err = next(e for e in store.im_list_run_events(summary.run_id) if e.event_type == "MARKET_ERROR")
    assert "provider unavailable" in (err.details_json or "")


def test_all_markets_failing_gives_failed_and_is_retryable():
    store = _store()

    def boom():
        raise RuntimeError("down")

    markets = [MarketSource(plan=_plan("US"), provider=boom)]
    first = import_instruments(store, source_label="listings", markets=markets)
    assert first.status == "FAILED" and store.im_count_instruments() == 0

    # A FAILED import is retryable: a fresh run is created (not a no-op) that can now succeed.
    ok_markets = [MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT")])]
    retry = import_instruments(store, source_label="listings", markets=ok_markets)
    assert retry.run_id != first.run_id and retry.status == "COMPLETED"
    assert store.im_count_instruments() == 1


# --------------------------------------------------------------------- idempotent re-run
def test_completed_import_rerun_is_a_noop():
    store = _store()
    markets = [MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT"), _cand("AAPL")])]
    first = import_instruments(store, source_label="listings", markets=markets)
    assert first.status == "COMPLETED"

    again = import_instruments(store, source_label="listings", markets=markets)
    assert again.already_done is True and again.run_id == first.run_id
    assert store.im_count_instruments() == 2                       # no duplicate rows
    assert len(store.im_list_runs()) == 1                          # no second run created


# --------------------------------------------------------------------- resumability
def test_interrupted_run_resumes_and_skips_completed_markets():
    store = _store()

    def must_not_run():
        raise AssertionError("completed market must not be re-fetched on resume")

    market_a = MarketSource(plan=_plan("US"), provider=must_not_run)                  # pretend already done
    market_b = MarketSource(plan=_plan("EU", region="EUROPE", country="DE", tz="Europe/Berlin",
                                       cal="xetra", ccy="EUR"),
                            provider=lambda: [_cand("SAP", exchange="XETRA", currency="EUR")])

    # Simulate a crashed run: RUNNING, market US already completed and persisted.
    checksum = import_request_checksum("listings", ["US", "EU"])
    store.im_create_import_run(run_id="run-resume", request_checksum=checksum,
                               source_label="listings", planned_markets=["US", "EU"])
    assert store.im_advance_run_status("run-resume", "PLANNED", "RUNNING")
    assert store.im_record_market_progress("run-resume", market="US", market_status="COMPLETED",
                                           counts={"discovered": 1, "inserted": 1}, event=None)

    summary = import_instruments(store, source_label="listings", markets=[market_a, market_b])
    assert summary.resumed is True and summary.run_id == "run-resume"
    assert summary.status == "COMPLETED"
    assert set(summary.completed_markets) == {"US", "EU"}
    # Only market B's instrument was imported by the resumed pass (A's provider was never called).
    assert store.im_count_instruments(exchange="XETRA") == 1


def test_progress_guard_refuses_writes_once_terminal():
    store = _store()
    markets = [MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT")])]
    summary = import_instruments(store, source_label="listings", markets=markets)
    assert summary.status == "COMPLETED"
    # The run is terminal; a late progress write is refused (guarded on status='RUNNING').
    assert store.im_record_market_progress(summary.run_id, market="US", market_status="COMPLETED",
                                           counts={"discovered": 99}, event=None) is False
    assert store.im_get_run(summary.run_id).discovered_count == 1


# --------------------------------------------------------------------- DB-enforced immutability
def test_import_events_are_immutable():
    store = _store()
    markets = [MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT")])]
    summary = import_instruments(store, source_label="listings", markets=markets)
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE instrument_import_events SET severity='X' WHERE run_id=?",
                        (summary.run_id,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "DELETE FROM instrument_import_events WHERE run_id=?", (summary.run_id,))


def test_terminal_run_is_frozen():
    store = _store()
    markets = [MarketSource(plan=_plan("US"), provider=lambda: [_cand("MSFT")])]
    summary = import_instruments(store, source_label="listings", markets=markets)
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE instrument_import_runs SET source_label='x' WHERE run_id=?",
                        (summary.run_id,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "DELETE FROM instrument_import_runs WHERE run_id=?", (summary.run_id,))


# --------------------------------------------------------------------- reclaim stale RUNNING
def test_reclaim_stale_running_run():
    store = _store()
    store.im_create_import_run(run_id="stale", request_checksum="sha256:c", source_label="listings",
                               planned_markets=["US"])
    store.im_advance_run_status("stale", "PLANNED", "RUNNING")
    # backdate the heartbeat (allowed while RUNNING — not yet terminal)
    with store.tx() as cur:
        store._exec(cur, "UPDATE instrument_import_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))
    reclaimed = store.im_reclaim_stale_running("2020-01-01T00:00:00+00:00",
                                               failure_code="STALE", failure_reason="worker crashed")
    assert reclaimed == ["stale"]
    run = store.im_get_run("stale")
    assert run.status == "FAILED" and run.failure_code == "STALE" and run.ended_at
    assert any(e.event_type == "RECLAIM" for e in store.im_list_run_events("stale"))


def test_reclaim_leaves_fresh_running_run_untouched():
    store = _store()
    store.im_create_import_run(run_id="fresh", request_checksum="sha256:d", source_label="listings",
                               planned_markets=["US"])
    store.im_advance_run_status("fresh", "PLANNED", "RUNNING")
    reclaimed = store.im_reclaim_stale_running("2000-01-01T00:00:00+00:00",
                                               failure_code="STALE", failure_reason="x")
    assert reclaimed == [] and store.im_get_run("fresh").status == "RUNNING"
