"""§ WP4 unit — the unified market-data model + fail-closed classifiers (pure, no store, no network).

Proves the never-fabricate / never-realtime-without-entitlement contract of `atp.marketdata.model` and the
deterministic stub provider. SAFETY: read-only data only — no trading path.
"""
from __future__ import annotations

from decimal import Decimal

from atp.marketdata.model import (
    DataStatus,
    LicenseType,
    QualityFlag,
    QuoteObservation,
    classify_data_status,
    classify_quality,
    latency_ms,
    mid_price,
    spread,
    utc_ts,
)
from atp.marketdata.provider_base import (
    InstrumentRef,
    MarketDataEntitlementError,
    MarketDataUnavailableError,
    ProviderQuote,
    StubMarketDataProvider,
)

T0 = "2026-09-02T13:30:00+00:00"
T1 = "2026-09-02T13:30:01+00:00"
T_LATE = "2026-09-02T14:30:00+00:00"


def _cds(**kw):
    base = dict(declared=DataStatus.REALTIME, entitled=True, verified=True, has_price=True,
                source_ts=T0, now=T1, max_age_s=30.0)
    base.update(kw)
    return classify_data_status(**base)


# --------------------------------------------------------------------- fail-closed data status
def test_realtime_requires_entitlement_and_verified():
    assert _cds() is DataStatus.REALTIME
    assert _cds(entitled=False) is DataStatus.DELAYED          # not entitled → never realtime
    assert _cds(verified=False) is DataStatus.DELAYED          # not verified → never realtime
    assert _cds(entitled=False, verified=False) is DataStatus.DELAYED


def test_no_price_or_no_timestamp_is_no_data():
    assert _cds(has_price=False) is DataStatus.NO_DATA
    assert _cds(source_ts=None) is DataStatus.NO_DATA
    assert _cds(source_ts="not-a-timestamp") is DataStatus.NO_DATA
    assert _cds(source_ts="2026-09-02T13:30:00") is DataStatus.NO_DATA   # naive → not evidence


def test_future_dated_is_no_data_never_realtime():
    assert _cds(source_ts=T1, now=T0) is DataStatus.NO_DATA


def test_staleness():
    assert _cds(now=T_LATE) is DataStatus.STALE
    assert classify_data_status(declared=DataStatus.DELAYED, entitled=True, verified=True, has_price=True,
                                source_ts=T0, now=T_LATE) is DataStatus.STALE


def test_end_of_day_and_delayed_pass_through():
    assert classify_data_status(declared=DataStatus.END_OF_DAY, entitled=False, verified=False,
                                has_price=True, source_ts=T0, now=T_LATE) is DataStatus.END_OF_DAY
    assert classify_data_status(declared=DataStatus.DELAYED, entitled=False, verified=True, has_price=True,
                                source_ts=T0, now=T1) is DataStatus.DELAYED


# --------------------------------------------------------------------- quality / derived
def test_classify_quality():
    assert classify_quality(Decimal("10"), Decimal("10.1"), Decimal("10.05")) is QualityFlag.OK
    assert classify_quality(Decimal("10.1"), Decimal("10"), None) is QualityFlag.INVALID   # crossed
    assert classify_quality(Decimal("-1"), None, None) is QualityFlag.INVALID              # negative
    assert classify_quality(Decimal("10"), None, None) is QualityFlag.DEGRADED             # one-sided
    assert classify_quality(None, None, None) is QualityFlag.NO_DATA


def test_mid_spread_and_crossed():
    assert mid_price(Decimal("10"), Decimal("10.2")) == Decimal("10.1")
    assert spread(Decimal("10"), Decimal("10.2")) == Decimal("0.2")
    assert mid_price(Decimal("10.2"), Decimal("10")) is None    # crossed → None, not a fabricated mid
    assert spread(Decimal("10"), None) is None


def test_mid_spread_reject_nonpositive_prices():
    # Regression: a negative or zero price is not a valid book — mid/spread are NULL, never fabricated.
    assert mid_price(Decimal("-2"), Decimal("10")) is None
    assert spread(Decimal("-2"), Decimal("10")) is None
    assert mid_price(Decimal("0"), Decimal("10")) is None
    assert spread(Decimal("10"), Decimal("0")) is None


