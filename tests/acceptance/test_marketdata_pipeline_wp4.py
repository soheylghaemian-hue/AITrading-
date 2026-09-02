"""§ WP4 acceptance — provider-neutral, persistent, fault-tolerant market-data pipeline (DATA ONLY).

End-to-end over the durable store + ingest orchestrator with a deterministic stub provider:
  * migration 28 applies; the six additive tables exist and market_data_health/ohlc_bars are untouched;
  * ONLY WP3-VERIFIED instruments are ingested (fail-closed); a free/unentitled REALTIME claim is downgraded;
  * the CURRENT quote is monotonic (late/out-of-order/backward packets never overwrite the newer row);
  * immutable append-only history/bars/corporate-actions with duplicate detection; DB-frozen terminal runs;
  * observable run counters + append-only audit events; per-instrument AND per-market error isolation;
  * explicit provider availability/entitlement/license storage; bars + corporate actions; stale-run reclaim.

SAFETY: no orders/execution/account, no subscription purchase, no real network.
"""
from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from atp.core.enums import AssetClass
from atp.instruments.model import InstrumentRecord
from atp.marketdata import (
    DataStatus,
    IngestConfig,
    LicenseType,
    ProviderBar,
    ProviderCorporateAction,
    ProviderQuote,
    StubMarketDataProvider,
    ingest_market_data,
)
from atp.store import open_store

T0 = "2026-09-02T13:30:00+00:00"
NOW = "2026-09-02T13:30:01+00:00"


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _verified(store, symbol, *, exchange="NASDAQ", verified=True, con_id=None):
    rec = InstrumentRecord(symbol=symbol, asset_class=AssetClass.EQUITY, exchange=exchange,
                           trading_currency="USD", region="AMERICAS", country="US",
                           timezone="America/New_York", trading_calendar="us_equity", multiplier="1",
                           primary_exchange=exchange, con_id=con_id, source="t")
    store.im_upsert_instrument(rec.as_record())
    if verified:
        with store.tx() as cur:
            store._exec(cur, "UPDATE instruments SET qualification_status='VERIFIED' WHERE instrument_id=?",
                        (rec.instrument_id,))
    return rec.instrument_id


def _rt_quote(pid="P", **kw):
    base = dict(provider_instrument_id=pid, bid=Decimal("100.00"), ask=Decimal("100.10"),
                last=Decimal("100.05"), data_currency="USD", source_ts=T0, receive_ts=T0,
                declared_status=DataStatus.REALTIME)
    base.update(kw)
    return ProviderQuote(**base)


def _quote_record(iid, provider, *, source_ts, bid, checksum):
    return {"instrument_id": iid, "provider": provider, "provider_instrument_id": "P", "bid": Decimal(bid),
            "ask": None, "last": None, "mid": None, "spread": None, "bid_size": None, "ask_size": None,
            "volume": None, "reference_price": None, "previous_close": None, "data_currency": "USD",
            "source_ts": source_ts, "receive_ts": source_ts, "latency_ms": None, "data_status": "DELAYED",
            "entitlement_status": "DELAYED_ONLY", "license": "FREE_OFFICIAL", "quality_status": "OK",
            "adjustment_policy": "RAW", "corporate_action_version": 0, "provenance_checksum": checksum}


def _noop(_):
    return None


# --------------------------------------------------------------------- migration
def test_migration_28_applied_and_health_untouched():
    store = _store()
    versions = {r[0] for r in store._all("SELECT version FROM schema_migrations")}
    assert {27, 28} <= versions and max(versions) == 28
    for tbl in ("md_quotes_current", "md_quote_history", "md_bars", "md_corporate_actions",
                "md_provider_entitlements", "md_import_runs", "md_import_events"):
        assert store._all(f"SELECT COUNT(*) FROM {tbl}")[0][0] == 0
    # WP-earlier live tables remain exactly as they were (Paper-Canary fill-safety preserved)
    store._one("SELECT symbol,source,status,latency_ms,updated_at,quote_ts FROM market_data_health LIMIT 0")
    store._one("SELECT symbol,interval,ts,open FROM ohlc_bars LIMIT 0")


