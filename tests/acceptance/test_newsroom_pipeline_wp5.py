"""§ WP5 acceptance — worldwide news & official filings pipeline (RESEARCH DATA ONLY).

The section-11 case matrix over the durable store + ingest orchestrator with a deterministic stub provider:
migration 27; primary vs secondary; exact duplicate; syndicated near-duplicate cluster; correction; retraction;
ambiguous mapping; symbol collision across exchanges; multiple affected instruments; original language kept
separate from translation; missing license → metadata only; provider outage → FAILED/UNAVAILABLE; rate limit;
cursor resume; parallel workers; stale-run recovery; idempotency; future timestamp; missing timestamp;
immutable original; append-only audit; per-region error isolation; observable counters.

SAFETY: no orders/execution/account, no subscription/news purchase, no HTTP write path.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.newsroom import (
    EventCategory,
    IngestConfig,
    LicenseMetadata,
    LicenseStatus,
    MappingHint,
    NewsMessage,
    Primacy,
    ProviderNewsItem,
    StubNewsProvider,
    ingest_news,
    message_id_for,
    seed_registry,
)
from atp.store import open_store

NOW = "2026-09-02T12:00:00+00:00"
PUB = "2026-09-02T10:00:00+00:00"
LIC = LicenseMetadata(license_status=LicenseStatus.LICENSED_STORE_ONLY, storage_allowed=True)


def _store():
    s = open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))
    seed_registry(s)
    return s


def _inst(store, symbol, exchange="NASDAQ", isin=None, con_id=None):
    rec = InstrumentRecord(symbol=symbol, asset_class=AssetClass.EQUITY, exchange=exchange,
                           trading_currency="USD", region="AMERICAS", country="US",
                           timezone="America/New_York", trading_calendar="us_equity", multiplier="1",
                           primary_exchange=exchange, isin=isin, con_id=con_id, source="t")
    store.im_upsert_instrument(rec.as_record())
    return rec.instrument_id


def _item(pid, title, *, body="body", lang="en", primacy=Primacy.PRIMARY, cat=EventCategory.EARNINGS,
          hints=(), pub=PUB, corr=None, retr=None, regions=("AMERICAS",), countries=("US",)):
    return ProviderNewsItem(provider_id=pid, title=title, body=body, language=lang, url=f"http://x/{pid}",
                            source_name="Src", published_at=pub, primacy=primacy, event_category=cat,
                            mapping_hints=hints, correction_of_provider_id=corr, retraction_of_provider_id=retr,
                            regions=regions, countries=countries)


def _prov(pages, **kw):
    return StubNewsProvider(name="AGG", source_id="licensed_aggregators", pages=pages, license=LIC, **kw)


def _run(store, prov, label="d", **cfg):
    return ingest_news(store, prov, run_label=label, config=IngestConfig(page_limit=50, **cfg), now=NOW)


# --------------------------------------------------------------------- migration
def test_migration_27_applied_and_legacy_untouched():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {26, 27} <= versions      # WP6 stacks migration 29 on top on its branch, so 27 is not the max there
    for tbl in ("news_messages", "news_message_instruments", "news_message_events", "news_sources",
                "news_import_runs", "news_import_events"):
        assert store._all(f"SELECT COUNT(*) FROM {tbl}")[0][0] >= 0
    store._one("SELECT id,symbol,title FROM news_items LIMIT 0")     # legacy news_items untouched
    store._one("SELECT symbol,company_name FROM companies LIMIT 0")


# --------------------------------------------------------------------- primacy / dedup / cluster
def test_primary_vs_secondary_and_syndication_cluster():
    store = _store()
    prov = _prov([[_item("m1", "Beats earnings"),
                   _item("m1s", "Beats earnings", body="reworded", primacy=Primacy.SECONDARY)]])
    _run(store, prov)
    m1 = store.nx_get_message(message_id_for("AGG", "m1"))
    m1s = store.nx_get_message(message_id_for("AGG", "m1s"))
    assert m1.primacy == "PRIMARY" and m1s.primacy == "SECONDARY"
    assert m1.cluster_id == m1s.cluster_id and m1.content_checksum != m1s.content_checksum


def test_exact_duplicate_is_flagged():
    store = _store()
    prov = _prov([[_item("m1", "Beats earnings"), _item("m1b", "Beats earnings")]])   # identical content
    s = _run(store, prov)
    assert s.duplicates == 1
    dup = store.nx_get_message(message_id_for("AGG", "m1b"))
    assert dup.duplicate_of_id == message_id_for("AGG", "m1")


def test_dedup_identity_is_independent_of_license_gate():
    """Regression: the content checksum fingerprints the AS-FETCHED body, not the license-gated stored body.
    A metadata-only source (body not stored) must NOT collapse two same-headline items with DIFFERENT bodies
    into a false duplicate, and must still catch a genuine same-body duplicate."""
    ml = LicenseMetadata(license_status=LicenseStatus.NO_LICENSE, storage_allowed=False)   # body NOT stored
    # same title + time, DIFFERENT bodies → NOT a duplicate
    store = _store()
    prov = StubNewsProvider(name="ML", source_id="company_ir", license=ml, pages=[[
        _item("a", "Q3 Results", body="Revenue UP 10 percent"),
        _item("b", "Q3 Results", body="Revenue DOWN 10 percent")]])
    s = _run(store, prov)
    assert s.duplicates == 0
    assert store.nx_get_message(message_id_for("ML", "b")).duplicate_of_id is None
    assert store.nx_get_message(message_id_for("ML", "a")).original_body is None    # body still not stored
    # same title + time + SAME body → still flagged, even though neither body is stored
    store2 = _store()
    prov2 = StubNewsProvider(name="ML", source_id="company_ir", license=ml, pages=[[
        _item("a", "Q3 Results", body="identical wire copy"),
        _item("b", "Q3 Results", body="identical wire copy")]])
    assert _run(store2, prov2).duplicates == 1


# --------------------------------------------------------------------- corrections / retractions
def test_correction_and_retraction_link_and_preserve_original():
    store = _store()
    prov = _prov([[_item("m1", "Original"),
                   _item("m2", "Corrected", corr="m1"),
                   _item("m3", "Withdrawn", retr="m1")]])
    s = _run(store, prov)
    assert s.corrections == 1 and s.retractions == 1
    m1_id = message_id_for("AGG", "m1")
    assert store.nx_get_message(m1_id).original_title == "Original"        # original never overwritten
    assert store.nx_derived_message_status(m1_id) == "RETRACTED"           # derived, not mutated
    types = {e.event_type for e in store.nx_list_message_events(m1_id)}
    assert {"CORRECTION", "RETRACTION"} <= types                          # append-only audit on the original
    assert store.nx_get_run(s.run_id).fetched_count == 3                  # counter consistency: 3 items fetched


def test_correction_before_original_still_records_audit_on_original():
    """Regression: a retraction/correction arriving BEFORE its original must still be recorded in the
    original's append-only audit log (order-independent backfill), not silently dropped."""
    store = _store()
    prov = _prov([[_item("m3", "Withdrawn", retr="m1"), _item("m1", "Original")]])   # retraction FIRST
    _run(store, prov)
    m1_id = message_id_for("AGG", "m1")
    assert store.nx_derived_message_status(m1_id) == "RETRACTED"
    assert "RETRACTION" in {e.event_type for e in store.nx_list_message_events(m1_id)}   # backfilled


