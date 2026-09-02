"""§ WP7 acceptance — the fundamentals & macro-series pipeline end-to-end over the REAL store.

Proves every safety-critical guarantee: migration 30, series + immutable observations, fail-closed
instrument linkage (NONE/VERIFIED/AMBIGUOUS/UNMAPPED), the REVISION lifecycle (original immutable, current
derived), dedup independent of the license gate, no fabrication of a missing value, fail-closed timestamps,
license-gated value storage (with the license-status cross-check), provider outage/rate-limit handling,
cursor pagination/idempotency/resume, per-message dead-letter + cursor hold, straggler-safe counters,
per-region isolation, immutability, reclaim, and read-only coverage/health observability.

SAFETY: research/reference data only — no orders/execution/broker/runtime/autonomous/risk path.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atp.core.enums import AssetClass
from atp.fundseries import (
    Frequency,
    FundamentalCategory,
    FundamentalIngestConfig,
    FundamentalItem,
    FundamentalObservation,
    LicenseMetadata,
    LicenseStatus,
    MappingHint,
    Primacy,
    StubFundamentalProvider,
    Unit,
    fundamentals_health,
    fundamentals_source_coverage,
    ingest_fundamentals,
    observation_id_for,
    series_id_for,
)
from atp.instruments.model import InstrumentRecord
from atp.store import open_store

NOW = "2026-09-16T00:00:00+00:00"
PUB = "2026-09-01T12:00:00+00:00"
LIC = LicenseMetadata(license_status=LicenseStatus.LICENSED_STORE_ONLY, storage_allowed=True)


def _store():
    s = open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))
    from atp.fundseries import seed_registry
    seed_registry(s)
    return s


def _inst(store, symbol, exchange="NASDAQ", isin=None, con_id=None):
    rec = InstrumentRecord(symbol=symbol, asset_class=AssetClass.EQUITY, exchange=exchange,
                           trading_currency="USD", region="AMERICAS", country="US",
                           timezone="America/New_York", trading_calendar="us_equity", multiplier="1",
                           primary_exchange=exchange, isin=isin, con_id=con_id, source="t")
    store.im_upsert_instrument(rec.as_record())
    return rec.instrument_id


def _item(skey, pid, *, cat=FundamentalCategory.INFLATION, metric="CPI_YOY", unit=Unit.PERCENT,
          freq=Frequency.MONTHLY, region="AMERICAS", country="US", currency=None, period="2026-08",
          value=3.2, value_text=None, rev=0, rev_of=None, prelim=False, hints=(), pub=PUB,
          primacy=Primacy.PRIMARY):
    return FundamentalItem(series_key=skey, provider_id=pid, category=cat, metric=metric, unit=unit,
                           frequency=freq, region=region, country=country, currency=currency, period=period,
                           value=value, value_text=value_text, revision_seq=rev, revision_of_provider_id=rev_of,
                           is_preliminary=prelim, mapping_hints=hints, published_at=pub, primacy=primacy)


def _prov(pages, **kw):
    return StubFundamentalProvider(name="BLS", source_id="us_bls", license=LIC, pages=pages, **kw)


def _run(store, prov, label="d", **cfg):
    return ingest_fundamentals(store, prov, run_label=label, config=FundamentalIngestConfig(page_limit=50, **cfg),
                               now=NOW)


# --------------------------------------------------------------------- migration
def test_migration_30_applied_and_legacy_untouched():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {26, 27, 29, 30} <= versions
    for tbl in ("fundamental_sources", "fundamental_series", "fundamental_series_instruments",
                "fundamental_observations"):
        assert store._all(f"SELECT COUNT(*) FROM {tbl}")[0][0] >= 0
    store._one("SELECT message_id FROM news_messages LIMIT 0")       # WP5 tables present
    store._one("SELECT message_id FROM macro_events LIMIT 0")        # WP6 tables present
    store._one("SELECT id,symbol,title FROM news_items LIMIT 0")     # legacy untouched


# --------------------------------------------------------------------- series + observation
def test_series_and_observation_persisted():
    store = _store()
    s = _run(store, _prov([[_item("CPIAUCSL", "o1", value=3.2)]]))
    assert s.status == "COMPLETED" and s.stored == 1
    sid = series_id_for("us_bls", "CPIAUCSL")
    ser = store.fx_get_series(sid)
    assert ser is not None and ser.category == "INFLATION" and ser.unit == "PERCENT" and ser.region == "AMERICAS"
    obs = store.fx_get_observation(observation_id_for("BLS", "o1"))
    assert obs.value == "3.2" and obs.value_status == "OK" and obs.period == "2026-08"
    assert obs.series_id == sid          # the observation references its series (which carries link_status)


def test_numeric_value_normalized_end_to_end():
    store = _store()
    _run(store, _prov([[_item("S", "o", value=3.20)]]))               # 3.20 → canonical 3.2
    assert store.fx_get_observation(observation_id_for("BLS", "o")).value == "3.2"


# --------------------------------------------------------------------- fail-closed instrument linkage
def test_link_status_none_for_macro_series():
    store = _store()
    _run(store, _prov([[_item("CPIAUCSL", "o1")]]))                   # macro series, no instrument hints
    assert store.fx_get_series(series_id_for("us_bls", "CPIAUCSL")).link_status == "NONE"


def test_link_status_verified_ambiguous_unmapped():
    store = _store()
    _inst(store, "AAPL", exchange="NASDAQ", con_id=111)
    _inst(store, "AAPL", exchange="LSE", con_id=222)
    prov = _prov([[
        _item("AAPL_REV", "v", cat=FundamentalCategory.EARNINGS, metric="REVENUE",
              hints=(MappingHint(con_id=111),)),
        _item("AAPL_SYM", "a", cat=FundamentalCategory.EARNINGS, metric="REVENUE",
              hints=(MappingHint(symbol="AAPL"),)),
        _item("XYZ", "u", cat=FundamentalCategory.EARNINGS, metric="REVENUE",
              hints=(MappingHint(con_id=999),)),
    ]])
    _run(store, prov)
    assert store.fx_get_series(series_id_for("us_bls", "AAPL_REV")).link_status == "VERIFIED"
    assert store.fx_get_series(series_id_for("us_bls", "AAPL_SYM")).link_status == "AMBIGUOUS"
    assert store.fx_get_series(series_id_for("us_bls", "XYZ")).link_status == "UNMAPPED"
    links = store.fx_list_series_instruments(series_id_for("us_bls", "AAPL_REV"))
    assert len(links) == 1 and links[0].mapping_status == "VERIFIED"


def test_series_link_status_is_derived_from_stored_rows_order_independent():
    """Regression: a series's link_status is DERIVED from the immutable stored mapping rows (not a per-
    observation recompute), so heterogeneous hints for one series_key can never yield an order-dependent
    FABRICATED VERIFIED — the summary always agrees with the rows and is identical across observation order."""
    def run_order(first, second):
        store = _store()
        _inst(store, "AAPL", exchange="NASDAQ", con_id=111)
        _inst(store, "AAPL", exchange="LSE", con_id=222)          # same symbol, two exchanges
        items = {"sym": _item("X", "sym", cat=FundamentalCategory.EARNINGS, metric="REV",
                              hints=(MappingHint(symbol="AAPL"),)),         # symbol-only → AMBIGUOUS (2 rows)
                 "cid": _item("X", "cid", cat=FundamentalCategory.EARNINGS, metric="REV",
                              hints=(MappingHint(con_id=111),))}            # con_id → a single VERIFIED match
        _run(store, _prov([[items[first], items[second]]]))
        sid = series_id_for("us_bls", "X")
        return store.fx_get_series(sid).link_status, {r.mapping_status for r in store.fx_list_series_instruments(sid)}

    link_a, rows_a = run_order("sym", "cid")
    link_b, rows_b = run_order("cid", "sym")
    assert link_a == link_b == "AMBIGUOUS"                        # order-independent, never a fabricated VERIFIED
    # the derived summary agrees with its own stored rows in BOTH orders: an AMBIGUOUS row → not VERIFIED
    assert "AMBIGUOUS" in rows_a and link_a != "VERIFIED"
    assert "AMBIGUOUS" in rows_b and link_b != "VERIFIED"


def test_unmapped_link_status_is_sticky_and_macro_stays_none():
    """A series that once had a usable hint but no catalogue match stays UNMAPPED even when a later
    observation carries no hints (sticky) — while a series that never had a hint stays NONE."""
    store = _store()
    prov = _prov([[_item("U", "u1", cat=FundamentalCategory.EARNINGS, metric="REV",
                         hints=(MappingHint(con_id=999),)),                # hint, no match → UNMAPPED
                   _item("U", "u2", cat=FundamentalCategory.EARNINGS, metric="REV", period="2026-07"),   # no hints
                   _item("M", "m1")]])                                     # macro series, never any hint
    _run(store, prov)
    assert store.fx_get_series(series_id_for("us_bls", "U")).link_status == "UNMAPPED"   # sticky, not regressed
    assert store.fx_get_series(series_id_for("us_bls", "M")).link_status == "NONE"


# --------------------------------------------------------------------- revision lifecycle
def test_revision_lifecycle_original_immutable_current_derived():
    store = _store()
    prov = _prov([[
        _item("CPI", "o1", value=3.2, rev=0, pub="2026-09-01T12:00:00+00:00"),
        _item("CPI", "o1r1", value=3.3, rev=1, rev_of="o1", pub="2026-09-15T12:00:00+00:00"),
    ]])
    s = _run(store, prov)
    assert s.stored == 2 and s.revisions == 1
    sid = series_id_for("us_bls", "CPI")
    revs = store.fx_list_revisions(sid, "2026-08")
    assert [(r.revision_seq, r.value) for r in revs] == [(0, "3.2"), (1, "3.3")]     # both immutable rows kept
    cur = store.fx_current_observation(sid, "2026-08")
    assert cur.value == "3.3" and cur.revision_seq == 1                              # current is DERIVED
    o1 = store.fx_get_observation(observation_id_for("BLS", "o1"))
    assert o1.value == "3.2"                                                         # original never overwritten
    assert store.fx_get_observation(observation_id_for("BLS", "o1r1")).revision_of_id == o1.observation_id


# --------------------------------------------------------------------- dedup / no-fabrication
def test_dedup_independent_of_license_gate():
    ml = LicenseMetadata(license_status=LicenseStatus.NO_LICENSE, storage_allowed=False)   # value NOT stored
    store = _store()
    prov = StubFundamentalProvider(name="ML", source_id="imf_data", license=ml, pages=[[
        _item("GDP", "a", metric="GDP", value=2.1),
        _item("GDP", "b", metric="GDP", value=2.9)]])       # same series+period, different value → not a dup
    s = _run(store, prov)
    assert s.duplicates == 0
    assert store.fx_get_observation(observation_id_for("ML", "b")).duplicate_of_id is None
    assert store.fx_get_observation(observation_id_for("ML", "a")).value is None   # value not stored (unlicensed)


def test_missing_value_is_not_fabricated():
    store = _store()
    _run(store, _prov([[_item("S", "o", value=None)]]))              # provider gave no value
    obs = store.fx_get_observation(observation_id_for("BLS", "o"))
    assert obs.value is None and obs.value_status == "MISSING"       # never zero, never interpolated


def test_non_numeric_value_text_is_stored_as_non_numeric():
    store = _store()
    _run(store, _prov([[_item("RTG", "o", cat=FundamentalCategory.RATING_EVENT, metric="CREDIT_RATING",
                              unit=Unit.UNKNOWN, value=None, value_text="AA+")]]))
    obs = store.fx_get_observation(observation_id_for("BLS", "o"))
    assert obs.value is None and obs.value_text == "AA+" and obs.value_status == "NON_NUMERIC"


# --------------------------------------------------------------------- time integrity
def test_future_and_missing_timestamps_flagged():
    store = _store()
    _run(store, _prov([[_item("S", "f", pub="2999-01-01T00:00:00+00:00"),
                        _item("S", "n", pub=None)]]))
    assert store.fx_get_observation(observation_id_for("BLS", "f")).time_status == "FUTURE_CONFLICT"
    m = store.fx_get_observation(observation_id_for("BLS", "n"))
    assert m.time_status == "MISSING_PUBLISH" and m.published_at is None


# --------------------------------------------------------------------- license gating
def test_missing_license_stores_metadata_only():
    store = _store()
    prov = StubFundamentalProvider(name="NL", source_id="oecd_data", pages=[[_item("S", "x", value=5.5)]],
                                   license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE,
                                                           storage_allowed=False))
    _run(store, prov)
    obs = store.fx_get_observation(observation_id_for("NL", "x"))
    assert obs.storage_status == "STORED_METADATA_ONLY" and obs.value is None


def test_storage_allowed_flag_cannot_override_a_non_storable_license():
    store = _store()
    prov = StubFundamentalProvider(name="BUG", source_id="us_bls", pages=[[_item("S", "x", value=5.5)]],
                                   license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE,
                                                           storage_allowed=True))
    _run(store, prov)
    obs = store.fx_get_observation(observation_id_for("BUG", "x"))
    assert obs.storage_status == "STORED_METADATA_ONLY" and obs.value is None      # value NOT stored


# --------------------------------------------------------------------- provider states
def test_provider_unavailable_marks_source_and_fails_run():
    store = _store()
    s = _run(store, StubFundamentalProvider(name="D", source_id="eurostat", unavailable=True))
    assert s.status == "FAILED" and store.fx_get_source("eurostat").available is False


def test_unconfigured_provider_fails_closed():
    class Unconfigured(StubFundamentalProvider):
        @property
        def configured(self):
            return False
    store = _store()
    s = _run(store, Unconfigured(name="X", source_id="us_bls", pages=[[_item("S", "o")]]))
    assert s.status == "FAILED" and store.fx_count_observations() == 0


def test_rate_limited_is_partial_not_a_fresh_success():
    """Regression: a throttled pass must NOT be stamped a clean COMPLETED success, and must NOT leave the
    source looking freshly-healthy (no last_success bump) — it is a soft, resumable, incomplete pass."""
    store = _store()
    s = _run(store, _prov([[_item("S", "o")]], rate_limited=True))
    assert store.fx_count_observations() == 0                         # nothing fabricated on a rate-limit
    assert s.status == "PARTIAL"                                      # not a clean COMPLETED
    src = store.fx_get_source("us_bls")
    assert src.available is True                                      # the source is up (just throttled) …
    assert src.last_success_at is None and src.last_error == "provider rate-limited"   # … but NOT a fresh success


# --------------------------------------------------------------------- cursor / resume / idempotency
def test_cursor_pagination_and_idempotent_resume():
    store = _store()
    prov = _prov([[_item("S", "o1")], [_item("S", "o2", period="2026-07")],
                  [_item("S", "o3", period="2026-06")]])
    s = _run(store, prov)
    assert s.status == "COMPLETED" and s.stored == 3 and prov.calls == [None, "1", "2"]
    s2 = _run(store, _prov([[_item("S", "o1")], [_item("S", "o2", period="2026-07")],
                            [_item("S", "o3", period="2026-06")]]))
    assert s2.stored == 0 and store.fx_count_observations() == 3      # idempotent per observation_id


def test_parallel_workers_do_not_collide():
    store = _store()
    a = ingest_fundamentals(store, _prov([[_item("S", "o1"), _item("S", "o2", period="2026-07")]]),
                            run_label="d", run_id="run-a", now=NOW)
    b = ingest_fundamentals(store, _prov([[_item("S", "o1"), _item("S", "o2", period="2026-07")]]),
                            run_label="d", run_id="run-b", now=NOW)
    assert a.stored == 2 and b.stored == 0 and store.fx_count_observations() == 2


# --------------------------------------------------------------------- per-message / per-region isolation
class _OneObsFailStore:
    """Proxy whose fx_insert_observation raises a TRANSIENT error for ONE observation while the error-path
    bump SUCCEEDS — the failure stays per-message (ok=False). `heal()` stops failing."""

    def __init__(self, store, bad_observation_id):
        self._s = store
        self._bad = bad_observation_id
        self._healed = False

    def heal(self):
        self._healed = True

    def __getattr__(self, name):
        return getattr(self._s, name)

    def fx_insert_observation(self, record, **kw):
        if not self._healed and record.get("observation_id") == self._bad:
            raise RuntimeError("transient lock timeout")
        return self._s.fx_insert_observation(record, **kw)


def test_observation_error_holds_cursor_and_is_recoverable():
    store = _store()
    bad = observation_id_for("BLS", "b")
    proxy = _OneObsFailStore(store, bad)
    prov = _prov([[_item("S", "a", region="AMERICAS", country="US"),
                   _item("S", "b", region="AMERICAS", country="US"),
                   _item("S", "c", region="EUROPE", country="DE")]])
    s = ingest_fundamentals(proxy, prov, run_label="d", config=FundamentalIngestConfig(page_limit=50), now=NOW)
    assert s.status == "PARTIAL" and s.error == 1
    assert s.completed_regions == ["EUROPE"] and s.failed_regions == ["AMERICAS"]
    assert store.fx_get_observation(observation_id_for("BLS", "a")) is not None    # sibling stored
    assert store.fx_get_observation(bad) is None                                  # failed one NOT stored
    assert store.nx_get_run(s.run_id).cursor is None                             # cursor HELD
    errs = [e for e in store.nx_list_run_events(s.run_id) if e.event_type == "OBSERVATION_ERROR"]
    assert errs and errs[0].message_id == bad and "provider_id=b" in (errs[0].reason or "")
    proxy.heal()
    s2 = ingest_fundamentals(proxy, prov, run_label="d",
                             config=FundamentalIngestConfig(page_limit=50, start_cursor=store.nx_get_run(s.run_id).cursor),
                             now=NOW)
    assert store.fx_get_observation(bad) is not None and s2.status == "COMPLETED"


def test_straggler_write_to_a_reclaimed_run_does_not_desync_it():
    store = _store()
    store.nx_create_run(run_id="R", request_checksum="sha256:x", run_label="l", provider="BLS",
                        source_id="us_bls")
    store.nx_advance_run_status("R", "PLANNED", "RUNNING")
    store.nx_finalize_run("R", status="FAILED", failure_code="STALE", failure_reason="reclaimed")
    events_before = len(store.nx_list_run_events("R"))
    # a series must exist for the observation FK
    from atp.fundseries import FundamentalSeries
    store.fx_upsert_series(FundamentalSeries(source_id="us_bls", series_key="S").as_record())
    obs = FundamentalObservation(series_id=series_id_for("us_bls", "S"), provider="BLS", provider_id="late",
                                 source_id="us_bls", period="2026-08", value="1.0")
    res = store.fx_insert_observation(obs.as_record(), run_id="R",
                                      run_event={"id": "R-late", "event_type": "OBSERVATION_STORED",
                                                 "severity": "INFO", "message_id": obs.observation_id})
    assert res == "inserted"
    assert store.fx_get_observation(obs.observation_id) is not None              # global observation stored
    run = store.nx_get_run("R")
    assert run.status == "FAILED" and run.stored_count == 0                      # frozen run's counters unchanged
    assert len(store.nx_list_run_events("R")) == events_before                  # no event appended


# --------------------------------------------------------------------- immutability / reclaim
def test_observation_and_series_instruments_are_immutable():
    store = _store()
    _inst(store, "AAPL", con_id=111)
    _run(store, _prov([[_item("AAPL_REV", "o1", cat=FundamentalCategory.EARNINGS, metric="REVENUE",
                              hints=(MappingHint(con_id=111),))]]))
    oid = observation_id_for("BLS", "o1")
    sid = series_id_for("us_bls", "AAPL_REV")
    for sql in (f"UPDATE fundamental_observations SET value='9' WHERE observation_id='{oid}'",
                f"DELETE FROM fundamental_observations WHERE observation_id='{oid}'",
                f"UPDATE fundamental_series_instruments SET mapping_status='X' WHERE series_id='{sid}'"):
        with pytest.raises(Exception), store.tx() as cur:  # noqa: B017
            store._exec(cur, sql)


def test_reclaim_stale_running_fundamentals_run():
    store = _store()
    store.nx_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", provider="BLS",
                        source_id="us_bls")
    store.nx_advance_run_status("stale", "PLANNED", "RUNNING")
    with store.tx() as cur:
        store._exec(cur, "UPDATE news_import_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))
    reclaimed = store.nx_reclaim_stale_running("2020-01-01T00:00:00+00:00", failure_code="STALE",
                                               failure_reason="crashed")
    assert reclaimed == ["stale"] and store.nx_get_run("stale").status == "FAILED"


# --------------------------------------------------------------------- read-only observability
def test_seed_registry_is_fail_closed():
    store = _store()
    sources = store.fx_list_sources()
    assert len(sources) == 11
    assert all((not s.available) and s.license_status == "UNKNOWN" and (not s.storage_allowed)
               and (not s.redistribution_allowed) and (not s.commercial_use_allowed) for s in sources)


def test_reseeding_preserves_source_operational_history():
    """Regression: re-running seed_registry after a successful ingest must PRESERVE a source's
    last_success_at/last_error (operational history owned by fx_mark_source_result), not wipe it — parity
    with the newsroom source registry."""
    from atp.fundseries import seed_registry
    store = _store()
    _run(store, _prov([[_item("S", "o")]]))                 # marks us_bls available + last_success_at
    before = store.fx_get_source("us_bls")
    assert before.available is True and before.last_success_at is not None
    seed_registry(store)                                    # re-seed the registry
    after = store.fx_get_source("us_bls")
    assert after.last_success_at == before.last_success_at   # operational history preserved (not wiped)
    assert after.last_error == before.last_error


def test_readmodel_reports_partial_coverage_and_breakdowns():
    store = _store()
    cov = fundamentals_source_coverage(store)
    assert cov["coverage_partial"] is True and cov["active_sources"] == []
    assert "STATISTICS_OFFICE" in cov["by_source_type"]
    _run(store, _prov([[_item("CPI", "o1", value=3.2),
                        _item("GDP", "g1", cat=FundamentalCategory.GDP, metric="GDP", value=None)]]))
    h = fundamentals_health(store)
    assert h["series"] == 2 and h["observations"] == 2
    assert h["by_value_status"] == {"MISSING": 1, "OK": 1}
    assert h["by_series_link_status"]["NONE"] == 2
    assert "us_bls" in h["sources"]["active_sources"] and h["sources"]["coverage_partial"] is True
    assert h["recent_runs"] and all(r["source_id"] == "us_bls" for r in h["recent_runs"])