# --------------------------------------------------------------------- fail-closed gating
def test_only_verified_ingested_and_free_realtime_downgraded():
    store = _store()
    v = _verified(store, "MSFT")
    u = _verified(store, "PENNY", verified=False)
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote(), u: _rt_quote()},
                                  mappings={v: "P", u: "P"}, license=LicenseType.FREE_OFFICIAL,
                                  realtime_entitled=False)
    s = ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    assert s.status == "COMPLETED" and s.processed == 1 and s.quotes_written == 1
    cur = store.md_get_current_quote(v, "FREE")
    assert cur.data_status == "DELAYED"                       # free/unentitled realtime claim downgraded
    assert store.md_get_current_quote(u, "FREE") is None      # unverified never ingested (fail-closed)


def test_entitled_verified_realtime_is_realtime():
    store = _store()
    v = _verified(store, "AAPL")
    prov = StubMarketDataProvider(name="IBKR", quotes={v: _rt_quote()}, mappings={v: "265598"},
                                  license=LicenseType.BROKER_ENTITLED, realtime_entitled=True)
    ingest_market_data(store, prov, run_label="rt", now=NOW, sleep=_noop)
    cur = store.md_get_current_quote(v, "IBKR")
    assert cur.data_status == "REALTIME" and cur.entitlement_status == "ENTITLED"
    assert cur.mid == Decimal("100.05") and cur.spread == Decimal("0.10")


def test_unmapped_instrument_is_skipped_not_fabricated():
    store = _store()
    v = _verified(store, "NOPE")
    prov = StubMarketDataProvider(name="FREE", quotes={}, mappings={})   # cannot map → no data
    s = ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    assert s.skipped == 1 and store.md_get_current_quote(v, "FREE") is None


# --------------------------------------------------------------------- monotonic / duplicate / history
def test_current_quote_is_monotonic_out_of_order_safe():
    store = _store()
    v = _verified(store, "MSFT")
    assert store.md_record_quote(_quote_record(v, "FREE", source_ts=T0, bid="100", checksum="sha256:a"))["current"] == "inserted"
    # a NEWER packet updates
    newer = "2026-09-02T13:31:00+00:00"
    assert store.md_record_quote(_quote_record(v, "FREE", source_ts=newer, bid="101", checksum="sha256:b"))["current"] == "updated"
    # a LATE / backward packet (a distinct, older-than-current source_ts) is ignored for CURRENT but archived
    late_ts = "2026-09-02T13:30:30+00:00"
    late = store.md_record_quote(_quote_record(v, "FREE", source_ts=late_ts, bid="1", checksum="sha256:c"))
    assert late["current"] == "stale-ignored" and late["history_appended"] is True
    assert store.md_get_current_quote(v, "FREE").bid == Decimal("101")     # newer value preserved
    assert len(store.md_list_quote_history(v, "FREE")) == 3                # all three archived


def test_duplicate_source_ts_not_appended():
    store = _store()
    v = _verified(store, "MSFT")
    store.md_record_quote(_quote_record(v, "FREE", source_ts=T0, bid="100", checksum="sha256:a"))
    dup = store.md_record_quote(_quote_record(v, "FREE", source_ts=T0, bid="100", checksum="sha256:a"))
    assert dup["history_appended"] is False                                # duplicate detection
    assert len(store.md_list_quote_history(v, "FREE")) == 1