# --------------------------------------------------------------------- fail-closed instrument mapping
def test_verified_ambiguous_and_symbol_collision_mapping():
    store = _store()
    msft = _inst(store, "MSFT", con_id=1001, isin="US5949181045")
    aapl_us = _inst(store, "AAPL", "NASDAQ")
    aapl_uk = _inst(store, "AAPL", "XLON")
    prov = _prov([[
        _item("v1", "MSFT via conId", hints=(MappingHint(con_id=1001),)),
        _item("v2", "MSFT via ISIN", hints=(MappingHint(isin="US5949181045"),)),
        _item("amb", "AAPL symbol only", hints=(MappingHint(symbol="AAPL"),)),     # symbol alone
    ]])
    _run(store, prov)
    assert [(m.instrument_id == msft, m.mapping_status)
            for m in store.nx_list_message_instruments(message_id_for("AGG", "v1"))] == [(True, "VERIFIED")]
    assert store.nx_list_message_instruments(message_id_for("AGG", "v2"))[0].mapping_status == "VERIFIED"
    amb = store.nx_list_message_instruments(message_id_for("AGG", "amb"))
    assert {m.instrument_id for m in amb} == {aapl_us, aapl_uk}            # both exchanges kept separate
    assert all(m.mapping_status == "AMBIGUOUS" for m in amb)              # symbol alone is never VERIFIED