def test_classify_bar_quality():
    from atp.marketdata.model import classify_bar_quality
    assert classify_bar_quality(99, 101, 98, 100, 1000) is QualityFlag.OK
    assert classify_bar_quality(99, 50, 200, None, -5) is QualityFlag.INVALID   # high<low AND negative volume
    assert classify_bar_quality(100, 90, 95, 92) is QualityFlag.INVALID         # high < open
    assert classify_bar_quality(99, 101, 98, None) is QualityFlag.DEGRADED       # missing close
    assert classify_bar_quality(None, None, None, None) is QualityFlag.NO_DATA


def test_utc_ts_canonicalization():
    assert utc_ts("2026-09-02T13:30:00Z") == "2026-09-02T13:30:00+00:00"
    assert utc_ts("2026-09-02T08:30:00-05:00") == "2026-09-02T13:30:00+00:00"
    assert utc_ts("2026-09-02T13:30:00") is None                # naive rejected
    assert utc_ts(None) is None


def test_latency_only_when_forward():
    assert latency_ms(T0, "2026-09-02T13:30:00.250000+00:00") == Decimal("250.000")
    assert latency_ms(T0, "2026-09-02T13:29:59+00:00") is None  # backward → not a valid latency
    assert latency_ms(T0, None) is None


# --------------------------------------------------------------------- observation checksum + derived
def test_quote_observation_checksum_and_derived():
    q = QuoteObservation(instrument_id="INS-1", provider="FREE", provider_instrument_id="X",
                         bid=Decimal("100.00"), ask=Decimal("100.10"), source_ts=T0,
                         receive_ts="2026-09-02T13:30:00.100000+00:00")
    assert q.mid == Decimal("100.05")
    assert q.spread == Decimal("0.10")
    assert q.latency_ms == Decimal("100.000")
    same = QuoteObservation(instrument_id="INS-1", provider="FREE", provider_instrument_id="X",
                            bid=Decimal("100.00"), ask=Decimal("100.10"), source_ts=T0,
                            receive_ts="2026-09-02T13:30:00.999999+00:00")   # receive_ts not in checksum
    assert q.checksum == same.checksum
    changed = QuoteObservation(instrument_id="INS-1", provider="FREE", provider_instrument_id="X",
                               bid=Decimal("100.01"), ask=Decimal("100.10"), source_ts=T0)
    assert q.checksum != changed.checksum and q.checksum.startswith("sha256:")


# --------------------------------------------------------------------- stub provider
def test_stub_provider_is_deterministic_and_honest():
    ref = InstrumentRef(instrument_id="INS-1", symbol="MSFT", exchange="NASDAQ", currency="USD",
                        asset_class="equity", verified=True)
    prov = StubMarketDataProvider(
        name="FREE", quotes={"INS-1": ProviderQuote(provider_instrument_id="MSFT.P", bid=Decimal("1"))},
        mappings={"INS-1": "MSFT.P"}, license=LicenseType.FREE_OFFICIAL, realtime_entitled=False)
    assert prov.configured is True
    assert prov.map_instrument(ref).provider_instrument_id == "MSFT.P"
    ent = prov.probe_entitlement(ref)
    assert ent.license is LicenseType.FREE_OFFICIAL and ent.realtime_available is False
    assert prov.get_quote(ref).bid == Decimal("1")


def test_stub_provider_error_paths():
    ref = InstrumentRef(instrument_id="INS-2", symbol="X", exchange="NASDAQ", currency="USD",
                        asset_class="equity", verified=True)
    down = StubMarketDataProvider(name="FREE", unavailable={"INS-2"})
    try:
        down.get_quote(ref)
        raise AssertionError("expected MarketDataUnavailableError")
    except MarketDataUnavailableError:
        pass
    blocked = StubMarketDataProvider(name="FREE", not_entitled={"INS-2"})
    try:
        blocked.get_quote(ref)
        raise AssertionError("expected MarketDataEntitlementError")
    except MarketDataEntitlementError:
        pass


def test_provider_interface_has_no_order_or_execution_methods():
    # the read-only interface must not expose any trading surface
    from atp.marketdata.provider_base import MarketDataProvider
    forbidden = {"place_order", "submit_order", "cancel_order", "create_order", "buy", "sell",
                 "positions", "account", "withdraw", "deposit", "subscribe", "reqMktData"}
    assert forbidden.isdisjoint(set(dir(MarketDataProvider)))
