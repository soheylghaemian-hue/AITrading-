"""Phase G2.2 — Fundamentals Intelligence Layer (read-only intelligence input).

Covers: provider interface + Polygon parsers, persistence, deterministic quality scoring +
strengths/risks, missing-data → NO DATA, API read-model shape, restart durability, and no-secrets.
Touches no Trading Core / Risk / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from atp.fundamentals.collector import FundamentalsCollector
from atp.fundamentals.provider import (
    AnalystEstimates, CompanyProfile, Earnings, FinancialMetrics, FundamentalsProvider,
    NullFundamentalsProvider, PolygonFundamentalsProvider, Valuation, parse_polygon_financials,
    parse_polygon_profile, resolve_provider,
)
from atp.fundamentals.quality import company_quality, strengths_and_risks
from atp.fundamentals.readmodel import build_fundamentals
from atp.store import open_store


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))          # migrates fundamentals tables (migration 6)


NVDA_FIN = FinancialMetrics("NVDA", period="FY", revenue=130e9, revenue_growth=0.35, gross_margin=0.60,
                            operating_margin=0.50, net_margin=0.44, eps=2.3, eps_growth=0.35)
NVDA_VAL = Valuation("NVDA", market_cap=3.0e12, pe_ratio=50.0, price_sales=23.0)
WEAK_FIN = FinancialMetrics("WEAK", period="FY", revenue=1e9, revenue_growth=-0.10, gross_margin=0.20,
                            operating_margin=-0.05, net_margin=-0.05)


class FakeProvider(FundamentalsProvider):
    name = "fake"

    @property
    def configured(self): return True
    def get_company_profile(self, s):
        return CompanyProfile(s.upper(), "NVIDIA Corporation", "SEMICONDUCTORS", "SEMICONDUCTORS", "XNAS", "US", 3.0e12)
    def get_financials(self, s): return NVDA_FIN
    def get_earnings(self, s): return Earnings(s.upper(), eps=2.3, period="FY")
    def get_valuation(self, s): return NVDA_VAL
    def get_estimates(self, s): return None               # analyst data not available → NO DATA


POLY_TICKERS = {"results": {
    "ticker": "NVDA", "name": "NVIDIA Corporation", "primary_exchange": "XNAS", "locale": "us",
    "sic_description": "SEMICONDUCTORS & RELATED DEVICES", "market_cap": 3.0e12}}
POLY_FIN = {"results": [
    {"fiscal_period": "FY", "financials": {"income_statement": {
        "revenues": {"value": 130e9}, "gross_profit": {"value": 78e9},
        "operating_income_loss": {"value": 65e9}, "net_income_loss": {"value": 57e9},
        "diluted_earnings_per_share": {"value": 2.3}}}},
    {"fiscal_period": "FY", "financials": {"income_statement": {
        "revenues": {"value": 96e9}, "diluted_earnings_per_share": {"value": 1.7}}}}]}


def test_provider_interface_default_and_null():
    assert isinstance(resolve_provider(), PolygonFundamentalsProvider)   # default = real Polygon/Massive
    n = NullFundamentalsProvider()
    assert n.get_company_profile("X") is None and n.get_financials("X") is None
    assert n.get_valuation("X") is None and n.get_estimates("X") is None and n.get_earnings("X") is None
    assert PolygonFundamentalsProvider(api_key="").configured is False   # no key → NO DATA


def test_polygon_parsers_extract_real_fields():
    prof = parse_polygon_profile(POLY_TICKERS, "NVDA")
    assert prof.company_name == "NVIDIA Corporation" and prof.exchange == "XNAS" and prof.country == "US"
    assert prof.market_cap == 3.0e12
    fin = parse_polygon_financials(POLY_FIN, "NVDA")
    assert fin.revenue == 130e9
    assert fin.revenue_growth == pytest.approx((130 - 96) / 96)
    assert fin.gross_margin == pytest.approx(78 / 130) and fin.net_margin == pytest.approx(57 / 130)
    assert fin.eps_growth == pytest.approx((2.3 - 1.7) / 1.7)
    assert parse_polygon_financials({}, "NVDA") is None                  # empty → NO DATA


def test_quality_score_and_strengths_risks():
    q = company_quality(NVDA_FIN, NVDA_VAL)
    assert q is not None and 80 <= q <= 92                                # strong company scores high
    strengths, risks = strengths_and_risks(NVDA_FIN, NVDA_VAL)
    assert "Revenue growth" in strengths and "High margins" in strengths
    assert "High valuation" in risks                                     # P/E 50 → risk flagged
    # a weak company scores far lower and surfaces the real risks
    qw = company_quality(WEAK_FIN, Valuation("WEAK"))
    assert qw is not None and qw < 50 and qw < q
    _, wrisks = strengths_and_risks(WEAK_FIN, Valuation("WEAK"))
    assert "Declining revenue" in wrisks and "Unprofitable" in wrisks
    assert company_quality(None, None) is None                           # no data → NO DATA


def test_persistence_and_readmodel(store):
    assert FundamentalsCollector(store, FakeProvider()).collect("NVDA") is True
    assert store.count_companies() == 1

    fm = build_fundamentals(store, "NVDA")
    assert fm["company"]["company_name"] == "NVIDIA Corporation"
    assert 80 <= fm["quality_score"] <= 92
    assert fm["financials"]["revenue"] == 130e9 and fm["financials"]["net_margin"] == pytest.approx(0.44)
    assert fm["valuation"]["pe_ratio"] == 50.0
    assert fm["analyst_estimates"] is None                               # no analyst data → NO DATA
    assert "Revenue growth" in fm["strengths"] and "High valuation" in fm["risks"]

    # re-collect → idempotent
    FundamentalsCollector(store, FakeProvider()).collect("NVDA")
    assert store.count_companies() == 1


def test_missing_data_is_no_data(store):
    fm = build_fundamentals(store, "NVDA")
    assert fm["company"] is None and fm["financials"] is None and fm["valuation"] is None
    assert fm["quality_score"] is None and fm["strengths"] == [] and fm["risks"] == []
    assert FundamentalsCollector(store, NullFundamentalsProvider()).collect("NVDA") is False
    assert store.count_companies() == 0


def test_provider_never_puts_key_in_url(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"results": {}}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr("atp.fundamentals.provider.urlopen", fake_urlopen)
    PolygonFundamentalsProvider(api_key="SECRETKEY123").get_company_profile("NVDA")
    assert "SECRETKEY123" not in captured["url"]
    assert captured["auth"] == "Bearer SECRETKEY123"


def test_no_secret_in_readmodel(store):
    FundamentalsCollector(store, FakeProvider()).collect("NVDA")
    blob = json.dumps(build_fundamentals(store, "NVDA")).lower()
    for secret in ("apikey", "api_key", "bearer", "authorization", "massive_api_key", "token"):
        assert secret not in blob


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "atp.db")
    s1 = open_store(path)
    FundamentalsCollector(s1, FakeProvider()).collect("NVDA")
    s1.close()
    s2 = open_store(path)                                                 # "restart"
    fm = build_fundamentals(s2, "NVDA")
    assert fm["company"]["company_name"] == "NVIDIA Corporation" and fm["quality_score"] is not None