# --------------------------------------------------------------------- observability + counters
def test_run_counters_and_events_observable():
    store = _store()
    ok = _verified(store, "MSFT")
    nodata = _verified(store, "GHOST")
    prov = StubMarketDataProvider(name="FREE", quotes={ok: _rt_quote()},
                                  mappings={ok: "P", nodata: "P"})   # GHOST maps but has no quote → NO_DATA
    s = ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    assert s.processed == 2 and s.quotes_written == 1 and s.no_data == 1
    run = store.md_get_run(s.run_id)
    assert run.status == "COMPLETED" and run.started_at and run.ended_at
    types = {e.event_type for e in store.md_list_run_events(s.run_id)}
    assert {"QUOTE_OK", "NO_DATA", "MARKET_OK"} <= types


def test_per_instrument_error_isolation():
    store = _store()
    ok1 = _verified(store, "AAA")
    bad = _verified(store, "BBB")
    ok2 = _verified(store, "CCC")
    prov = StubMarketDataProvider(name="FREE", quotes={ok1: _rt_quote(), ok2: _rt_quote()},
                                  mappings={ok1: "P", ok2: "P", bad: "P"}, unavailable={bad})
    s = ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    assert s.status == "COMPLETED" and s.quotes_written == 2 and s.error == 1
    assert store.md_get_current_quote(ok1, "FREE") is not None
    assert store.md_get_current_quote(ok2, "FREE") is not None
    assert store.md_get_current_quote(bad, "FREE") is None


class _MarketFailStore:
    """Delegating proxy that makes a specific market's instruments raise an INFRA error escaping _process_one."""

    def __init__(self, store, bad_ids):
        self._s = store
        self._bad = set(bad_ids)

    def __getattr__(self, name):
        return getattr(self._s, name)

    def md_bump_counter(self, run_id, counter=None, *, event=None):
        if event and event.get("instrument_id") in self._bad:
            raise RuntimeError("market infrastructure down")
        return self._s.md_bump_counter(run_id, counter, event=event)


def test_per_market_error_isolation_gives_partial():
    store = _store()
    good = _verified(store, "GOOD", exchange="NASDAQ")
    bad = _verified(store, "BADM", exchange="BADMKT")
    proxy = _MarketFailStore(store, {bad})
    # the bad instrument has NO quote → it goes through the no_data path whose md_bump_counter the proxy
    # fails, so the error escapes _process_one and fails the whole (BADMKT) market.
    prov = StubMarketDataProvider(name="FREE", quotes={good: _rt_quote()},
                                  mappings={good: "P", bad: "P"})
    s = ingest_market_data(proxy, prov, run_label="d", now=NOW, sleep=_noop)
    assert s.status == "PARTIAL"
    assert s.completed_markets == ["NASDAQ"] and s.failed_markets == ["BADMKT"]
    assert store.md_get_current_quote(good, "FREE") is not None
    # counter invariant: a written quote is always counted as processed (never quotes_written > processed)
    run = store.md_get_run(s.run_id)
    assert run.quotes_written_count <= run.processed_count
    assert store.md_get_current_quote(bad, "FREE") is None    # failed market left no phantom quote


# --------------------------------------------------------------------- adversarial-review regressions
def test_freshness_judged_at_fetch_time_not_run_start():
    """Regression (fail-closed-realtime): freshness is judged against the per-instrument clock reading (real
    fetch time), so a quote that is fresh vs run-start but STALE at write time is not mislabeled REALTIME."""
    store = _store()
    v = _verified(store, "AAPL")
    prov = StubMarketDataProvider(name="IBKR", quotes={v: _rt_quote(source_ts=T0)}, mappings={v: "265598"},
                                  license=LicenseType.BROKER_ENTITLED, realtime_entitled=True)
    # the clock (real time at fetch) is 60s after the quote's source_ts → stale beyond max_age_s=30
    ingest_market_data(store, prov, run_label="rt", clock=lambda: "2026-09-02T13:31:00+00:00",
                       config=IngestConfig(max_age_s=30.0), sleep=_noop)
    assert store.md_get_current_quote(v, "IBKR").data_status == "STALE"   # not REALTIME
    # control: a fresh clock reading yields REALTIME
    v2 = _verified(store, "MSFT")
    prov2 = StubMarketDataProvider(name="IBKR", quotes={v2: _rt_quote(source_ts=T0)}, mappings={v2: "P"},
                                   license=LicenseType.BROKER_ENTITLED, realtime_entitled=True)
    ingest_market_data(store, prov2, run_label="rt2", clock=lambda: "2026-09-02T13:30:05+00:00",
                       config=IngestConfig(max_age_s=30.0), sleep=_noop)
    assert store.md_get_current_quote(v2, "IBKR").data_status == "REALTIME"


