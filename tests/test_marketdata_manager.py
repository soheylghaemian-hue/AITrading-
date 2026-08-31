"""Phase 10 — provider-independent market-data layer: NormalizedQuote, quality gate, manager."""

from datetime import datetime, timedelta, timezone

from atp.marketdata import (
    GLOBAL_UNIVERSE,
    MarketDataManager,
    NormalizedQuote,
    QualityStatus,
    quality_gate,
)
from atp.marketdata.universe import InstrumentSpec

NOW = datetime(2026, 8, 13, 15, 0, 0, tzinfo=timezone.utc)


def _q(**kw):
    base = dict(
        symbol="AAPL", con_id=1, asset_class="EQUITY", currency="USD",
        exchange="SMART", primary_exchange="NASDAQ", source="NASDAQ",
        bid=100.0, ask=100.1, last=100.05, bid_size=10, ask_size=12, volume=1000,
        timestamp=NOW, market_data_type="REALTIME",
    )
    base.update(kw)
    return NormalizedQuote(**base)


# ---------------------------------------------------------------- READY (the only pass)
def test_ready_is_the_only_tradeable_state():
    status, _ = quality_gate(_q(), now=NOW)
    assert status is QualityStatus.READY


# ---------------------------------------------------------------- rejections
def test_subscription_required_from_10089():
    status, reason = quality_gate(_q(bid=None, ask=None, last=None, error_code=10089), now=NOW)
    assert status is QualityStatus.SUBSCRIPTION_REQUIRED
    assert "subscription" in reason.lower()


def test_subscription_required_from_message():
    status, _ = quality_gate(_q(error_message="Requested market data requires a subscription"), now=NOW)
    assert status is QualityStatus.SUBSCRIPTION_REQUIRED


def test_competing_session_blocked_10197():
    status, _ = quality_gate(_q(error_code=10197), now=NOW)
    assert status is QualityStatus.BLOCKED


def test_delayed_rejected():
    status, _ = quality_gate(_q(market_data_type="DELAYED"), now=NOW)
    assert status is QualityStatus.DELAYED


def test_frozen_rejected():
    status, _ = quality_gate(_q(market_data_type="FROZEN"), now=NOW)
    assert status is QualityStatus.INVALID


def test_negative_sentinel_rejected():
    status, _ = quality_gate(_q(bid=-1.0), now=NOW)
    assert status is QualityStatus.INVALID


def test_missing_bid_rejected():
    status, _ = quality_gate(_q(bid=None), now=NOW)
    assert status is QualityStatus.INVALID


def test_missing_ask_rejected():
    status, _ = quality_gate(_q(ask=None), now=NOW)
    assert status is QualityStatus.INVALID


def test_crossed_quote_rejected():
    status, _ = quality_gate(_q(bid=101.0, ask=100.0), now=NOW)
    assert status is QualityStatus.INVALID


def test_stale_timestamp_rejected():
    old = NOW - timedelta(seconds=120)
    status, _ = quality_gate(_q(timestamp=old), now=NOW, max_age_s=30)
    assert status is QualityStatus.STALE


def test_missing_or_future_quote_timestamp_rejected():
    missing, _ = quality_gate(_q(timestamp=None), now=NOW)
    future, _ = quality_gate(_q(timestamp=NOW + timedelta(seconds=1)), now=NOW)
    assert missing is QualityStatus.INVALID
    assert future is QualityStatus.INVALID


def test_no_data_at_all():
    status, _ = quality_gate(_q(bid=None, ask=None, last=None), now=NOW)
    assert status is QualityStatus.DATA_NOT_AVAILABLE


def test_zero_prices_not_tradeable():
    status, _ = quality_gate(_q(bid=0.0, ask=0.0, last=0.0), now=NOW)
    assert status is not QualityStatus.READY


def test_not_realtime_rejected():
    status, _ = quality_gate(_q(market_data_type="UNKNOWN"), now=NOW)
    assert status is QualityStatus.INVALID


# ---------------------------------------------------------------- manager
def test_manager_normalizes_sentinels_to_none():
    mgr = MarketDataManager([InstrumentSpec("AAPL", "USA", "SMART", "NASDAQ", "USD")])
    quotes = mgr.classify({"AAPL": {"bid": -1, "ask": float("nan"), "last": None}}, now=NOW)
    assert quotes[0].bid is None and quotes[0].ask is None


def test_manager_ready_filters_only_ready():
    mgr = MarketDataManager([
        InstrumentSpec("AAPL", "USA", "SMART", "NASDAQ", "USD", label="NASDAQ"),
        InstrumentSpec("NVDA", "USA", "SMART", "NASDAQ", "USD", label="NASDAQ"),
    ])
    raw = {
        "AAPL": {"bid": 100, "ask": 100.1, "last": 100.05, "market_data_type": "REALTIME", "source": "NASDAQ", "timestamp": NOW},
        "NVDA": {"error_code": 10089},
    }
    quotes = mgr.classify(raw, now=NOW)
    ready = mgr.ready(quotes)
    assert [q.symbol for q in ready] == ["AAPL"]


def test_manager_dashboard_rows_shape():
    mgr = MarketDataManager([InstrumentSpec("SAP", "Germany", "IBIS", "XETRA", "EUR", label="XETRA")])
    quotes = mgr.classify({"SAP": {"error_code": 10089}}, now=NOW)
    row = mgr.dashboard_rows(quotes)[0]
    assert row["region"] == "Germany"
    assert row["status"] == QualityStatus.SUBSCRIPTION_REQUIRED.value
    assert row["subscription_state"] == "REQUIRED"
    for key in ("region", "exchange", "symbol", "source", "status", "realtime",
                "bid", "ask", "last", "spread", "bid_size", "ask_size", "volume",
                "timestamp", "error", "subscription_state"):
        assert key in row


def test_manager_missing_symbol_is_data_not_available():
    mgr = MarketDataManager([InstrumentSpec("AAPL", "USA", "SMART", "NASDAQ", "USD")])
    quotes = mgr.classify({}, now=NOW)  # no raw provided
    assert quotes[0].status == QualityStatus.DATA_NOT_AVAILABLE.value


# ---------------------------------------------------------------- universe
def test_global_universe_multi_region():
    regions = {s.region for s in GLOBAL_UNIVERSE}
    for r in ("USA", "Germany", "UK", "Switzerland", "France", "Japan", "FX"):
        assert r in regions
    # not limited to the US mega-caps
    assert len(GLOBAL_UNIVERSE) > 20


def test_normalized_quote_as_dict_serializable():
    d = _q().as_dict()
    assert d["timestamp"] == NOW.isoformat()
    assert d["spread"] is not None