def test_multiple_affected_instruments():
    store = _store()
    a = _inst(store, "AAA", con_id=1)
    b = _inst(store, "BBB", con_id=2)
    prov = _prov([[_item("m", "Two-company merger", cat=EventCategory.MA,
                         hints=(MappingHint(con_id=1), MappingHint(con_id=2)))]])
    _run(store, prov)
    assert {m.instrument_id for m in store.nx_list_message_instruments(message_id_for("AGG", "m"))} == {a, b}


def test_unmapped_when_no_catalogue_match():
    store = _store()
    prov = _prov([[_item("m", "Unknown co", hints=(MappingHint(con_id=999),))]])
    s = _run(store, prov)
    assert s.unmapped == 1 and store.nx_list_message_instruments(message_id_for("AGG", "m")) == []


# --------------------------------------------------------------------- language / translation
def test_original_language_and_separate_translation():
    store = _store()
    item = ProviderNewsItem(provider_id="de1", title="Gewinn steigt", body="Originaltext", language="de",
                            published_at=PUB, primacy=Primacy.PRIMARY, regions=("EUROPE",), countries=("DE",))
    _run(store, _prov([[item]]))
    m = store.nx_get_message(message_id_for("AGG", "de1"))
    assert m.original_language == "de" and m.original_title == "Gewinn steigt"
    assert m.translated_title is None and m.translation_status == "ORIGINAL_ONLY"   # never a fake translation


# --------------------------------------------------------------------- license gating
def test_missing_license_stores_metadata_only():
    store = _store()
    prov = StubNewsProvider(name="NL", source_id="company_ir",
                            pages=[[_item("x", "Headline", body="full licensed body")]],
                            license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE, storage_allowed=False))
    _run(store, prov)
    m = store.nx_get_message(message_id_for("NL", "x"))
    assert m.storage_status == "STORED_METADATA_ONLY" and m.original_body is None
    assert m.original_title == "Headline" and m.license_status == "NO_LICENSE"


def test_storage_allowed_flag_cannot_override_a_non_storable_license():
    """Regression (fail-closed): a misconfigured provider that sets storage_allowed=True under a license status
    that does NOT grant storage must still be stored metadata-only — the body is never persisted."""
    store = _store()
    prov = StubNewsProvider(name="BUG", source_id="company_ir",
                            pages=[[_item("x", "Headline", body="full licensed body")]],
                            license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE, storage_allowed=True))
    _run(store, prov)
    m = store.nx_get_message(message_id_for("BUG", "x"))
    assert m.storage_status == "STORED_METADATA_ONLY" and m.original_body is None   # body NOT stored
    assert m.license_status == "NO_LICENSE"


# --------------------------------------------------------------------- time integrity
def test_future_and_missing_timestamps_are_flagged_not_fabricated():
    store = _store()
    prov = _prov([[_item("f", "Future", pub="2999-01-01T00:00:00+00:00"),
                   _item("t", "No time", pub=None)]])
    _run(store, prov)
    f = store.nx_get_message(message_id_for("AGG", "f"))
    t = store.nx_get_message(message_id_for("AGG", "t"))
    assert f.time_status == "FUTURE_CONFLICT"
    assert t.time_status == "MISSING_PUBLISH" and t.published_at is None and t.received_at is not None