def test_data_currency_never_fabricated_from_catalogue():
    """Regression (no-fabrication): when the provider does not report a currency it is stored NULL, never
    the catalogue trading currency (which would masquerade as provider-attested provenance)."""
    store = _store()
    v = _verified(store, "MSFT")   # catalogue trading_currency='USD'
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote(data_currency=None)}, mappings={v: "P"})
    ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    assert store.md_get_current_quote(v, "FREE").data_currency is None    # NOT 'USD'
    # control: a provider-reported currency is stored verbatim
    v2 = _verified(store, "VOD", exchange="XLON")
    prov2 = StubMarketDataProvider(name="FREE", quotes={v2: _rt_quote(data_currency="GBX")}, mappings={v2: "P"})
    ingest_market_data(store, prov2, run_label="d2", now=NOW, sleep=_noop)
    assert store.md_get_current_quote(v2, "FREE").data_currency == "GBX"


def test_negative_book_yields_null_mid_spread_and_invalid_quality():
    """Regression (no-fabrication): a structurally invalid (negative) book stores NULL mid/spread and
    quality INVALID — never a plausible-looking fabricated derived value."""
    store = _store()
    v = _verified(store, "MSFT")
    q = ProviderQuote(provider_instrument_id="P", bid=Decimal("-2"), ask=Decimal("10"), last=Decimal("5"),
                      data_currency="USD", source_ts=T0, receive_ts=T0, declared_status=DataStatus.DELAYED)
    prov = StubMarketDataProvider(name="FREE", quotes={v: q}, mappings={v: "P"})
    ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    cur = store.md_get_current_quote(v, "FREE")
    assert cur is not None and cur.mid is None and cur.spread is None and cur.quality_status == "INVALID"


def test_structurally_invalid_bar_is_not_labeled_ok():
    """Regression (no-fabrication): a bar with high<low / negative volume / missing close is not stored OK."""
    store = _store()
    v = _verified(store, "MSFT")
    bad = ProviderBar(interval="1d", ts="2026-09-01T00:00:00+00:00", open=Decimal("99"), high=Decimal("50"),
                      low=Decimal("200"), close=None, volume=Decimal("-5"),
                      declared_status=DataStatus.END_OF_DAY)
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote()}, mappings={v: "P"}, bars={v: [bad]})
    ingest_market_data(store, prov, run_label="d", config=IngestConfig(fetch_bars=True), now=NOW, sleep=_noop)
    bars = store.md_list_bars(v, "FREE")
    assert len(bars) == 1 and bars[0].quality_status == "INVALID"


def test_monotonic_guard_canonicalizes_source_ts_in_store():
    """Regression (ordering): the store canonicalizes source_ts, so a non-canonical-offset OLDER packet
    cannot lexicographically beat the newer canonical CURRENT quote."""
    store = _store()
    v = _verified(store, "MSFT")
    store.md_record_quote(_quote_record(v, "FREE", source_ts="2026-09-02T13:30:00+00:00", bid="100", checksum="sha256:a"))
    # 15:29+02:00 == 13:29 UTC → OLDER than the 13:30 UTC current, though its raw string sorts greater
    res = store.md_record_quote(_quote_record(v, "FREE", source_ts="2026-09-02T15:29:00+02:00", bid="1", checksum="sha256:b"))
    assert res["current"] == "stale-ignored"
    assert store.md_get_current_quote(v, "FREE").bid == Decimal("100")


