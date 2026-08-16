"""Phase G2.3 — Options Intelligence Layer (read-only intelligence input).

Covers: provider interface + Polygon parser, persistence, deterministic analytics (aggregate + score +
signals/risks), missing-data → NO DATA, no-execution side effects, restart durability, no-secrets.
Touches no Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atp.optflow.analytics import (
    aggregate_chain, options_score, sentiment_from_pcr, signals_and_risks, unusual_activity_label,
)
from atp.optflow.collector import OptionsCollector
from atp.optflow.provider import (
    NullOptionsProvider, OptionContract, OptionsProvider, PolygonOptionsProvider, parse_polygon_options,
    resolve_provider,
)
from atp.optflow.readmodel import build_options
from atp.store import open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates options tables (migration 7)


CHAIN = [
    OptionContract("NVDA", "2026-09-18", 210, "call", bid=5.0, ask=5.2, last=5.1, volume=20000, open_interest=15000, implied_volatility=0.42),
    OptionContract("NVDA", "2026-09-18", 220, "call", bid=3.0, ask=3.2, last=3.1, volume=15000, open_interest=12000, implied_volatility=0.45),
    OptionContract("NVDA", "2026-09-18", 200, "put", bid=4.0, ask=4.2, last=4.1, volume=8000, open_interest=10000, implied_volatility=0.40),
    OptionContract("NVDA", "2026-09-18", 190, "put", bid=2.0, ask=2.2, last=2.1, volume=5000, open_interest=9000, implied_volatility=0.38),
]

POLY_SNAPSHOT = {"results": [
    {"details": {"contract_type": "call", "strike_price": 210, "expiration_date": "2026-09-18"},
     "day": {"volume": 20000, "close": 5.1}, "open_interest": 15000, "implied_volatility": 0.42,
     "last_quote": {"bid": 5.0, "ask": 5.2}, "last_trade": {"price": 5.1}},
    {"details": {"contract_type": "put", "strike_price": 200, "expiration_date": "2026-09-18"},
     "day": {"volume": 8000, "close": 4.1}, "open_interest": 10000, "implied_volatility": 0.40,
     "last_quote": {"bid": 4.0, "ask": 4.2}, "last_trade": {"price": 4.1}},
    {"details": {"contract_type": "other"}},   # dropped
]}


class FakeProvider(OptionsProvider):
    name = "fake"

    def __init__(self, chain): self._chain = chain
    @property
    def configured(self): return True
    def get_option_chain(self, symbol): return list(self._chain)
    def get_unusual_activity(self, symbol): return {"contracts": len(self._chain)}


def test_provider_interface_default_and_null():
    assert isinstance(resolve_provider(), PolygonOptionsProvider)        # default = real Polygon/Massive
    n = NullOptionsProvider()
    assert n.get_option_chain("X") == [] and n.get_unusual_activity("X") is None
    assert PolygonOptionsProvider(api_key="").configured is False        # no key → NO DATA


def test_polygon_parser():
    cs = parse_polygon_options(POLY_SNAPSHOT, "NVDA")
    assert len(cs) == 2                                                  # non-call/put dropped
    assert cs[0].option_type == "call" and cs[0].strike == 210 and cs[0].volume == 20000
    assert cs[0].implied_volatility == 0.42 and cs[0].open_interest == 15000
    assert parse_polygon_options({}, "NVDA") == []


def test_analytics_aggregate_score_and_signals():
    flow = aggregate_chain(CHAIN, "NVDA")
    assert flow.call_volume == 35000 and flow.put_volume == 13000
    assert flow.call_put_ratio == pytest.approx(13000 / 35000, rel=1e-3)  # PCR < 0.7
    assert flow.sentiment == "Bullish"
    assert flow.large_trade_count == 4                                   # all four premiums > $1M
    assert flow.implied_volatility == pytest.approx(0.4219, abs=1e-3)

    score = options_score(flow)
    assert score is not None and 82 <= score <= 92                       # ~88 in the spec example

    signals, risks = signals_and_risks(flow)
    assert "High call activity" in signals and "Positive positioning" in signals
    assert "Large premium trades detected" in signals
    assert "Elevated implied volatility" in risks
    assert unusual_activity_label(flow) == "Detected"

    assert sentiment_from_pcr(1.5) == "Bearish" and sentiment_from_pcr(0.85) == "Neutral"
    assert aggregate_chain([], "NVDA") is None and options_score(None) is None   # empty → NO DATA


def test_persistence_and_readmodel(store):
    assert OptionsCollector(store, FakeProvider(CHAIN)).collect("NVDA") is True
    assert store.count_options_flow() == 1
    assert len(store.list_options_snapshots("NVDA")) == 4

    o = build_options(store, "NVDA")
    assert 82 <= o["options_score"] <= 92
    assert o["sentiment"] == "Bullish" and o["unusual_activity"] == "Detected"
    assert o["call_put_ratio"] == pytest.approx(13000 / 35000, rel=1e-3)
    assert o["volume"] == 48000 and o["open_interest"] == 46000
    assert "High call activity" in o["signals"] and "Elevated implied volatility" in o["risks"]

    OptionsCollector(store, FakeProvider(CHAIN)).collect("NVDA")          # idempotent
    assert store.count_options_flow() == 1


def test_missing_data_is_no_data(store):
    o = build_options(store, "NVDA")
    assert o["options_score"] is None and o["sentiment"] is None and o["unusual_activity"] is None
    assert o["signals"] == [] and o["risks"] == []
    assert OptionsCollector(store, NullOptionsProvider()).collect("NVDA") is False
    assert store.count_options_flow() == 0


def test_provider_never_puts_key_in_url(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"results": []}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr("atp.optflow.provider.urlopen", fake_urlopen)
    PolygonOptionsProvider(api_key="SECRETKEY123").get_option_chain("NVDA")
    assert "SECRETKEY123" not in captured["url"]
    assert captured["auth"] == "Bearer SECRETKEY123"


def test_no_execution_side_effects():
    pkg = Path(__file__).resolve().parents[2] / "src" / "atp" / "optflow"
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(")
    for f in pkg.glob("*.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    OptionsCollector(s1, FakeProvider(CHAIN)).collect("NVDA")
    s1.close()
    s2 = open_store(path)                                                 # "restart"
    o = build_options(s2, "NVDA")
    assert o["options_score"] is not None and o["sentiment"] == "Bullish"
