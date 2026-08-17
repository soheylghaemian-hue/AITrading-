"""Phase R1.2 — Macro Intelligence Layer (read-only intelligence input).

Covers: provider interface (Null + FRED parser), persistence + immutable snapshots, regime calculation,
RISK_ON / RISK_NEUTRAL / RISK_OFF classification, macro score, missing data → NO DATA, macro context,
data-completeness integration, no fabricated values, and no execution side effects. Touches no Trading
Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atp.completeness.engine import compute_completeness
from atp.macrodata.collector import MacroCollector
from atp.macrodata.provider import (
    FredMacroProvider, MacroMetrics, MacroProvider, NullMacroProvider, parse_fred_observation,
    parse_polygon_prev,
    resolve_provider,
)
from atp.macrodata.readmodel import build_macro, build_macro_context
from atp.macrodata.regime import classify_regime, macro_score, signals_and_risks
from atp.store import open_store

T0 = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 13 (macro_snapshots)


class FakeProvider(MacroProvider):
    name = "fake"

    def __init__(self, metrics): self._m = metrics
    @property
    def configured(self): return True
    def get_interest_rates(self): return self._m
    def get_inflation(self): return None
    def get_employment(self): return None
    def get_currency(self): return None
    def get_volatility(self): return None
    def get_market_regime_data(self): return None


RISK_ON_M = MacroMetrics(fed_rate=2.5, treasury_10y=3.2, treasury_2y=2.6, cpi=2.3, vix=13.0, dxy=100.0)
RISK_OFF_M = MacroMetrics(fed_rate=5.5, treasury_10y=4.8, treasury_2y=5.1, cpi=6.0, vix=34.0, dxy=108.0)


# ------------------------------------------------------------------ provider interface
def test_default_provider_is_fred_and_null_is_no_data():
    assert isinstance(resolve_provider(), FredMacroProvider)   # default = real FRED
    assert FredMacroProvider(api_key="").configured is False   # no key → NO DATA
    n = NullMacroProvider()
    assert n.configured is False
    assert n.snapshot().any_present() is False                 # never fabricates


def test_fred_observation_parser():
    payload = {"observations": [{"value": "4.25"}, {"value": "4.10"}]}
    assert parse_fred_observation(payload) == 4.25
    assert parse_fred_observation({"observations": [{"value": "."}, {"value": "3.0"}]}) == 3.0  # skips '.'
    assert parse_fred_observation({}) is None                   # empty → None (NO DATA)


def test_provider_snapshot_merges_domains():
    m = FakeProvider(MacroMetrics(fed_rate=4.5, treasury_10y=4.2)).snapshot()
    assert m.fed_rate == 4.5 and m.treasury_10y == 4.2
    assert m.vix is None                                        # unread domain stays None


# ------------------------------------------------------------------ regime + score
def test_regime_risk_on():
    assert classify_regime(RISK_ON_M) == "RISK_ON"
    assert macro_score(RISK_ON_M) >= 65


def test_regime_risk_off():
    assert classify_regime(RISK_OFF_M) == "RISK_OFF"
    assert macro_score(RISK_OFF_M) < 40


def test_regime_neutral_midrange():
    mid = MacroMetrics(treasury_10y=4.0, treasury_2y=3.9, cpi=3.2, vix=20.0, dxy=103.0)
    assert classify_regime(mid) == "RISK_NEUTRAL"


def test_falling_vix_and_improving_cpi_are_signals():
    prev = MacroMetrics(vix=22.0, cpi=3.5, treasury_10y=4.0)
    cur = MacroMetrics(vix=15.0, cpi=2.8, treasury_10y=4.0)
    signals, _ = signals_and_risks(cur, prev)
    assert "Volatility decreasing" in signals
    assert "Inflation improving" in signals


def test_rising_yields_and_inverted_curve_are_risks():
    prev = MacroMetrics(treasury_10y=4.0)
    cur = MacroMetrics(treasury_10y=4.6, treasury_2y=4.9, fed_rate=5.25, vix=28.0)
    _, risks = signals_and_risks(cur, prev)
    assert "Rising yields" in risks
    assert "Inverted yield curve" in risks
    assert "Rates elevated" in risks
    assert "Rising volatility" in risks


def test_score_none_when_no_metrics():
    assert macro_score(MacroMetrics()) is None
    assert classify_regime(MacroMetrics()) is None


# ------------------------------------------------------------------ persistence + read-model
def test_missing_data_is_no_data(store):
    m = build_macro(store)
    assert m["status"] == "NO DATA"
    assert m["score"] is None and m["regime"] is None
    assert m["metrics"] == {}


def test_collect_persist_and_build(store):
    assert MacroCollector(store, FakeProvider(RISK_ON_M)).collect(T0) is True
    m = build_macro(store)
    assert m["regime"] == "RISK_ON"
    assert m["score"] >= 65
    assert m["metrics"]["vix"]["value"] == 13.0
    assert m["source"] == "fake"


def test_core_cpi_persists_and_is_exposed(store):
    # core_cpi must survive persist → read-model (it had no column before this fix).
    MacroCollector(store, FakeProvider(MacroMetrics(cpi=3.3, core_cpi=2.5, vix=15.0, treasury_10y=4.0))).collect(T0)
    assert store.latest_macro_snapshot().core_cpi == 2.5
    m = build_macro(store)
    assert m["metrics"]["core_cpi"]["value"] == 2.5
    assert m["metrics"]["core_cpi"]["label"] == "Core CPI (YoY)"


def test_snapshot_is_immutable(store):
    MacroCollector(store, FakeProvider(RISK_ON_M)).collect(T0)
    assert store.count_macro_snapshots() == 1
    # same hour → no second row, no rewrite
    MacroCollector(store, FakeProvider(RISK_OFF_M)).collect(T0)
    assert store.count_macro_snapshots() == 1
    assert store.latest_macro_snapshot().vix == 13.0           # original preserved


def test_trend_uses_previous_snapshot(store):
    MacroCollector(store, FakeProvider(MacroMetrics(vix=22.0, treasury_10y=4.0, treasury_2y=3.8, cpi=3.5))).collect(T0)
    MacroCollector(store, FakeProvider(MacroMetrics(vix=15.0, treasury_10y=4.0, treasury_2y=3.8, cpi=2.8))).collect(T0 + timedelta(hours=1))
    m = build_macro(store)
    assert m["metrics"]["vix"]["trend"] == "down"
    assert "Volatility decreasing" in m["signals"]


def test_empty_provider_persists_nothing(store):
    assert MacroCollector(store, NullMacroProvider()).collect(T0) is False
    assert store.count_macro_snapshots() == 0                  # NO DATA, never fabricated


# ------------------------------------------------------------------ macro context + completeness
def test_macro_context_tailwind(store):
    MacroCollector(store, FakeProvider(RISK_ON_M)).collect(T0)
    ctx = build_macro_context(store, "NVDA")
    assert ctx["symbol"] == "NVDA"
    assert ctx["regime"] == "RISK_ON"
    assert ctx["relevance"] == "TAILWIND"


def test_macro_context_no_data(store):
    ctx = build_macro_context(store, "NVDA")
    assert ctx["status"] == "NO DATA"
    assert ctx["relevance"] is None


def test_completeness_reflects_macro(store):
    # Before any macro snapshot → macro missing.
    c0 = compute_completeness(store, "NVDA", T0)
    assert "macro" in c0["missing"]
    # After a real snapshot with rates + VIX → macro becomes available.
    MacroCollector(store, FakeProvider(RISK_ON_M)).collect(T0)
    c1 = compute_completeness(store, "NVDA", T0)
    assert "macro" in c1["available"]
    assert c1["details"]["macro"]["checks"]["macro_snapshot"] is True


# ------------------------------------------------------------------ security: no execution
def test_no_execution_side_effects(store):
    MacroCollector(store, FakeProvider(RISK_ON_M)).collect(T0)
    build_macro(store)
    build_macro_context(store, "NVDA")
    assert store.list_positions() == []
    assert store.list_fills() == []


def test_macro_source_has_no_broker_tokens():
    root = Path(__file__).resolve().parents[2] / "src" / "atp"
    files = list((root / "macrodata").glob("*.py")) + [root / "services" / "macro_intelligence.py"]
    forbidden = ("placeOrder", "cancelOrder", "submit_order", "ib_async", "reqMktData", "IB(", "ibapi")
    for f in files:
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"


# ------------------------------------------------------------------ FRED units + Polygon gold (fixes)
import json as _json  # noqa: E402


class _Resp:
    def __init__(self, payload): self._b = _json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_parse_polygon_prev():
    assert parse_polygon_prev({"results": [{"c": 2400.5}]}) == 2400.5
    assert parse_polygon_prev({"results": []}) is None
    assert parse_polygon_prev({}) is None                    # no data → None (never fabricated)


def test_cpi_requested_as_yoy_percent(monkeypatch):
    urls = []
    def fake(req, timeout=None):
        urls.append(req.full_url)
        return _Resp({"observations": [{"value": "3.1"}]})
    monkeypatch.setattr("atp.macrodata.provider.urlopen", fake)
    m = FredMacroProvider(api_key="K").get_inflation()
    assert m.cpi == 3.1                                       # a real YoY rate, not a ~318 index level
    assert any("CPIAUCSL" in u and "units=pc1" in u for u in urls)   # headline CPI requested as YoY %


def test_gold_from_polygon(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "PK")
    def fake(req, timeout=None):
        url = req.full_url
        if "C:XAUUSD/prev" in url:
            return _Resp({"results": [{"c": 2410.0}]})       # Polygon gold
        return _Resp({"observations": [{"value": "78.5"}]})  # FRED oil
    monkeypatch.setattr("atp.macrodata.provider.urlopen", fake)
    m = FredMacroProvider(api_key="K").get_market_regime_data()
    assert m.oil == 78.5 and m.gold == 2410.0                # oil (FRED) + gold (Polygon)


def test_series_latest_falls_back_past_unreleased_point(monkeypatch):
    # Newest observation is '.' (month not yet released) → fall back to the last real value, not None.
    urls = []
    def fake(req, timeout=None):
        urls.append(req.full_url)
        return _Resp({"observations": [{"value": "."}, {"value": "3.4"}, {"value": "3.5"}]})
    monkeypatch.setattr("atp.macrodata.provider.urlopen", fake)
    v = FredMacroProvider(api_key="K")._series_latest("CPILFESL", "pc1")
    assert v == 3.4                                          # most recent REAL value (skips the '.')
    assert "limit=1&" not in urls[0] and "limit=12" in urls[0]   # window >1 so a fallback exists


def test_gold_no_polygon_key_is_no_data(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.setattr("atp.macrodata.provider.urlopen",
                        lambda req, timeout=None: _Resp({"observations": [{"value": "78.5"}]}))
    m = FredMacroProvider(api_key="K").get_market_regime_data()
    assert m.oil == 78.5 and m.gold is None                  # no Polygon key → gold NO DATA (honest)