# --------------------------------------------------------------------- provider outage / rate limit
def test_provider_unavailable_fails_closed_and_marks_source():
    store = _store()
    prov = StubNewsProvider(name="DOWN", source_id="sec_edgar", unavailable=True)
    s = ingest_news(store, prov, run_label="d", now=NOW)
    assert s.status == "FAILED" and store.nx_get_run(s.run_id).failure_code == "PROVIDER_UNAVAILABLE"
    assert store.nx_get_source("sec_edgar").available is False


def test_unconfigured_provider_fails_closed():
    store = _store()

    class Unconfigured(StubNewsProvider):
        @property
        def configured(self):
            return False

    s = ingest_news(store, Unconfigured(name="X", source_id="rns_uk"), run_label="d", now=NOW)
    assert s.status == "FAILED" and store.nx_get_run(s.run_id).failure_code == "PROVIDER_NOT_CONFIGURED"


def test_rate_limit_stops_politely_and_is_resumable():
    store = _store()
    prov = _prov([[_item("m1", "One")]], rate_limited=True)
    s = ingest_news(store, prov, run_label="d", now=NOW)
    # nothing fetched; the run finishes without crashing and a RATE_LIMITED event is recorded
    assert s.fetched == 0
    assert any(e.event_type == "RATE_LIMITED" for e in store.nx_list_run_events(s.run_id))


# --------------------------------------------------------------------- cursor resume / idempotency
def test_cursor_pagination_and_idempotent_reingest():
    store = _store()
    prov = _prov([[_item("m1", "A")], [_item("m2", "B")], [_item("m3", "C")]])
    s = _run(store, prov)
    assert s.fetched == 3 and s.stored == 3 and prov.calls == [None, "1", "2"]
    # re-ingesting the same messages stores nothing new (idempotent per message_id)
    s2 = _run(store, _prov([[_item("m1", "A")], [_item("m2", "B")], [_item("m3", "C")]]))
    assert s2.stored == 0 and store.nx_count_messages() == 3


def test_resume_from_cursor_processes_only_remaining():
    store = _store()
    # a first pass persisted m1 (page 0); resume from cursor '1' should fetch only page 1 onward
    _run(store, _prov([[_item("m1", "A")], [_item("m2", "B")]]))
    prov = _prov([[_item("m1", "A")], [_item("m2", "B")]])
    ingest_news(store, prov, run_label="d", config=IngestConfig(start_cursor="1"), now=NOW)
    assert prov.calls == ["1"]                                   # started at the resumed cursor
    assert store.nx_count_messages() == 2                        # m1 already there, m2 added


# --------------------------------------------------------------------- per-region isolation
class _RegionFailStore:
    """Delegating proxy whose store writes for a bad region raise an INFRA error that ESCAPES the
    per-message handler (both the insert AND the error-path bump fail), so the whole region fails."""

    def __init__(self, store, bad_region):
        self._s = store
        self._bad = bad_region

    def __getattr__(self, name):
        return getattr(self._s, name)

    def nx_insert_message(self, record, **kw):
        if (kw.get("run_event") or {}).get("region") == self._bad:
            raise RuntimeError("region infra down")
        return self._s.nx_insert_message(record, **kw)

    def nx_bump(self, run_id, counters, *, event=None):
        if (event or {}).get("region") == self._bad:
            raise RuntimeError("region infra down")
        return self._s.nx_bump(run_id, counters, event=event)


def test_per_region_error_isolation_gives_partial():
    store = _store()
    proxy = _RegionFailStore(store, "EUROPE")
    prov = _prov([[_item("us", "US news", regions=("AMERICAS",), countries=("US",)),
                   _item("eu", "EU news", regions=("EUROPE",), countries=("DE",))]])
    s = ingest_news(proxy, prov, run_label="d", config=IngestConfig(page_limit=50), now=NOW)
    assert s.status == "PARTIAL"
    assert s.completed_regions == ["AMERICAS"] and s.failed_regions == ["EUROPE"]
    assert store.nx_get_message(message_id_for("AGG", "us")) is not None
    # resume-without-loss: the cursor is NOT advanced past a page whose region failed (stays at page 0)
    assert store.nx_get_run(s.run_id).cursor is None


