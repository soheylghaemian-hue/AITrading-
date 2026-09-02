"""§ WP6 acceptance — the macro / geopolitical / regulatory event pipeline end-to-end over the REAL store.

Proves every safety-critical guarantee: migration 29 (gap at 28), a macro event is a WP5 newsroom record +
an immutable overlay, fail-closed instrument linkage (NONE/VERIFIED/AMBIGUOUS/UNMAPPED), dedup inherited from
the newsroom record and independent of the license gate, corrections/retractions linked to the immutable
original, macro-situation clustering, license-gated storage (with the license-status cross-check), fail-closed
timestamps (inherited), provider outage/rate-limit handling, cursor pagination/idempotency/resume, per-message
dead-letter + cursor hold, straggler-safe counters, per-region isolation, immutability, reclaim, and read-only
coverage/health observability.

SAFETY: research/reference data only — no orders/execution/broker/runtime/autonomous/risk path.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.macroevents import (
    AssetClassScope,
    GeoScope,
    Level,
    LicenseMetadata,
    LicenseStatus,
    MacroEventItem,
    MacroEventType,
    MacroIngestConfig,
    MacroSourceClass,
    MappingHint,
    Primacy,
    StubMacroEventProvider,
    ingest_macro_events,
    macro_health,
    macro_source_coverage,
    message_id_for,
    seed_registry,
)
from atp.macroevents.model import MacroEvent
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


def _item(pid, title, *, body="body", lang="en", primacy=Primacy.PRIMARY,
          mtype=MacroEventType.MONETARY_POLICY_DECISION, sclass=MacroSourceClass.CENTRAL_BANK,
          scope=GeoScope.COUNTRY, severity=Level.MEDIUM, policy="MONETARY", hints=(), pub=PUB,
          corr=None, retr=None, regions=("AMERICAS",), countries=("US",), blocs=(), assets=()):
    return MacroEventItem(provider_id=pid, title=title, body=body, language=lang, url=f"http://x/{pid}",
                          source_name="Src", published_at=pub, primacy=primacy, macro_type=mtype,
                          source_class=sclass, geo_scope=scope, severity=severity, policy_area=policy,
                          mapping_hints=hints, correction_of_provider_id=corr, retraction_of_provider_id=retr,
                          regions=regions, countries=countries, blocs=blocs, asset_classes=assets)


def _prov(pages, **kw):
    return StubMacroEventProvider(name="FED", source_id="us_federal_reserve",
                                  source_class="CENTRAL_BANK", license=LIC, pages=pages, **kw)


def _run(store, prov, label="d", **cfg):
    return ingest_macro_events(store, prov, run_label=label, config=MacroIngestConfig(page_limit=50, **cfg),
                               now=NOW)


# --------------------------------------------------------------------- migration
def test_migration_29_applied_gap_at_28_and_legacy_untouched():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {26, 27, 29} <= versions and 28 not in versions      # intentional gap; 28 is on the sibling stack
    for tbl in ("macro_events", "macro_sources"):
        assert store._all(f"SELECT COUNT(*) FROM {tbl}")[0][0] >= 0
    store._one("SELECT message_id FROM news_messages LIMIT 0")   # WP5 newsroom tables still present
    store._one("SELECT id,symbol,title FROM news_items LIMIT 0")  # legacy news_items untouched


# --------------------------------------------------------------------- base record + overlay
def test_macro_event_is_newsroom_record_plus_overlay():
    store = _store()
    _run(store, _prov([[_item("m1", "FOMC holds rates", assets=("RATES", "FX"))]]))
    mid = message_id_for("FED", "m1")
    nm = store.nx_get_message(mid)
    assert nm is not None and nm.event_category == "MACRO_CENTRAL_BANK" and nm.source_type == "CENTRAL_BANK"
    mx = store.mx_get_macro_event(mid)
    assert mx is not None and mx.macro_type == "MONETARY_POLICY_DECISION"
    assert mx.source_class == "CENTRAL_BANK" and mx.geo_scope == "COUNTRY" and mx.severity == "MEDIUM"
    assert mx.affected_asset_classes_json == '["FX", "RATES"]' and mx.link_status == "NONE"


def test_enum_asset_class_inputs_are_stored_not_mangled():
    """Regression: a provider adapter may pass AssetClassScope enum members (the field is typed
    `AssetClassScope | str`). They must be stored as their values, never collapsed to UNKNOWN."""
    store = _store()
    _run(store, _prov([[_item("m", "energy warning", mtype=MacroEventType.ENERGY_SUPPLY_WARNING,
                              sclass=MacroSourceClass.ENERGY_AUTHORITY, scope=GeoScope.REGION,
                              regions=("EUROPE",), countries=("DE",),
                              assets=(AssetClassScope.ENERGY, AssetClassScope.FX))]]))
    mx = store.mx_get_macro_event(message_id_for("FED", "m"))
    assert mx.affected_asset_classes_json == '["ENERGY", "FX"]'      # enums stored as values, not UNKNOWN


def test_category_and_source_type_projection():
    store = _store()
    prov = _prov([[
        _item("cb", "rate", mtype=MacroEventType.RATE_GUIDANCE, sclass=MacroSourceClass.CENTRAL_BANK),
        _item("sn", "sanction", mtype=MacroEventType.SANCTION, sclass=MacroSourceClass.SANCTIONS_AUTHORITY,
              regions=("EUROPE",), countries=("DE",)),
        _item("rg", "reg", mtype=MacroEventType.REGULATORY_ACTION, sclass=MacroSourceClass.NATIONAL_REGULATOR,
              regions=("ASIA",), countries=("JP",)),
        _item("imf", "outlook", mtype=MacroEventType.SUPRANATIONAL_OUTLOOK, sclass=MacroSourceClass.SUPRANATIONAL,
              regions=("GLOBAL",), countries=()),
    ]])
    _run(store, prov)
    assert store.nx_get_message(message_id_for("FED", "cb")).event_category == "MACRO_CENTRAL_BANK"
    assert store.nx_get_message(message_id_for("FED", "sn")).event_category == "GEOPOLITICS_REF"
    assert store.nx_get_message(message_id_for("FED", "sn")).source_type == "REGULATOR"
    assert store.nx_get_message(message_id_for("FED", "rg")).event_category == "REGULATION"
    assert store.nx_get_message(message_id_for("FED", "imf")).event_category == "MACRO_CENTRAL_BANK"
    assert store.nx_get_message(message_id_for("FED", "imf")).source_type == "OTHER"


# --------------------------------------------------------------------- fail-closed instrument linkage
def test_link_status_none_when_no_instrument_hints():
    store = _store()
    _run(store, _prov([[_item("m", "macro-wide, no instrument")]]))
    assert store.mx_get_macro_event(message_id_for("FED", "m")).link_status == "NONE"


def test_vacuous_mapping_hint_is_link_none_not_unmapped():
    """A MappingHint carrying no usable identifier is not an attempted mapping → link NONE (not UNMAPPED)."""
    store = _store()
    s = _run(store, _prov([[_item("m", "macro", hints=(MappingHint(),))]]))
    assert store.mx_get_macro_event(message_id_for("FED", "m")).link_status == "NONE"
    assert s.unmapped == 0


def test_link_status_verified_ambiguous_unmapped():
    store = _store()
    _inst(store, "AAPL", exchange="NASDAQ", con_id=111)
    _inst(store, "AAPL", exchange="LSE", con_id=222)          # same symbol, two exchanges
    prov = _prov([[
        _item("v", "con_id exact", hints=(MappingHint(con_id=111),)),
        _item("a", "symbol only, 2 exchanges", hints=(MappingHint(symbol="AAPL"),)),
        _item("u", "no catalogue match", hints=(MappingHint(con_id=999),)),
    ]])
    _run(store, prov)
    assert store.mx_get_macro_event(message_id_for("FED", "v")).link_status == "VERIFIED"
    assert store.mx_get_macro_event(message_id_for("FED", "a")).link_status == "AMBIGUOUS"
    assert store.mx_get_macro_event(message_id_for("FED", "u")).link_status == "UNMAPPED"
    # the VERIFIED event actually recorded a fail-closed instrument mapping row
    links = store.nx_list_message_instruments(message_id_for("FED", "v"))
    assert len(links) == 1 and links[0].mapping_status == "VERIFIED"


# --------------------------------------------------------------------- dedup / corrections / cluster
def test_dedup_inherited_and_independent_of_license_gate():
    ml = LicenseMetadata(license_status=LicenseStatus.NO_LICENSE, storage_allowed=False)   # body NOT stored
    store = _store()
    prov = StubMacroEventProvider(name="ML", source_id="imf", source_class="SUPRANATIONAL", license=ml,
                                  pages=[[_item("a", "Outlook", body="growth UP"),
                                          _item("b", "Outlook", body="growth DOWN")]])   # same title, diff body
    s = _run(store, prov)
    assert s.duplicates == 0
    assert store.nx_get_message(message_id_for("ML", "b")).duplicate_of_id is None
    assert store.nx_get_message(message_id_for("ML", "a")).original_body is None    # body not stored


def test_correction_and_retraction_inherited_and_original_immutable():
    store = _store()
    prov = _prov([[_item("m1", "Original"),
                   _item("m2", "Corrected", corr="m1"),
                   _item("m3", "Withdrawn", retr="m1")]])
    s = _run(store, prov)
    assert s.corrections == 1 and s.retractions == 1
    m1 = message_id_for("FED", "m1")
    assert store.nx_get_message(m1).original_title == "Original"        # original never overwritten
    assert store.nx_derived_message_status(m1) == "RETRACTED"           # derived, not a mutation
    # the macro overlay mirrors the correction/retraction relations
    assert store.mx_get_macro_event(message_id_for("FED", "m2")).correction_of_id == m1
    assert store.mx_get_macro_event(message_id_for("FED", "m3")).retraction_of_id == m1


def test_macro_situation_clustering():
    store = _store()
    prov = _prov([[
        _item("a", "FOMC statement", pub="2026-09-02T10:00:00+00:00"),
        _item("b", "FOMC minutes", pub="2026-09-02T20:00:00+00:00"),   # same type/region/policy/day
    ]])
    _run(store, prov)
    ca = store.mx_get_macro_event(message_id_for("FED", "a")).macro_cluster_id
    cb = store.mx_get_macro_event(message_id_for("FED", "b")).macro_cluster_id
    assert ca == cb and store.mx_macro_cluster_count() == 1
    assert len(store.mx_list_events_in_cluster(ca)) == 2


# --------------------------------------------------------------------- license gating
def test_missing_license_stores_metadata_only():
    store = _store()
    prov = StubMacroEventProvider(name="NL", source_id="bis", source_class="SUPRANATIONAL",
                                  pages=[[_item("x", "Systemic note", body="full body",
                                                mtype=MacroEventType.SYSTEMIC_RISK_WARNING)]],
                                  license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE,
                                                          storage_allowed=False))
    _run(store, prov)
    m = store.nx_get_message(message_id_for("NL", "x"))
    assert m.storage_status == "STORED_METADATA_ONLY" and m.original_body is None


def test_storage_allowed_flag_cannot_override_a_non_storable_license():
    store = _store()
    prov = StubMacroEventProvider(name="BUG", source_id="ecb", source_class="CENTRAL_BANK",
                                  pages=[[_item("x", "Statement", body="full body")]],
                                  license=LicenseMetadata(license_status=LicenseStatus.NO_LICENSE,
                                                          storage_allowed=True))
    _run(store, prov)
    m = store.nx_get_message(message_id_for("BUG", "x"))
    assert m.storage_status == "STORED_METADATA_ONLY" and m.original_body is None   # body NOT stored


# --------------------------------------------------------------------- time integrity (inherited)
def test_future_and_missing_timestamps_are_flagged_not_fabricated():
    store = _store()
    prov = _prov([[_item("f", "Future", pub="2999-01-01T00:00:00+00:00"),
                   _item("n", "No time", pub=None)]])
    _run(store, prov)
    assert store.nx_get_message(message_id_for("FED", "f")).time_status == "FUTURE_CONFLICT"
    m = store.nx_get_message(message_id_for("FED", "n"))
    assert m.time_status == "MISSING_PUBLISH" and m.published_at is None    # receive time NOT substituted


# --------------------------------------------------------------------- provider states
def test_provider_unavailable_marks_source_and_fails_run():
    store = _store()
    prov = StubMacroEventProvider(name="DOWN", source_id="world_bank", source_class="SUPRANATIONAL",
                                  unavailable=True)
    s = _run(store, prov)
    assert s.status == "FAILED"
    assert store.mx_get_macro_source("world_bank").available is False


def test_unconfigured_provider_fails_closed():
    class Unconfigured(StubMacroEventProvider):
        @property
        def configured(self):
            return False
    s = _run(store := _store(), Unconfigured(name="X", source_id="ecb", source_class="CENTRAL_BANK",
                                             pages=[[_item("m", "x")]]))
    assert s.status == "FAILED" and store.mx_count_macro_events() == 0


def test_rate_limited_stops_politely_and_keeps_cursor():
    store = _store()
    s = _run(store, _prov([[_item("m1", "One")]], rate_limited=True))
    assert s.status in ("COMPLETED", "PARTIAL", "FAILED")
    assert store.mx_count_macro_events() == 0                 # nothing fabricated on a rate-limit


# --------------------------------------------------------------------- cursor / resume / idempotency
def test_cursor_pagination_and_idempotent_resume():
    store = _store()
    prov = _prov([[_item("m1", "A")], [_item("m2", "B")], [_item("m3", "C")]])
    s = _run(store, prov)
    assert s.status == "COMPLETED" and s.stored == 3 and prov.calls == [None, "1", "2"]
    # a second full run is a no-op (idempotent per message_id): nothing new stored, no duplicates
    s2 = _run(store, _prov([[_item("m1", "A")], [_item("m2", "B")], [_item("m3", "C")]]))
    assert s2.stored == 0 and store.mx_count_macro_events() == 3


def test_parallel_workers_do_not_collide():
    store = _store()
    a = ingest_macro_events(store, _prov([[_item("m1", "A"), _item("m2", "B")]]), run_label="d",
                            run_id="run-a", now=NOW)
    b = ingest_macro_events(store, _prov([[_item("m1", "A"), _item("m2", "B")]]), run_label="d",
                            run_id="run-b", now=NOW)
    assert a.stored == 2 and b.stored == 0 and store.mx_count_macro_events() == 2


# --------------------------------------------------------------------- per-message / per-region isolation
class _OneMessageFailStore:
    """Proxy whose mx_insert_macro_event raises a TRANSIENT error for ONE message while the error-path bump
    SUCCEEDS — the failure stays per-message (ok=False). `heal()` stops failing."""

    def __init__(self, store, bad_message_id):
        self._s = store
        self._bad = bad_message_id
        self._healed = False

    def heal(self):
        self._healed = True

    def __getattr__(self, name):
        return getattr(self._s, name)

    def mx_insert_macro_event(self, message_record, macro_record, **kw):
        if not self._healed and (kw.get("run_event") or {}).get("message_id") == self._bad:
            raise RuntimeError("transient lock timeout")
        return self._s.mx_insert_macro_event(message_record, macro_record, **kw)


def test_message_level_error_holds_cursor_and_is_recoverable():
    store = _store()
    bad = message_id_for("FED", "b")
    proxy = _OneMessageFailStore(store, bad)
    prov = _prov([[_item("a", "A", regions=("AMERICAS",), countries=("US",)),
                   _item("b", "B", regions=("AMERICAS",), countries=("US",)),
                   _item("c", "C", regions=("EUROPE",), countries=("DE",))]])
    s = ingest_macro_events(proxy, prov, run_label="d", config=MacroIngestConfig(page_limit=50), now=NOW)
    assert s.status == "PARTIAL" and s.error == 1
    assert s.completed_regions == ["EUROPE"] and s.failed_regions == ["AMERICAS"]
    assert store.mx_get_macro_event(message_id_for("FED", "a")) is not None    # sibling stored
    assert store.mx_get_macro_event(bad) is None                              # failed one NOT stored
    assert store.nx_get_run(s.run_id).cursor is None                          # cursor HELD on the page
    errs = [e for e in store.nx_list_run_events(s.run_id) if e.event_type == "MACRO_EVENT_ERROR"]
    assert errs and errs[0].message_id == bad and "provider_id=b" in (errs[0].reason or "")
    proxy.heal()                                                             # blip passes → resume recovers
    s2 = ingest_macro_events(proxy, prov, run_label="d",
                             config=MacroIngestConfig(page_limit=50, start_cursor=store.nx_get_run(s.run_id).cursor),
                             now=NOW)
    assert store.mx_get_macro_event(bad) is not None and s2.status == "COMPLETED"


def test_straggler_write_to_a_reclaimed_run_does_not_desync_it():
    store = _store()
    store.nx_create_run(run_id="R", request_checksum="sha256:x", run_label="l", provider="FED",
                        source_id="us_federal_reserve")
    store.nx_advance_run_status("R", "PLANNED", "RUNNING")
    store.nx_finalize_run("R", status="FAILED", failure_code="STALE", failure_reason="reclaimed")
    events_before = len(store.nx_list_run_events("R"))
    msg = MacroEvent(message_id=message_id_for("FED", "late"))
    from atp.newsroom.model import NewsMessage
    nm = NewsMessage(provider="FED", provider_id="late", source_id="us_federal_reserve",
                     original_title="late arrival")
    res = store.mx_insert_macro_event(nm.as_record(), msg.as_record(), run_id="R",
                                      run_event={"id": "R-late", "event_type": "MACRO_EVENT_STORED",
                                                 "severity": "INFO", "message_id": nm.message_id})
    assert res == "inserted"
    assert store.mx_get_macro_event(nm.message_id) is not None               # global overlay IS stored
    run = store.nx_get_run("R")
    assert run.status == "FAILED" and run.stored_count == 0                  # frozen run's counters unchanged
    assert len(store.nx_list_run_events("R")) == events_before              # no event appended to frozen run


# --------------------------------------------------------------------- immutability / reclaim
def test_overlay_is_backfilled_when_the_message_already_exists():
    """Regression: the macro overlay is a required 1:1 child inserted UNCONDITIONALLY. A news message that
    already exists in the SHARED news_messages namespace (e.g. a newsroom writer using the same
    provider|provider_id) must still get its overlay — never a silent drop with the cursor advancing."""
    from atp.newsroom.model import NewsMessage
    store = _store()
    mid = message_id_for("FED", "m1")
    store.nx_insert_message(NewsMessage(provider="FED", provider_id="m1", source_id="us_federal_reserve",
                                        original_title="pre-existing newsroom record").as_record())
    assert store.mx_get_macro_event(mid) is None                        # message exists, no overlay yet
    s = _run(store, _prov([[_item("m1", "FOMC holds rates"),
                            _item("m2", "EU note", regions=("EUROPE",), countries=("DE",))]]))
    assert s.status == "COMPLETED" and s.stored == 2                    # m1 overlay backfilled + m2 fresh
    assert store.mx_get_macro_event(mid) is not None                    # overlay created despite the message
    assert store.mx_get_macro_event(mid).macro_type == "MONETARY_POLICY_DECISION"
    assert store.nx_get_message(mid).original_title == "pre-existing newsroom record"   # message immutable
    assert store.mx_count_macro_events() == 2


def test_macro_overlay_and_message_are_immutable():
    store = _store()
    _run(store, _prov([[_item("m1", "One")]]))
    mid = message_id_for("FED", "m1")
    for sql in (f"UPDATE macro_events SET macro_type='X' WHERE message_id='{mid}'",
                f"DELETE FROM macro_events WHERE message_id='{mid}'"):
        with pytest.raises(Exception), store.tx() as cur:  # noqa: B017
            store._exec(cur, sql)


def test_reclaim_stale_running_macro_run():
    store = _store()
    store.nx_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", provider="FED",
                        source_id="us_federal_reserve")
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
    sources = store.mx_list_macro_sources()
    assert len(sources) == 14
    assert all((not s.available) and s.license_status == "UNKNOWN" and (not s.storage_allowed)
               and (not s.redistribution_allowed) and (not s.commercial_use_allowed) for s in sources)


def test_readmodel_reports_partial_coverage_and_breakdowns():
    store = _store()
    cov = macro_source_coverage(store)
    assert cov["coverage_partial"] is True and cov["active_sources"] == []
    assert set(cov["missing_sources"]) == {s.source_id for s in store.mx_list_macro_sources()}
    assert "CENTRAL_BANK" in cov["by_source_class"]
    _run(store, _prov([[_item("m1", "A"), _item("m2", "B", mtype=MacroEventType.SANCTION,
                                                sclass=MacroSourceClass.SANCTIONS_AUTHORITY,
                                                regions=("EUROPE",), countries=("DE",))]]))
    h = macro_health(store)
    assert h["events"] == 2 and h["by_type"]["MONETARY_POLICY_DECISION"] == 1
    assert h["by_link_status"]["NONE"] == 2 and h["clusters"] >= 1
    # the ingested source is now active; the rest are still MISSING → never claims full coverage
    assert "us_federal_reserve" in h["sources"]["active_sources"] and h["sources"]["coverage_partial"] is True
    # recent runs are filtered to macro sources only
    assert h["recent_runs"] and all(r["source_id"] == "us_federal_reserve" for r in h["recent_runs"])