# --------------------------------------------------------------------- entitlement / bars / corp actions
def test_provider_entitlement_stored_explicitly():
    store = _store()
    v = _verified(store, "MSFT")
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote()}, mappings={v: "P"},
                                  license=LicenseType.FREE_OFFICIAL, realtime_entitled=False)
    ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    ent = store.md_get_provider_entitlement(v, "FREE")
    assert ent.license == "FREE_OFFICIAL" and ent.realtime_available is False and ent.available is True


def test_bars_and_corporate_actions_are_immutable_and_appended():
    store = _store()
    v = _verified(store, "MSFT")
    bar = ProviderBar(interval="1d", ts="2026-09-01T00:00:00+00:00", open=Decimal("99"), high=Decimal("101"),
                      low=Decimal("98"), close=Decimal("100"), volume=Decimal("1000"), trade_count=42,
                      data_currency="USD", declared_status=DataStatus.END_OF_DAY)
    ca = ProviderCorporateAction(action_type="SPLIT", effective_date="2026-06-01", corporate_action_version=1,
                                 ratio=Decimal("2"))
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote()}, mappings={v: "P"},
                                  bars={v: [bar]}, corporate_actions={v: [ca]})
    s = ingest_market_data(store, prov, run_label="d",
                           config=IngestConfig(fetch_bars=True, fetch_corporate_actions=True),
                           now=NOW, sleep=_noop)
    assert s.bars_written == 1
    bars = store.md_list_bars(v, "FREE")
    assert len(bars) == 1 and bars[0].trade_count == 42 and bars[0].data_status == "END_OF_DAY"
    cas = store.md_list_corporate_actions(v)
    assert len(cas) == 1 and cas[0].action_type == "SPLIT" and cas[0].ratio == Decimal("2")
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE md_bars SET close='0' WHERE instrument_id=?", (v,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "DELETE FROM md_corporate_actions WHERE instrument_id=?", (v,))


# --------------------------------------------------------------------- immutability + reclaim
def test_history_and_terminal_run_immutable():
    store = _store()
    v = _verified(store, "MSFT")
    prov = StubMarketDataProvider(name="FREE", quotes={v: _rt_quote()}, mappings={v: "P"})
    s = ingest_market_data(store, prov, run_label="d", now=NOW, sleep=_noop)
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE md_quote_history SET bid='0' WHERE instrument_id=?", (v,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "UPDATE md_import_runs SET run_label='x' WHERE run_id=?", (s.run_id,))
    with pytest.raises(Exception):  # noqa: B017
        with store.tx() as cur:
            store._exec(cur, "DELETE FROM md_import_events WHERE run_id=?", (s.run_id,))


def test_unconfigured_provider_fails_closed():
    store = _store()
    _verified(store, "MSFT")

    class Unconfigured(StubMarketDataProvider):
        @property
        def configured(self):
            return False

    s = ingest_market_data(store, Unconfigured(name="NOKEY"), run_label="d", now=NOW, sleep=_noop)
    assert s.status == "FAILED" and store.md_get_run(s.run_id).failure_code == "PROVIDER_NOT_CONFIGURED"


def test_reclaim_stale_running_market_data_run():
    store = _store()
    store.md_create_run(run_id="stale", request_checksum="sha256:z", run_label="l", provider="FREE",
                        kind="quote")
    store.md_advance_run_status("stale", "PLANNED", "RUNNING")
    with store.tx() as cur:
        store._exec(cur, "UPDATE md_import_runs SET updated_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", "stale"))
    reclaimed = store.md_reclaim_stale_running("2020-01-01T00:00:00+00:00",
                                               failure_code="STALE", failure_reason="crashed")
    assert reclaimed == ["stale"] and store.md_get_run("stale").status == "FAILED"
    assert any(e.event_type == "RECLAIM" for e in store.md_list_run_events("stale"))