class _OneMessageFailStore:
    """Proxy whose nx_insert_message raises a TRANSIENT infra error for ONE message (identified by the
    run_event's message_id) while the error-path bump SUCCEEDS — so the failure stays isolated per-message
    (ok=False) instead of escaping to the region handler. `heal()` stops failing (the blip passes)."""

    def __init__(self, store, bad_message_id):
        self._s = store
        self._bad = bad_message_id
        self._healed = False

    def heal(self):
        self._healed = True

    def __getattr__(self, name):
        return getattr(self._s, name)

    def nx_insert_message(self, record, **kw):
        if not self._healed and (kw.get("run_event") or {}).get("message_id") == self._bad:
            raise RuntimeError("transient lock timeout")
        return self._s.nx_insert_message(record, **kw)


def test_message_level_error_holds_cursor_and_is_recoverable():
    """Regression: a per-MESSAGE transient error must NOT let the cursor advance past its page (which would
    lose the item on resume). The failed item is recorded identifiably (dead-letter), the cursor is held, and
    a resume once the blip passes stores it — no silent data loss."""
    store = _store()
    bad = message_id_for("AGG", "b")
    proxy = _OneMessageFailStore(store, bad)
    prov = _prov([[_item("a", "A", regions=("AMERICAS",), countries=("US",)),
                   _item("b", "B", regions=("AMERICAS",), countries=("US",)),
                   _item("c", "C", regions=("EUROPE",), countries=("DE",))]])
    s = ingest_news(proxy, prov, run_label="d", config=IngestConfig(page_limit=50), now=NOW)
    assert s.status == "PARTIAL" and s.error == 1
    assert s.completed_regions == ["EUROPE"] and s.failed_regions == ["AMERICAS"]
    assert store.nx_get_message(message_id_for("AGG", "a")) is not None    # sibling stored
    assert store.nx_get_message(message_id_for("AGG", "c")) is not None    # other region stored
    assert store.nx_get_message(bad) is None                              # the failed one is NOT stored
    assert store.nx_get_run(s.run_id).cursor is None                      # cursor HELD on the page (page 0)
    # the failed message is identifiable in the audit trail — a recoverable dead-letter, never message_id=None
    errs = [e for e in store.nx_list_run_events(s.run_id) if e.event_type == "MESSAGE_ERROR"]
    assert errs and errs[0].message_id == bad and "provider_id=b" in (errs[0].reason or "")
    # resume once the blip passes → the previously-failed message is now stored (no loss)
    proxy.heal()
    s2 = ingest_news(proxy, prov, run_label="d",
                     config=IngestConfig(page_limit=50, start_cursor=store.nx_get_run(s.run_id).cursor), now=NOW)
    assert store.nx_get_message(bad) is not None and s2.status == "COMPLETED"


def test_straggler_write_to_a_reclaimed_run_does_not_desync_it():
    """Regression: after a run is finalized/reclaimed, a still-alive straggler worker calling nx_insert_message
    for that run stores the GLOBAL message (dedup-safe) but must NOT append an event to, or bump the counters
    of, the now-terminal run — its counters and event log stay consistent (no post-finalization drift)."""
    store = _store()
    store.nx_create_run(run_id="R", request_checksum="sha256:x", run_label="l", provider="AGG", source_id="s")
    store.nx_advance_run_status("R", "PLANNED", "RUNNING")
    store.nx_finalize_run("R", status="FAILED", failure_code="STALE", failure_reason="reclaimed")
    events_before = len(store.nx_list_run_events("R"))
    msg = NewsMessage(provider="AGG", provider_id="late", source_id="s", original_title="late arrival")
    res = store.nx_insert_message(msg.as_record(), run_id="R",
                                  run_event={"id": "R-late", "event_type": "MESSAGE_STORED", "severity": "INFO",
                                             "message_id": msg.message_id})
    assert res == "inserted"
    assert store.nx_get_message(msg.message_id) is not None               # global message IS stored
    run = store.nx_get_run("R")
    assert run.status == "FAILED" and run.stored_count == 0               # frozen run's counters unchanged
    assert len(store.nx_list_run_events("R")) == events_before           # no event appended to the frozen run


def test_parallel_workers_do_not_collide():
    """Two overlapping runs over the same messages: each is its own run (distinct run_id + per-run event
    ids), messages are idempotent per message_id, so nothing collides or double-stores."""
    store = _store()
    a = ingest_news(store, _prov([[_item("m1", "A"), _item("m2", "B")]]), run_label="d", run_id="run-a", now=NOW)
    b = ingest_news(store, _prov([[_item("m1", "A"), _item("m2", "B")]]), run_label="d", run_id="run-b", now=NOW)
    assert a.run_id == "run-a" and b.run_id == "run-b"
    assert a.stored == 2 and b.stored == 0                      # second worker re-ingests idempotently
    assert store.nx_count_messages() == 2                       # no duplicate rows, no PK collision


# --------------------------------------------------------------------- immutability / reclaim
def test_original_message_and_terminal_run_immutable():
    store = _store()
    _run(store, _prov([[_item("m1", "One")]]))
    mid = message_id_for("AGG", "m1")
    for sql in (f"UPDATE news_messages SET original_title='x' WHERE message_id='{mid}'",
                f"DELETE FROM news_messages WHERE message_id='{mid}'"):
        with pytest.raises(Exception), store.tx() as cur:  # noqa: B017
            store._exec(cur, sql)
    runs = store.nx_list_runs()
    with pytest.raises(Exception), store.tx() as cur:  # noqa: B017
        store._exec(cur, "UPDATE news_import_runs SET run_label='x' WHERE run_id=?", (runs[0].run_id,))


def test_reclaim_is_parallel_worker_safe():
    """Regression: two concurrent reclaimers must not collide on the RECLAIM event PK — the event insert is
    idempotent (ON CONFLICT DO NOTHING) and the guarded status flip is the sole arbiter. Simulated by a
    _probe that pre-inserts the RECLAIM event (as a racing worker would) before our reclaim tx."""
    store = _store()
    store.nx_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", provider="AGG",
                        source_id="s")
    store.nx_advance_run_status("stale", "PLANNED", "RUNNING")
    with store.tx() as cur:
        store._exec(cur, "UPDATE news_import_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))

    def probe(run_id):   # a racing worker that already inserted the RECLAIM event
        with store.tx() as cur:
            store._exec(cur, "INSERT INTO news_import_events (id,run_id,seq,ts,provider,region,message_id,"
                        "event_type,severity,reason,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{run_id}-reclaim", run_id, None, "2000-01-01T00:00:00+00:00", None, None, None,
                         "RECLAIM", "ERROR", "race", "{}", "2000-01-01T00:00:00+00:00"))

    reclaimed = store.nx_reclaim_stale_running("2020-01-01T00:00:00+00:00", failure_code="STALE",
                                               failure_reason="crashed", _probe=probe)
    assert reclaimed == ["stale"]                                # did not crash on the pre-existing event PK
    assert store.nx_get_run("stale").status == "FAILED"


def test_reclaim_stale_running_news_run():
    store = _store()
    store.nx_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", provider="AGG",
                        source_id="s")
    store.nx_advance_run_status("stale", "PLANNED", "RUNNING")
    with store.tx() as cur:
        store._exec(cur, "UPDATE news_import_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))
    reclaimed = store.nx_reclaim_stale_running("2020-01-01T00:00:00+00:00", failure_code="STALE",
                                               failure_reason="crashed")
    assert reclaimed == ["stale"] and store.nx_get_run("stale").status == "FAILED"
    assert any(e.event_type == "RECLAIM" for e in store.nx_list_run_events("stale"))


# --------------------------------------------------------------------- observability
def test_readmodel_never_claims_full_coverage():
    from atp.newsroom import news_health
    store = _store()
    _run(store, _prov([[_item("m1", "A"), _item("m1b", "A")]]))   # one exact dup
    h = news_health(store)
    assert h["sources"]["coverage_partial"] is True              # only some sources active
    assert h["sources"]["missing_sources"] and h["dedup"]["duplicate_rate"] > 0
